"""File-backed better-subagent Gateway domain service."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .contracts import (
    OCCUPYING_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    GatewayError,
    now_iso,
    run_status,
    session_summary,
    validate_interrupt_run,
    validate_session_config,
    validate_start_run,
)
from .transport import GatewayTransport, TransportOutcomeUnknown, TransportRejected


class JsonGatewayStore:
    """Small atomic JSON store; no queue or distributed locking."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._thread_lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            self._write_unlocked({"schemaVersion": 1, "sessions": {}, "runs": []})

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        with self._thread_lock:
            with self.lock_path.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    value = self._read_unlocked()
                    yield value
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, Any]:
        with self.locked() as value:
            return json.loads(json.dumps(value))

    def save(self, value: dict[str, Any]) -> None:
        self._write_unlocked(value)

    def _read_unlocked(self) -> dict[str, Any]:
        with self.path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise RuntimeError("better-subagent 数据文件 schemaVersion 非 1")
        if not isinstance(value.get("sessions"), dict) or not isinstance(value.get("runs"), list):
            raise RuntimeError("better-subagent 数据文件结构无效")
        return value

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix="better-subagent-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class GatewayService:
    def __init__(self, store: JsonGatewayStore, transport: GatewayTransport) -> None:
        self.store = store
        self.transport = transport

    def list_sessions(self) -> dict[str, Any]:
        board = self.store.read()
        summaries = [session_summary(session, board["runs"]) for session in board["sessions"].values()]
        summaries.sort(key=lambda item: (item["role"], item["owner"], item["sessionId"]))
        return {"sessions": summaries}

    def put_session(self, session_id: str, payload: Any) -> dict[str, Any]:
        record = validate_session_config(session_id, payload)
        with self.store.locked() as board:
            if any(
                run.get("sessionId") == session_id and run.get("status") in OCCUPYING_RUN_STATUSES
                for run in board["runs"]
            ):
                raise GatewayError("session_busy", "运行中的 Session 配置不能修改", status=409)
            board["sessions"][session_id] = record
            self.store.save(board)
            return {"session": session_summary(record, board["runs"])}

    def start_run(self, payload: Any) -> dict[str, Any]:
        request = validate_start_run(payload)
        prompt_hash = hashlib.sha256(request["prompt"].encode("utf-8")).hexdigest()
        with self.store.locked() as board:
            previous = next((run for run in board["runs"] if run.get("requestId") == request["requestId"]), None)
            if previous is not None:
                if previous.get("sessionId") != request["sessionId"] or previous.get("promptHash") != prompt_hash:
                    raise GatewayError(
                        "request_id_conflict",
                        "requestId 已绑定到不同的 StartRun",
                        status=409,
                        request_id=request["requestId"],
                    )
                return {"run": run_status(previous), "idempotent": True}
            session = board["sessions"].get(request["sessionId"])
            if not isinstance(session, dict):
                raise GatewayError("session_not_found", "目标 Session 不存在", status=404, request_id=request["requestId"])
            if not session.get("enabled", True):
                raise GatewayError(
                    "session_unavailable",
                    "目标 Session 当前不可调度",
                    status=409,
                    details={"reason": session.get("unavailableReason", "")},
                    request_id=request["requestId"],
                )
            occupying = next(
                (
                    run
                    for run in board["runs"]
                    if run.get("sessionId") == request["sessionId"] and run.get("status") in OCCUPYING_RUN_STATUSES
                ),
                None,
            )
            if occupying is not None:
                raise GatewayError(
                    "session_busy",
                    "目标 Session 已有未终结 Run",
                    status=409,
                    details={"gatewayRunId": occupying["gatewayRunId"], "status": occupying["status"]},
                    request_id=request["requestId"],
                )
            timestamp = now_iso()
            run = {
                "gatewayRunId": f"run-{uuid.uuid4().hex}",
                "requestId": request["requestId"],
                "sessionId": request["sessionId"],
                "promptHash": prompt_hash,
                "status": "starting",
                "createdAt": timestamp,
                "updatedAt": timestamp,
            }
            board["runs"].append(run)
            self.store.save(board)
            gateway_run_id = run["gatewayRunId"]
            runtime = {key: session[key] for key in ("threadId", "cwd", "model", "effort", "approvalPolicy", "sandboxPolicy")}

        def worker_result(status: str, error: str | None) -> None:
            if status == "unknown":
                self._set_unknown(gateway_run_id, error or "Codex SDK worker 状态失真")
            else:
                self._set_terminal(gateway_run_id, status, error)

        try:
            result = self.transport.start_turn(
                {"gatewayRunId": gateway_run_id, "prompt": request["prompt"], **runtime}, worker_result
            )
        except TransportOutcomeUnknown as exc:
            return {"run": self._set_unknown(gateway_run_id, str(exc)), "idempotent": False}
        except TransportRejected as exc:
            self._set_terminal(gateway_run_id, "failed", str(exc))
            raise GatewayError(
                "run_start_failed",
                "Codex Run 启动失败",
                status=502,
                details={"gatewayRunId": gateway_run_id, "reason": str(exc)[:1000]},
                request_id=request["requestId"],
            ) from exc
        except Exception as exc:
            self._set_terminal(gateway_run_id, "failed", str(exc))
            raise GatewayError(
                "run_start_failed",
                "Codex Run 启动失败",
                status=502,
                details={"gatewayRunId": gateway_run_id, "reason": str(exc)[:1000]},
                request_id=request["requestId"],
            ) from exc

        with self.store.locked() as board:
            run = self._find_run(board, gateway_run_id)
            run.update({key: result[key] for key in ("transportTurnId", "processId", "logPath", "stderrLogPath") if key in result})
            if run["status"] == "starting":
                run.update({"status": "active", "startedAt": now_iso(), "updatedAt": now_iso()})
            self.store.save(board)
            return {"run": run_status(run), "idempotent": False}

    def get_run(self, gateway_run_id: str) -> dict[str, Any]:
        board = self.store.read()
        return {"run": run_status(self._find_run(board, gateway_run_id))}

    def interrupt_run(self, gateway_run_id: str, payload: Any) -> dict[str, Any]:
        request = validate_interrupt_run(payload)
        with self.store.locked() as board:
            run = self._find_run(board, gateway_run_id)
            previous_request = run.get("interruptRequestId")
            if previous_request == request["requestId"] and run.get("status") in (
                TERMINAL_RUN_STATUSES | {"interrupting"}
            ):
                return {"run": run_status(run), "idempotent": True}
            if previous_request and previous_request != request["requestId"] and run.get("status") == "interrupting":
                raise GatewayError("interrupt_in_progress", "Run 正在处理另一个打断请求", status=409)
            if run.get("status") != "active":
                raise GatewayError(
                    "run_not_active",
                    "只有 active Run 可以打断",
                    status=409,
                    details={"status": run.get("status")},
                    request_id=request["requestId"],
                )
            if not run.get("transportTurnId") or not isinstance(run.get("processId"), int):
                raise GatewayError("run_not_interruptible", "Run 缺少当前 Gateway 持有的 worker", status=409)
            run.update({"status": "interrupting", "interruptRequestId": request["requestId"], "updatedAt": now_iso()})
            params = {"transportTurnId": run["transportTurnId"], "processId": run["processId"]}
            self.store.save(board)
        try:
            self.transport.interrupt_turn(params)
        except Exception as exc:
            with self.store.locked() as board:
                run = self._find_run(board, gateway_run_id)
                if run.get("status") == "interrupting":
                    run.update({"status": "active", "updatedAt": now_iso()})
                    self.store.save(board)
            raise GatewayError(
                "interrupt_failed", "Codex Run 打断失败", status=502,
                details={"reason": str(exc)[:1000]}, request_id=request["requestId"]
            ) from exc
        return {"run": self._set_terminal(gateway_run_id, "interrupted", None), "idempotent": False}

    def _set_unknown(self, gateway_run_id: str, error: str) -> dict[str, Any]:
        with self.store.locked() as board:
            run = self._find_run(board, gateway_run_id)
            if run.get("status") not in TERMINAL_RUN_STATUSES:
                run.update({"status": "unknown", "error": error[:1000], "updatedAt": now_iso()})
                self.store.save(board)
            return run_status(run)

    def _set_terminal(self, gateway_run_id: str, status: str, error: str | None) -> dict[str, Any]:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"invalid terminal status: {status}")
        with self.store.locked() as board:
            run = self._find_run(board, gateway_run_id)
            if run.get("status") not in TERMINAL_RUN_STATUSES:
                timestamp = now_iso()
                run.update({"status": status, "updatedAt": timestamp, "terminalAt": timestamp, "error": error})
                self.store.save(board)
            return run_status(run)

    @staticmethod
    def _find_run(board: dict[str, Any], gateway_run_id: str) -> dict[str, Any]:
        run = next((item for item in board["runs"] if item.get("gatewayRunId") == gateway_run_id), None)
        if run is None:
            raise GatewayError("run_not_found", "Gateway Run 不存在", status=404)
        return run
