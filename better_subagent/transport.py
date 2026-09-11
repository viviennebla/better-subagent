"""Codex SDK worker transport owned by the standalone Gateway process."""

from __future__ import annotations

import json
import os
import base64
import hashlib
import hmac
import secrets
import socket
import struct
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


NotificationCallback = Callable[[dict[str, Any]], None]
ApprovalCallback = Callable[[str, dict[str, Any]], None]


class AppServerTransport:
    """Long-lived JSON-RPC client for the GUI-managed App Server socket.

    This deliberately implements the small WebSocket subset used by the local
    Unix control socket.  It avoids starting, stopping, or proxying an App
    Server process; the caller owns the socket lifecycle.
    """

    def __init__(self, socket_path: Path | str, *, reconnect_delay: float = 0.2) -> None:
        self.socket_path = str(socket_path)
        self.reconnect_delay = reconnect_delay
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._next_id = 1
        self._generation = 0
        self._connected = threading.Event()
        self._stopping = False
        self._turns: dict[str, tuple[TerminalCallback, str]] = {}
        self._notifications: list[NotificationCallback] = []
        self._approval: ApprovalCallback | None = None
        self._pending_approvals: dict[str, dict[str, Any] | str] = {}
        self._approval_responses: set[str] = set()
        self._orphan_terminals: dict[tuple[int, str, str], tuple[str, str | None]] = {}
        self._frame_buffer = bytearray()
        self._message_buffer = bytearray()
        self.last_disconnect_reason: str | None = None

    def add_notification_listener(self, callback: NotificationCallback) -> None:
        self._notifications.append(callback)

    def set_approval_handler(self, callback: ApprovalCallback | None) -> None:
        self._approval = callback

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def connect(self, timeout: float = 5.0) -> dict[str, Any]:
        with self._lock:
            if self.connected:
                return {"connected": True, "generation": self._generation}
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(self.socket_path)
            key = base64.b64encode(secrets.token_bytes(16)).decode()
            request = (
                "GET / HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
            sock.sendall(request)
            header = self._read_http_header(sock)
            if b" 101 " not in header.split(b"\r\n", 1)[0]:
                sock.close()
                raise TransportRejected("App Server Unix WebSocket Upgrade 失败")
            expected = base64.b64encode(hashlib.sha1(key.encode() + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()).decode().encode()
            accept = next((line.split(b":", 1)[1].strip() for line in header.split(b"\r\n") if line.lower().startswith(b"sec-websocket-accept:")), b"")
            if not hmac.compare_digest(accept, expected):
                sock.close()
                raise TransportRejected("App Server WebSocket Sec-WebSocket-Accept 校验失败")
            sock.settimeout(None)
            self._socket = sock
            self._generation += 1
            self._frame_buffer.clear()
            self._message_buffer.clear()
            self._connected.set()
            self._stopping = False
            self._reader = threading.Thread(target=self._read_loop, args=(sock, self._generation), name="better-subagent-app-server", daemon=True)
            self._reader.start()
        result = self.request("initialize", {"clientInfo": {"name": "better-subagent", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}}, timeout=timeout)
        # The protocol expects initialize/initialized before normal traffic.
        try:
            self.notify("initialized", {})
        except Exception:
            pass
        return result

    def close(self) -> None:
        self._stopping = True
        self._connected.clear()
        with self._lock:
            sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def reconnect(self, timeout: float = 5.0) -> dict[str, Any]:
        self.close()
        time.sleep(self.reconnect_delay)
        return self.connect(timeout)

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        with self._lock:
            if not self.connected or self._socket is None:
                raise TransportOutcomeUnknown("App Server transport 未连接，结果未知")
            request_id = self._next_id
            self._next_id += 1
            event = threading.Event()
            slot: dict[str, Any] = {}
            self._pending[request_id] = (event, slot)
            try:
                self._send_json({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            except Exception as exc:
                self._pending.pop(request_id, None)
                self._mark_disconnected(f"send failed: {exc}")
                raise TransportOutcomeUnknown("App Server 请求发送后连接断开") from exc
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TransportOutcomeUnknown(f"App Server 请求超时: {method}")
        if "error" in slot:
            error = slot["error"]
            raise TransportRejected(str(error.get("message", error)) if isinstance(error, dict) else str(error))
        return slot.get("result") or {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            if not self.connected or self._socket is None:
                raise TransportOutcomeUnknown("App Server transport 未连接")
            self._send_json({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def read_thread(self, thread_id: str, *, include_turns: bool = True) -> dict[str, Any]:
        self._ensure_connected()
        return self.request("thread/read", {"threadId": thread_id, "includeTurns": include_turns})

    def resume_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.request("thread/resume", params)

    def start_turn(self, params: dict[str, Any], on_terminal: TerminalCallback) -> dict[str, Any]:
        self._ensure_connected()
        request = dict(params)
        request.pop("gatewayRunId", None)
        policy = request.pop("requestedPolicy", None)
        if policy and isinstance(policy, dict):
            request["permissions"] = policy.get("permissionProfileId", ":workspace")
            request["approvalPolicy"] = policy.get("approvalPolicy", request.get("approvalPolicy", "on-request"))
            request["approvalsReviewer"] = policy.get("approvalsReviewer", "auto_review")
            request["runtimeWorkspaceRoots"] = policy.get("runtimeWorkspaceRoots", request.get("runtimeWorkspaceRoots", []))
        prompt = request.pop("prompt")
        request["input"] = [{"type": "text", "text": prompt}]
        thread_id = str(request.get("threadId", ""))
        effective_policy = None
        if thread_id:
            resume = dict(request)
            resume.pop("input", None)
            resume.pop("model", None)
            resume.pop("effort", None)
            resume.pop("sandboxPolicy", None)
            try:
                read_result = self.read_thread(thread_id, include_turns=False)
                need_resume = False
            except TransportRejected as exc:
                message = str(exc).lower()
                missing = message.startswith(("no rollout found for thread id", "thread not loaded", "thread notloaded"))
                if not missing:
                    raise
                need_resume = True
                read_result = None
            if need_resume:
                resume_result = self.resume_thread(resume)
                effective_policy = resume_result.get("activePermissionProfile")
                thread = resume_result.get("thread") or {}
                resume_status = thread.get("status") or resume_result.get("status") or {}
                status_type = resume_status.get("type") if isinstance(resume_status, dict) else resume_status
                if status_type != "idle":
                    raise TransportRejected(f"Session 当前不可启动 turn: {status_type or 'unknown'}")
                read_result = self.read_thread(thread_id, include_turns=False)
            read_thread = read_result.get("thread") or read_result
            read_status = read_thread.get("status") or {}
            read_type = read_status.get("type") if isinstance(read_status, dict) else read_status
            if read_type == "notLoaded" and not need_resume:
                resume_result = self.resume_thread(resume)
                effective_policy = resume_result.get("activePermissionProfile")
                thread = resume_result.get("thread") or {}
                resume_status = thread.get("status") or resume_result.get("status") or {}
                status_type = resume_status.get("type") if isinstance(resume_status, dict) else resume_status
                if status_type != "idle":
                    raise TransportRejected(f"Session 当前不可启动 turn: {status_type or 'unknown'}")
                read_result = self.read_thread(thread_id, include_turns=False)
                read_thread = read_result.get("thread") or read_result
                read_status = read_thread.get("status") or {}
                read_type = read_status.get("type") if isinstance(read_status, dict) else read_status
            if read_type != "idle":
                raise TransportRejected(f"Session read 状态不可启动 turn: {read_type or 'unknown'}")
            if effective_policy is None:
                effective_policy = read_result.get("activePermissionProfile")
        # App Server uses permissions for named profiles; sandboxPolicy is a
        # legacy Gateway field and must never be sent alongside permissions.
        sandbox = request.pop("sandboxPolicy", None)
        if "permissionProfileId" in request:
            request["permissions"] = request.pop("permissionProfileId")
        if sandbox is not None and "permissions" not in request:
            request["sandboxPolicy"] = sandbox
        result = self.request("turn/start", request)
        turn = result.get("turn") if isinstance(result.get("turn"), dict) else result
        turn_id = str((turn or {}).get("id") or result.get("turnId") or f"rpc-turn-{self._next_id}")
        with self._lock:
            self._turns[turn_id] = (on_terminal, str(params.get("threadId", "")))
            orphan = self._orphan_terminals.pop((self._generation, str(params.get("threadId", "")), turn_id), None)
        if orphan is not None:
            on_terminal(orphan[0], orphan[1])
        result_value = {"transportTurnId": turn_id, "processId": self._generation}
        if effective_policy is not None:
            result_value["effectivePolicy"] = effective_policy
        return result_value

    def steer_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.request("turn/steer", {"threadId": params["threadId"], "expectedTurnId": params["expectedTurnId"], "input": [{"type": "text", "text": params["prompt"]}]})

    def interrupt_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        turn_id = params.get("transportTurnId") or params.get("turnId")
        return self.request("turn/interrupt", {"threadId": params["threadId"], "turnId": turn_id})

    def respond_approval(self, request_id: str | int, decision: str, *, permissions: dict[str, Any] | None = None, method: str | None = None, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            pending = self._pending_approvals.get(str(request_id))
            method = method or (pending.get("method") if isinstance(pending, dict) else pending)
            if str(request_id) in self._approval_responses:
                raise TransportRejected("审批响应已发送，等待 serverRequest/resolved")
        if method is None:
            raise TransportRejected("未知或已解决的审批请求")
        cancel_params = params if method == "item/permissions/requestApproval" and decision == "cancel" else None
        if cancel_params is not None and (not isinstance(cancel_params, dict) or not cancel_params.get("threadId") or not cancel_params.get("turnId")):
            raise TransportRejected("permissions cancel 缺少 threadId/turnId，无法终止 turn")
        if method == "item/permissions/requestApproval":
            if decision in {"accept", "acceptForSession"} and permissions is None:
                raise TransportRejected("permissions 审批需要 granted permissions")
            result = {"permissions": permissions or {}, "scope": "session" if decision == "acceptForSession" else "turn"}
        elif method == "item/commandExecution/requestApproval":
            result = {"decision": "acceptForSession" if decision == "acceptForSession" else decision}
        elif method == "item/fileChange/requestApproval":
            result = {"decision": "acceptForSession" if decision == "acceptForSession" else ("accept" if decision == "accept" else decision)}
        else:
            raise TransportRejected(f"不支持的审批请求类型: {method}")
        with self._lock:
            if not self.connected:
                raise TransportOutcomeUnknown("App Server transport 未连接")
            self._send_json({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            })
            self._approval_responses.add(str(request_id))
        if cancel_params is not None:
            # The permissions response has no decision field. The interrupt
            # is a separate RPC, deliberately sent after releasing _lock.
            self.interrupt_turn({"threadId": cancel_params["threadId"], "turnId": cancel_params["turnId"]})

    def _ensure_connected(self) -> None:
        if not self.connected:
            self.connect()

    def _read_http_header(self, sock: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise TransportRejected("App Server 在 WebSocket Upgrade 前断开")
            data.extend(chunk)
            if len(data) > 32_768:
                raise TransportRejected("App Server Upgrade header 过大")
        marker = data.index(b"\r\n\r\n") + 4
        self._frame_buffer.extend(data[marker:])
        return bytes(data[:marker])

    def _send_json(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        length = len(masked)
        if length < 126:
            header = bytes([0x81, 0x80 | length])
        elif length < 65536:
            header = bytes([0x81, 0xFE]) + struct.pack("!H", length)
        else:
            header = bytes([0x81, 0xFF]) + struct.pack("!Q", length)
        assert self._socket is not None
        self._socket.sendall(header + mask + masked)

    def _read_loop(self, sock: socket.socket, generation: int) -> None:
        try:
            while self.connected and self._socket is sock and generation == self._generation:
                fin, opcode, payload = self._read_frame(sock)
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self._send_control(0xA, payload)
                    continue
                if opcode == 0x1:
                    self._message_buffer = bytearray(payload)
                elif opcode == 0x0:
                    self._message_buffer.extend(payload)
                else:
                    continue
                if not fin:
                    continue
                message = json.loads(bytes(self._message_buffer).decode("utf-8"))
                self._message_buffer.clear()
                if "id" in message and ("result" in message or "error" in message):
                    with self._lock:
                        pending = self._pending.pop(int(message["id"]), None)
                    if pending:
                        event, slot = pending
                        slot.update({key: message[key] for key in ("result", "error") if key in message})
                        event.set()
                    continue
                self._handle_notification(message, generation)
        except Exception as exc:
            if not self._stopping:
                self._mark_disconnected(str(exc), generation)
        finally:
            if not self._stopping and generation == self._generation:
                self._mark_disconnected(self.last_disconnect_reason or "App Server WebSocket 已断开", generation)

    def _read_frame(self, sock: socket.socket) -> tuple[bool, int, bytes]:
        head = self._recv_exact(sock, 2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(sock, 8))[0]
        masked = bool(head[1] & 0x80)
        mask = self._recv_exact(sock, 4) if masked else b""
        payload = self._recv_exact(sock, length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def _recv_exact(self, sock: socket.socket, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            needed = length - len(data)
            if self._frame_buffer:
                chunk = bytes(self._frame_buffer[:needed])
                del self._frame_buffer[:len(chunk)]
            else:
                chunk = sock.recv(needed)
            if not chunk:
                raise ConnectionError("socket closed")
            data.extend(chunk)
        return bytes(data)

    def _send_control(self, opcode: int, payload: bytes) -> None:
        with self._lock:
            if self._socket is None:
                return
            mask = secrets.token_bytes(4)
            masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            self._socket.sendall(bytes([0x80 | opcode, 0x80 | len(masked)]) + mask + masked)

    def _mark_disconnected(self, reason: str, generation: int | None = None) -> None:
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self.last_disconnect_reason = reason
            self._connected.clear()
            pending, self._pending = self._pending, {}
            turns, self._turns = self._turns, {}
            self._orphan_terminals.clear()
            self._frame_buffer.clear()
            self._message_buffer.clear()
        for event, slot in pending.values():
            slot["error"] = {"message": f"transport disconnected: {reason}"}
            event.set()
        for callback, _thread_id in turns.values():
            try:
                callback("unknown", f"App Server transport disconnected: {reason}")
            except Exception:
                pass
        for callback in self._notifications:
            try:
                callback({"method": "transport/status", "params": {"status": "unknown", "reason": reason}})
            except Exception:
                pass

    def _handle_notification(self, message: dict[str, Any], generation: int | None = None) -> None:
        method = message.get("method", "")
        params = message.get("params") or {}
        if method == "turn/completed":
            turn = params.get("turn") or {}
            turn_id = str(turn.get("id", ""))
            with self._lock:
                entry = self._turns.pop(turn_id, None)
            if entry:
                status = {"completed": "completed", "interrupted": "interrupted", "failed": "failed"}.get(str(turn.get("status")), "unknown")
                error = turn.get("error")
                entry[0](status, error.get("message") if isinstance(error, dict) else error)
            else:
                status = {"completed": "completed", "interrupted": "interrupted", "failed": "failed"}.get(str(turn.get("status")), "unknown")
                error = turn.get("error")
                with self._lock:
                    thread_id = str(params.get("threadId", ""))
                    self._orphan_terminals[(generation or self._generation, thread_id, turn_id)] = (status, error.get("message") if isinstance(error, dict) else error)
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"}:
            request_id = message.get("id")
            self._pending_approvals[str(request_id)] = {"method": method, "params": params, "threadId": params.get("threadId")}
            if self._approval is not None:
                self._approval(request_id, {**params, "_requestMethod": method})
        if method == "serverRequest/resolved":
            request_id = params.get("requestId") or params.get("id")
            if request_id is not None:
                key = str(request_id)
                pending = self._pending_approvals.get(key)
                pending_thread = pending.get("threadId") if isinstance(pending, dict) else None
                resolved_thread = params.get("threadId")
                if pending_thread is not None and resolved_thread == pending_thread:
                    self._pending_approvals.pop(key, None)
                    self._approval_responses.discard(key)
        for callback in self._notifications:
            callback(message)


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
