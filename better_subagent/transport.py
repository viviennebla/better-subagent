"""Codex SDK worker transport owned by the standalone Gateway process."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol

from .contracts import STAGE_REPORT_OUTPUT_SCHEMA


TerminalCallback = Callable[[str, str | None], None]


class GatewayTransport(Protocol):
    def start_turn(self, params: dict[str, Any], on_terminal: TerminalCallback) -> dict[str, Any]: ...

    def interrupt_turn(self, params: dict[str, Any]) -> dict[str, Any]: ...


class TransportRejected(Exception):
    """The SDK definitely rejected a run before authoritative start."""


class TransportOutcomeUnknown(Exception):
    """The SDK may have started a run, but the authoritative result was not observed."""


class CodexSdkWorkerTransport:
    """Run the pinned TypeScript SDK worker, one bounded child per active Run."""

    def __init__(
        self,
        *,
        worker_path: Path | None = None,
        node_command: tuple[str, ...] = ("node",),
        log_dir: Path | None = None,
        startup_timeout_seconds: float = 30.0,
    ) -> None:
        if startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        root = Path(__file__).resolve().parents[1]
        self.worker_path = worker_path or root / "scripts" / "codex-sdk-worker.mjs"
        self.node_command = node_command
        self.log_dir = log_dir or root / "data" / "better-subagent-runtime"
        self.startup_timeout_seconds = startup_timeout_seconds
        self._children: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _request(params: dict[str, Any]) -> dict[str, Any]:
        sandbox = params["sandboxPolicy"]
        if sandbox not in {"read-only", "workspace-write"}:
            raise TransportRejected(f"不支持的 sandboxPolicy: {sandbox}")
        return {
            "codexPath": os.environ.get("VIMO_CODEX_BIN", "codex"),
            "threadId": params["threadId"],
            "prompt": params["prompt"],
            "cwd": params["cwd"],
            "model": params["model"],
            "effort": {"max": "xhigh"}.get(params["effort"], params["effort"]),
            "approvalPolicy": params["approvalPolicy"],
            "sandbox": sandbox,
            "additionalDirectories": [params["cwd"]] if sandbox == "workspace-write" else [],
            "outputSchema": STAGE_REPORT_OUTPUT_SCHEMA,
        }

    def start_turn(self, params: dict[str, Any], on_terminal: TerminalCallback) -> dict[str, Any]:
        request = self._request(params)
        gateway_run_id = params["gatewayRunId"]
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.log_dir / f"{gateway_run_id}.stdout.log"
        stderr_path = self.log_dir / f"{gateway_run_id}.stderr.log"
        stdout_log = stdout_path.open("ab", buffering=0)
        stderr_log = stderr_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                [*self.node_command, str(self.worker_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_log,
                text=True,
                encoding="utf-8",
                bufsize=1,
                close_fds=True,
            )
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except Exception:
            stdout_log.close()
            stderr_log.close()
            raise

        transport_turn_id = f"sdk-{process.pid}-{uuid.uuid4().hex[:12]}"
        state_lock = threading.Lock()
        child: dict[str, Any] = {
            "process": process,
            "stdin": process.stdin,
            "stdout": process.stdout,
            "stdoutLog": stdout_log,
            "stderrLog": stderr_log,
            "processId": process.pid,
            "threadStarted": False,
            "observedThreadId": None,
            "turnStarted": False,
            "started": threading.Event(),
            "outcomeUnknown": threading.Event(),
            "interruptAck": threading.Event(),
            "startupError": None,
            "identityError": None,
            "prematureTerminal": False,
            "authoritative": False,
            "terminalNotified": False,
            "cleaned": False,
        }

        def establish_authority_locked() -> None:
            if (
                child["threadStarted"]
                and child["observedThreadId"] == request["threadId"]
                and child["turnStarted"]
                and not child["startupError"]
                and not child["identityError"]
                and not child["prematureTerminal"]
            ):
                child["authoritative"] = True
                child["started"].set()

        def notify(status: str, error: str | None = None) -> None:
            with self._lock:
                if child["terminalNotified"]:
                    return
                child["terminalNotified"] = True
            on_terminal(status, error)

        def startup_error(message: str) -> None:
            with state_lock:
                if not child["startupError"]:
                    child["startupError"] = message
                observed_error = child["startupError"]
                was_authoritative = child["authoritative"]
                child["authoritative"] = False
                if child["turnStarted"]:
                    child["outcomeUnknown"].set()
            if was_authoritative:
                notify("unknown", observed_error)

        def terminal_event(status: str, error: str | None = None) -> None:
            with state_lock:
                authoritative = child["authoritative"] and not child["startupError"]
                if not authoritative:
                    child["prematureTerminal"] = True
                    if child["turnStarted"]:
                        child["outcomeUnknown"].set()
            if authoritative:
                notify(status, error)

        def read_events() -> None:
            try:
                for line in process.stdout:
                    stdout_log.write(line.encode("utf-8", errors="replace"))
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        startup_error("Codex SDK worker 返回非法事件")
                        continue
                    event_type = event.get("type")
                    if event_type == "thread.started":
                        post_authority_error = None
                        with state_lock:
                            was_authoritative = child["authoritative"]
                            child["threadStarted"] = True
                            child["observedThreadId"] = event.get("thread_id")
                            if child["observedThreadId"] != request["threadId"]:
                                child["identityError"] = "Codex SDK resume 返回了不一致的 thread id"
                                child["authoritative"] = False
                                if child["turnStarted"]:
                                    child["outcomeUnknown"].set()
                                if was_authoritative:
                                    post_authority_error = child["identityError"]
                            establish_authority_locked()
                        if post_authority_error:
                            notify("unknown", post_authority_error)
                    elif event_type == "turn.started":
                        with state_lock:
                            child["turnStarted"] = True
                            if child["startupError"] or child["identityError"] or child["prematureTerminal"]:
                                child["outcomeUnknown"].set()
                            establish_authority_locked()
                    elif event_type == "turn.completed":
                        terminal_event("completed")
                    elif event_type in {"turn.failed", "error", "dispatch.error"}:
                        error = event.get("error")
                        message = event.get("message") or (error.get("message") if isinstance(error, dict) else None)
                        with state_lock:
                            turn_started = child["turnStarted"]
                        if not turn_started:
                            startup_error(message or "Codex SDK turn 启动失败")
                        else:
                            terminal_event("failed", message or "Codex SDK turn 失败")
                    elif event_type == "dispatch.interrupted":
                        child["interruptAck"].set()
                        terminal_event("interrupted")
            finally:
                with state_lock:
                    authoritative = child["authoritative"] and not child["startupError"]
                if authoritative and not child["terminalNotified"]:
                    notify("failed", "SDK worker 在 terminal 事件前退出")
                self._cleanup(transport_turn_id, child)

        reader = threading.Thread(target=read_events, name=f"better-subagent-{process.pid}", daemon=True)
        child["reader"] = reader
        # Register before consuming events. A tiny turn can emit turn.started and
        # turn.completed in the same scheduler slice; cleanup must then remove an
        # existing registration instead of making a successful turn look rejected.
        with self._lock:
            self._children[transport_turn_id] = child
        reader.start()
        deadline = time.monotonic() + self.startup_timeout_seconds
        while (
            not child["started"].is_set()
            and not child["outcomeUnknown"].is_set()
            and process.poll() is None
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.02, remaining))
        with state_lock:
            start_is_authoritative = (
                child["started"].is_set()
                and child["authoritative"]
                and not child["startupError"]
                and not child["identityError"]
                and not child["prematureTerminal"]
            )
        if not start_is_authoritative:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            reader.join(timeout=1)
            self._cleanup(transport_turn_id, child)
            with state_lock:
                observed_error = child["startupError"] or child["identityError"]
                turn_started = child["turnStarted"]
                outcome_unknown = child["outcomeUnknown"].is_set()
            if turn_started or outcome_unknown:
                raise TransportOutcomeUnknown(
                    observed_error or "Codex SDK 在权威 Session identity 确认前报告了 turn.started"
                )
            if observed_error:
                raise TransportRejected(observed_error)
            raise TransportOutcomeUnknown("Codex SDK 未在有界窗口内报告权威 turn.started")
        return {
            "transportTurnId": transport_turn_id,
            "processId": process.pid,
            "logPath": str(stdout_path),
            "stderrLogPath": str(stderr_path),
        }

    def interrupt_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        turn_id = params.get("transportTurnId")
        process_id = params.get("processId")
        with self._lock:
            child = self._children.get(turn_id)
        if child is None or child["processId"] != process_id:
            raise RuntimeError("没有 Gateway 当前持有且匹配的 SDK worker")
        process = child["process"]
        if process.poll() is not None:
            self._cleanup(turn_id, child)
            raise RuntimeError("SDK worker 已退出，无法确认打断")
        child["stdin"].write("interrupt\n")
        child["stdin"].flush()
        if not child["interruptAck"].wait(5):
            raise RuntimeError("SDK worker 未返回 interrupt 终止事实")
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("SDK worker interrupt 后仍未退出") from exc
        self._cleanup(turn_id, child)
        return {"status": "terminated"}

    def _cleanup(self, turn_id: str, child: dict[str, Any]) -> None:
        with self._lock:
            if child["cleaned"]:
                return
            child["cleaned"] = True
            if self._children.get(turn_id) is child:
                self._children.pop(turn_id, None)
        process = child["process"]
        if process.poll() is None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        reader = child.get("reader")
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1)
        for key in ("stdin", "stdout", "stdoutLog", "stderrLog"):
            handle = child.get(key)
            if handle is not None and not handle.closed:
                handle.close()
