"""File-backed better-subagent Gateway domain service."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .contracts import (
    OCCUPYING_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    GatewayError,
    now_iso,
    run_status,
    session_summary,
    validate_interrupt_run,
    validate_approval_decision,
    validate_steer_run,
    validate_session_config,
    validate_start_run,
    validate_session_control,
)
from .transport import GatewayTransport, TransportOutcomeUnknown, TransportRejected


def _normalize_app_server_timestamp(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return normalized
    return None


from .storage import JsonGatewayStore, SqliteGatewayStore

_SUPPORTED_APPROVAL_DECISIONS = frozenset({"accept", "decline", "cancel", "acceptForSession"})


def _supported_approval_decisions(value: Any) -> list[str]:
    """Expose only approval decisions the Gateway can actually submit."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item in _SUPPORTED_APPROVAL_DECISIONS]


class GatewayService:
    THREAD_LOOKUP_PAGE_SIZE = 100
    THREAD_LOOKUP_MAX_PAGES = 10

    RUN_RECONCILE_PAGE_SIZE = 100
    RUN_RECONCILE_MAX_PAGES = 10
    RUN_RECONCILE_RETRY_SECONDS = 30.0

    OVERVIEW_ROUND_LIMIT = 1000
    OVERVIEW_ROUND_PAGE_SIZE = 200
    OVERVIEW_ROUND_WORKERS = 8

    def __init__(self, store: SqliteGatewayStore, transport: GatewayTransport) -> None:
        self.store = store
        self.transport = transport
        self._live_run_ids: set[str] = set()
        self._run_reconcile_inflight: set[str] = set()
        self._run_reconcile_after: dict[str, float] = {}
        self._live_run_lock = threading.Lock()
        self._runtime_projection_lock = threading.Lock()
        self._recent_terminal_turns: deque[tuple[str, str]] = deque(maxlen=256)
        listener = getattr(transport, "add_notification_listener", None)
        if listener is not None:
            listener(self._on_transport_notification)
        approval_handler = getattr(transport, "set_approval_handler", None)
        if approval_handler is not None:
            approval_handler(self._on_approval_request)
        # Persisted occupying Runs have no process-local terminal callback after
        # a Gateway restart. Reconcile them from exact App Server Turn facts;
        # failures remain fail-closed and must not prevent Gateway startup.
        self._reconcile_orphaned_runs()

    def _remember_live_run(self, gateway_run_id: str) -> None:
        with self._live_run_lock:
            self._live_run_ids.add(gateway_run_id)

    def _forget_live_run(self, gateway_run_id: str) -> None:
        with self._live_run_lock:
            self._live_run_ids.discard(gateway_run_id)

    def _claim_run_reconcile(self, gateway_run_id: str) -> bool:
        with self._live_run_lock:
            if (
                gateway_run_id in self._live_run_ids
                or gateway_run_id in self._run_reconcile_inflight
            ):
                return False
            now = time.monotonic()
            if now < self._run_reconcile_after.get(gateway_run_id, 0.0):
                return False
            self._run_reconcile_inflight.add(gateway_run_id)
            return True

    def _forget_run_reconcile(self, gateway_run_id: str) -> None:
        with self._live_run_lock:
            self._run_reconcile_after.pop(gateway_run_id, None)

    def _finish_run_reconcile(self, gateway_run_id: str) -> None:
        with self._live_run_lock:
            self._run_reconcile_inflight.discard(gateway_run_id)
            self._run_reconcile_after[gateway_run_id] = (
                time.monotonic() + self.RUN_RECONCILE_RETRY_SECONDS
            )

    @staticmethod
    def _turn_terminal_fact(turn: dict[str, Any]) -> tuple[str, str | None] | None:
        status = {
            "completed": "completed",
            "interrupted": "interrupted",
            "failed": "failed",
        }.get(str(turn.get("status")))
        if status is None:
            return None
        error = turn.get("error")
        return status, error.get("message") if isinstance(error, dict) else error

    def _find_runtime_turn(
        self, thread_id: str, transport_turn_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return the exact managed Turn and any observed newer active Turn."""
        lister = getattr(self.transport, "list_thread_turns", None)
        if not callable(lister):
            return None, None
        cursor: str | None = None
        active_turn: dict[str, Any] | None = None
        for _page in range(self.RUN_RECONCILE_MAX_PAGES):
            response = lister(
                thread_id,
                cursor=cursor,
                limit=self.RUN_RECONCILE_PAGE_SIZE,
                items_view="notLoaded",
            )
            turns = response.get("data") if isinstance(response, dict) else None
            if not isinstance(turns, list):
                return None, active_turn
            for turn in turns[:self.RUN_RECONCILE_PAGE_SIZE]:
                if not isinstance(turn, dict):
                    continue
                if active_turn is None and turn.get("status") in {
                    "active", "inProgress", "running"
                }:
                    active_turn = turn
                if turn.get("id") == transport_turn_id:
                    return turn, active_turn
            next_cursor = response.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return None, active_turn

    def _project_external_turn(self, thread_id: str, turn: dict[str, Any]) -> None:
        if not isinstance(turn.get("id"), str) or not turn["id"]:
            return
        turn_id = turn["id"]
        with self._runtime_projection_lock:
            if (thread_id, turn_id) in self._recent_terminal_turns:
                return
            with self.store.locked() as board:
                changed = False
                for session in board["sessions"].values():
                    if session.get("threadId") != thread_id:
                        continue
                    managed = any(
                        run.get("sessionId") == session.get("sessionId")
                        and run.get("status") in OCCUPYING_RUN_STATUSES
                        and run.get("transportTurnId") == turn_id
                        for run in board["runs"]
                    )
                    if managed:
                        continue
                    session.update({
                        "controlMode": "external",
                        "requestedControlMode": "external",
                        "runtimeStatus": "active",
                        "activeTurnId": turn_id,
                        "updatedAt": now_iso(),
                    })
                    changed = True
                if changed:
                    self.store.save(board)

    def _reconcile_orphaned_run(self, gateway_run_id: str) -> dict[str, Any] | None:
        board = self.store.read()
        try:
            run = self._find_run(board, gateway_run_id)
        except GatewayError:
            return None
        if run.get("status") not in OCCUPYING_RUN_STATUSES:
            return run_status(run)
        session = board["sessions"].get(run.get("sessionId"))
        thread_id = session.get("threadId") if isinstance(session, dict) else None
        turn_id = run.get("transportTurnId")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(turn_id, str) or not turn_id:
            return None
        try:
            turn, active_turn = self._find_runtime_turn(thread_id, turn_id)
        except Exception:
            return None
        terminal = self._turn_terminal_fact(turn) if isinstance(turn, dict) else None
        result = None
        # Project a newer external Turn before closing the stale managed Run.
        # This keeps the Session projection monotonic: _set_terminal only clears
        # runtime state when activeTurnId still belongs to the managed Run.
        if isinstance(active_turn, dict) and active_turn.get("id") != turn_id:
            self._project_external_turn(thread_id, active_turn)
        if terminal is not None:
            result = self._set_terminal(
                gateway_run_id,
                terminal[0],
                terminal[1],
                execution_established=True,
            )
        return result

    def _schedule_run_reconcile(self, gateway_run_id: str) -> None:
        if not self._claim_run_reconcile(gateway_run_id):
            return
        def worker() -> None:
            try:
                self._reconcile_orphaned_run(gateway_run_id)
            finally:
                self._finish_run_reconcile(gateway_run_id)

        try:
            threading.Thread(
                target=worker,
                name=f"gateway-reconcile-{gateway_run_id[:24]}",
                daemon=True,
            ).start()
        except Exception:
            self._finish_run_reconcile(gateway_run_id)
            raise

    def _reconcile_orphaned_runs(self) -> None:
        if not callable(getattr(self.transport, "list_thread_turns", None)):
            return
        board = self.store.read()
        for run in board["runs"]:
            gateway_run_id = run.get("gatewayRunId")
            if (
                isinstance(gateway_run_id, str)
                and run.get("status") in OCCUPYING_RUN_STATUSES
            ):
                self._schedule_run_reconcile(gateway_run_id)

    def _runtime_defaults(self) -> dict[str, str | None]:
        """Read deployment defaults; never invent a workspace/model/effort."""
        model = os.environ.get("BETTER_SUBAGENT_DEFAULT_MODEL") or os.environ.get("CODEX_MODEL")
        effort = os.environ.get("BETTER_SUBAGENT_DEFAULT_EFFORT") or os.environ.get("CODEX_EFFORT")
        cwd = os.environ.get("BETTER_SUBAGENT_DEFAULT_CWD") or os.getcwd()
        return {
            "cwd": cwd,
            "model": model,
            "effort": effort,
            "approvalPolicy": os.environ.get("BETTER_SUBAGENT_DEFAULT_APPROVAL_POLICY", "on-request"),
            "sandboxPolicy": os.environ.get("BETTER_SUBAGENT_DEFAULT_SANDBOX_POLICY", "workspace-write"),
        }

    def _resolve_minimal_runtime(self, session_id: str) -> dict[str, Any]:
        lister = getattr(self.transport, "list_threads", None)
        reader = getattr(self.transport, "read_thread", None)
        if not callable(reader):
            raise GatewayError("transport_unsupported", "当前 App Server transport 不支持 thread/read", status=501)
        thread_id = session_id
        response: dict[str, Any] | None = None
        try:
            direct = reader(session_id, include_turns=False)
        except TransportRejected as exc:
            message = str(exc).lower()
            missing = message.startswith((
                "no rollout found for thread id",
                "thread not found",
                "thread_not_found",
            ))
            if not missing:
                raise GatewayError(
                    "runtime_unavailable",
                    "App Server thread/read 读取失败",
                    status=502,
                    details={"threadId": session_id, "reason": str(exc)[:1000]},
                ) from exc
        except Exception as exc:
            raise GatewayError(
                "runtime_unavailable",
                "App Server thread/read 读取失败",
                status=502,
                details={"threadId": session_id, "reason": str(exc)[:1000]},
            ) from exc
        else:
            response = direct if isinstance(direct, dict) else None

        # Exact UUID lookup is authoritative and includes persisted subagent
        # threads that may not be returned by thread/list. Keep bounded listing
        # only as a compatibility fallback when callers provide sessionId rather
        # than the underlying thread id.
        if response is None and callable(lister):
            response: dict[str, Any] = {}
            match: dict[str, Any] | None = None
            cursor: str | None = None
            for page_number in range(self.THREAD_LOOKUP_MAX_PAGES):
                try:
                    if cursor is None:
                        response = lister(limit=self.THREAD_LOOKUP_PAGE_SIZE)
                    else:
                        response = lister(cursor=cursor, limit=self.THREAD_LOOKUP_PAGE_SIZE)
                except Exception as exc:
                    raise GatewayError("runtime_unavailable", "App Server thread/list 读取失败", status=502, details={"reason": str(exc)[:1000]}) from exc
                threads = response.get("data", []) if isinstance(response, dict) else []
                match = next((item for item in threads if isinstance(item, dict) and (item.get("id") == session_id or item.get("sessionId") == session_id)), None)
                if isinstance(match, dict) and isinstance(match.get("id"), str) and match["id"]:
                    thread_id = match["id"]
                    break
                next_cursor = response.get("nextCursor") if isinstance(response, dict) else None
                if not next_cursor:
                    raise GatewayError("thread_not_found", "App Server thread/list 未找到目标 Session", status=404, details={"sessionId": session_id})
                if not isinstance(next_cursor, str) or next_cursor == cursor:
                    raise GatewayError("runtime_unavailable", "App Server thread/list 分页游标无效", status=502)
                cursor = next_cursor
            else:
                raise GatewayError("runtime_unavailable", "App Server thread/list 超过有界查找页数", status=502, details={"maxPages": self.THREAD_LOOKUP_MAX_PAGES})
            try:
                listed = reader(thread_id, include_turns=False)
            except Exception as exc:
                raise GatewayError("runtime_unavailable", "App Server thread/read 读取失败", status=502, details={"threadId": thread_id, "reason": str(exc)[:1000]}) from exc
            response = listed if isinstance(listed, dict) else None
        if response is None:
            raise GatewayError(
                "thread_not_found",
                "App Server 未找到目标 Session",
                status=404,
                details={"sessionId": session_id},
            )
        thread = response.get("thread") if isinstance(response, dict) and isinstance(response.get("thread"), dict) else response
        if not isinstance(thread, dict):
            raise GatewayError("runtime_unavailable", "App Server thread/read 响应无效", status=502, details={"threadId": thread_id})
        defaults = self._runtime_defaults()
        def value(key: str) -> Any:
            current = thread.get(key)
            if key == "effort" and current in (None, ""):
                current = thread.get("reasoningEffort")
            result = current if current not in (None, "") else defaults[key]
            if result in (None, ""):
                raise GatewayError("runtime_defaults_unavailable", f"App Server thread 缺少 {key}，且服务端未配置默认值", status=422)
            return result
        sandbox = value("sandboxPolicy")
        if isinstance(sandbox, dict):
            sandbox = {"readOnly": "read-only", "workspaceWrite": "workspace-write"}.get(sandbox.get("type"), sandbox.get("type"))
        resolved = {
            "threadId": thread.get("id") if isinstance(thread.get("id"), str) and thread.get("id") else thread_id,
            "cwd": value("cwd"),
            "model": value("model"),
            "effort": value("effort"),
            "approvalPolicy": value("approvalPolicy"),
            "sandboxPolicy": sandbox,
            "runtimeStatus": (thread.get("status") or {}).get("type") if isinstance(thread.get("status"), dict) else thread.get("status", "notLoaded"),
        }
        if resolved["runtimeStatus"] in {"active", "waitingOnApproval"}:
            resolved.update({"controlMode": "external", "requestedControlMode": "external"})
        if thread.get("canAcceptDirectInput") is False:
            resolved.update({
                "enabled": False,
                "unavailableReason": "Codex App Server 标记该 Session 不接受直接输入",
            })
        if not isinstance(resolved["threadId"], str) or not resolved["threadId"] or not isinstance(resolved["cwd"], str) or not Path(resolved["cwd"]).is_absolute() or resolved["cwd"].find("\x00") >= 0:
            raise GatewayError("runtime_unavailable", "App Server 返回的 threadId/cwd 无效", status=502, details={"threadId": thread_id})
        if resolved["effort"] not in {"low", "medium", "high", "xhigh", "max"}:
            raise GatewayError("runtime_unavailable", "App Server 返回的 effort 无效", status=502, details={"effort": resolved["effort"]})
        if resolved["approvalPolicy"] not in {"on-request", "never", "untrusted"} or resolved["sandboxPolicy"] not in {"read-only", "workspace-write"}:
            raise GatewayError("runtime_unavailable", "App Server 返回的运行策略无效", status=502)
        if not isinstance(resolved["runtimeStatus"], str) or not resolved["runtimeStatus"]:
            resolved["runtimeStatus"] = "notLoaded"
        profile = response.get("activePermissionProfile") if isinstance(response, dict) else None
        profile_id = profile.get("id") if isinstance(profile, dict) else None
        requested = {"preset": os.environ.get("BETTER_SUBAGENT_DEFAULT_POLICY_PRESET", "development"), "permissionProfileId": profile_id or os.environ.get("BETTER_SUBAGENT_DEFAULT_PERMISSION_PROFILE", ":workspace"), "approvalPolicy": resolved["approvalPolicy"], "runtimeWorkspaceRoots": [resolved["cwd"]]}
        resolved.update({"requestedPolicy": requested, "effectivePolicy": response.get("effectivePolicy") if isinstance(response, dict) else None, "policySource": "appServer" if profile_id else "default", "activeTurnId": None, "runtimeWorkspaceRoots": [resolved["cwd"]]})
        return resolved

    def _assert_runtime_idle(self, session: dict[str, Any]) -> None:
        reader = getattr(self.transport, "read_thread", None)
        thread_id = session.get("threadId")
        if not callable(reader) or not isinstance(thread_id, str) or not thread_id:
            return
        try:
            response = reader(thread_id, include_turns=False)
        except TransportRejected as exc:
            message = str(exc).lower()
            if message.startswith(("no rollout found for thread id", "thread not loaded", "thread notloaded")):
                return
            raise GatewayError("runtime_unavailable", "App Server thread/read 读取失败", status=502, details={"threadId": thread_id, "reason": str(exc)[:1000]}) from exc
        except Exception as exc:
            raise GatewayError("runtime_unavailable", "App Server thread/read 读取失败", status=502, details={"threadId": thread_id, "reason": str(exc)[:1000]}) from exc
        thread = response.get("thread") if isinstance(response, dict) and isinstance(response.get("thread"), dict) else response
        raw_status = thread.get("status") if isinstance(thread, dict) else None
        status = raw_status.get("type") if isinstance(raw_status, dict) else raw_status
        if status not in {"idle", "notLoaded", None}:
            raise GatewayError("session_not_idle", f"目标 Session 当前 runtime 状态为 {status}", status=409, details={"threadId": thread_id, "runtimeStatus": status})

    def _on_approval_request(self, request_id: str | int, params: dict[str, Any]) -> None:
        with self.store.locked() as board:
            params = dict(params)
            method = params.pop("_requestMethod", None)
            board.setdefault("pendingApprovals", {})[str(request_id)] = {"requestId": request_id, "method": method, "params": params, "createdAt": now_iso()}
            thread_id = params.get("threadId")
            for session in board["sessions"].values():
                if session.get("threadId") == thread_id:
                    session.update({"runtimeStatus": "waitingOnApproval", "updatedAt": now_iso()})
            self.store.save(board)

    def _on_transport_notification(self, message: dict[str, Any]) -> None:
        """Project an unowned active turn as external; never interrupt it."""
        method = message.get("method", "")
        params = message.get("params") or {}
        if method == "serverRequest/resolved":
            request_id = str(params.get("requestId") or params.get("id") or "")
            if request_id:
                with self.store.locked() as board:
                    pending = board.setdefault("pendingApprovals", {}).get(request_id)
                    if isinstance(pending, dict) and pending.get("params", {}).get("threadId") == params.get("threadId"):
                        board["pendingApprovals"].pop(request_id, None)
                        for session in board["sessions"].values():
                            if session.get("runtimeStatus") == "waitingOnApproval" and session.get("threadId") == params.get("threadId"):
                                session.update({"runtimeStatus": "active", "updatedAt": now_iso()})
                    self.store.save(board)
            return
        if method == "turn/completed":
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            thread_id = params.get("threadId")
            if isinstance(turn_id, str) and turn_id and isinstance(thread_id, str) and thread_id:
                with self._runtime_projection_lock:
                    self._recent_terminal_turns.append((thread_id, turn_id))
                board = self.store.read()
                matching_run_ids = [
                    run["gatewayRunId"]
                    for run in board["runs"]
                    if run.get("status") in OCCUPYING_RUN_STATUSES
                    and run.get("transportTurnId") == turn_id
                    and isinstance(board["sessions"].get(run.get("sessionId")), dict)
                    and board["sessions"][run["sessionId"]].get("threadId") == thread_id
                ]
                terminal = self._turn_terminal_fact(turn)
                for gateway_run_id in matching_run_ids:
                    if terminal is None:
                        self._set_unknown(
                            gateway_run_id,
                            f"App Server terminal Turn 状态无法识别: {turn.get('status')}",
                        )
                    else:
                        self._set_terminal(
                            gateway_run_id,
                            terminal[0],
                            terminal[1],
                            execution_established=True,
                        )
            with self.store.locked() as board:
                for session in board["sessions"].values():
                    if (
                        session.get("controlMode") == "external"
                        and session.get("threadId") == thread_id
                        and session.get("activeTurnId") in {None, turn_id}
                    ):
                        session.update({
                            "runtimeStatus": "idle",
                            "activeTurnId": None,
                            "updatedAt": now_iso(),
                        })
                self.store.save(board)
            return
        if method not in {"turn/started", "thread/status/changed", "transport/status"}:
            return
        status = params.get("status") or params.get("threadStatus")
        if isinstance(status, dict):
            status = status.get("type")
        if method == "transport/status":
            if params.get("status") == "unknown":
                with self.store.locked() as board:
                    for session in board["sessions"].values():
                        if session.get("controlMode", "managed") == "managed":
                            session.update({"runtimeStatus": "unknown", "updatedAt": now_iso()})
                    self.store.save(board)
            return
        active = method == "turn/started" or status in {"active", "inProgress", "running"}
        if not active:
            return
        turn = params.get("turn") or {}
        thread = params.get("threadId") or params.get("thread", {}).get("id")
        if not thread:
            return
        with self._live_run_lock:
            live_run_ids = set(self._live_run_ids)
        with self.store.locked() as board:
            for session in board["sessions"].values():
                if session.get("threadId") == thread:
                    turn_id = params.get("turnId") or turn.get("id")
                    managed = any(
                        run.get("sessionId") == session.get("sessionId")
                        and run.get("status") in OCCUPYING_RUN_STATUSES
                        and (
                            not turn_id
                            or run.get("transportTurnId") == turn_id
                            or (
                                run.get("status") == "starting"
                                and not run.get("transportTurnId")
                                and run.get("gatewayRunId") in live_run_ids
                            )
                        )
                        for run in board["runs"]
                    )
                    if managed:
                        continue
                    session.update({"controlMode": "external", "requestedControlMode": "external", "runtimeStatus": "active", "activeTurnId": turn_id, "updatedAt": now_iso()})
            self.store.save(board)

    def list_sessions(self) -> dict[str, Any]:
        board = self.store.read()
        summaries = [session_summary(session, board["runs"], pending_approval_count=len(self._pending_for_session(board, session))) for session in board["sessions"].values()]
        summaries.sort(key=lambda item: (item["role"], item["owner"], item["sessionId"]))
        return {"sessions": summaries}

    def sessions_overview(self) -> dict[str, Any]:
        lister = getattr(self.transport, "list_threads", None)
        if not callable(lister):
            raise GatewayError("transport_unsupported", "当前 transport 不支持 Session overview", status=501)
        try:
            response = lister(limit=100)
        except Exception as exc:
            raise GatewayError(
                "overview_unavailable",
                "Session overview 读取失败",
                status=502,
                details={"reason": str(exc)[:1000]},
            ) from exc
        threads = response.get("data") if isinstance(response, dict) else None
        if not isinstance(threads, list):
            raise GatewayError("overview_unavailable", "App Server thread/list 响应无效", status=502)
        registry = self.store.read().get("sessions", {})
        sessions: list[dict[str, Any]] = []
        seen_thread_ids: set[str] = set()
        for thread in threads:
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str) or not thread["id"]:
                continue
            thread_id = thread["id"]
            if thread_id in seen_thread_ids:
                continue
            seen_thread_ids.add(thread_id)
            session_id = thread_id
            session_root_id = thread.get("sessionId") if isinstance(thread.get("sessionId"), str) else thread_id
            registered = registry.get(session_id)
            if not isinstance(registered, dict):
                registered = next(
                    (
                        item for item in registry.values()
                        if isinstance(item, dict) and item.get("threadId") == thread_id
                    ),
                    None,
                )
            registered = registered if isinstance(registered, dict) else None
            raw_status = thread.get("status")
            runtime_status = raw_status.get("type") if isinstance(raw_status, dict) else raw_status
            if not isinstance(runtime_status, str) or not runtime_status:
                runtime_status = "notLoaded"
            preview = thread.get("preview") if isinstance(thread.get("preview"), str) else ""
            raw_name = thread.get("name") if isinstance(thread.get("name"), str) else ""
            main_work = raw_name.strip() or preview.strip()[:120]
            updated_at = _normalize_app_server_timestamp(thread.get("updatedAt"))
            recency_at = _normalize_app_server_timestamp(thread.get("recencyAt"))
            sessions.append({
                "sessionId": session_id,
                "sessionRootId": session_root_id,
                "threadId": thread_id,
                "name": main_work,
                "mainWork": main_work,
                "preview": preview,
                "status": "active" if runtime_status == "active" else "idle",
                "runtimeStatus": runtime_status,
                "cwd": thread.get("cwd") if isinstance(thread.get("cwd"), str) else "",
                "source": thread.get("source", "unknown"),
                "updatedAt": updated_at,
                "lastActivated": recency_at or updated_at,
                "owner": registered.get("owner", "") if registered else "",
                "role": registered.get("role", "") if registered else "",
                "registered": registered is not None,
                "controlMode": registered.get("controlMode", "managed") if registered else "external",
            })
        if sessions:
            workers = min(self.OVERVIEW_ROUND_WORKERS, len(sessions))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                rounds = executor.map(
                    self._overview_round_count,
                    (item["threadId"] for item in sessions),
                )
                for session, (round_count, exact) in zip(sessions, rounds):
                    session["round"] = round_count
                    session["roundExact"] = exact
        return {"sessions": sessions, "nextCursor": response.get("nextCursor")}

    def _overview_round_count(self, thread_id: str) -> tuple[int | None, bool]:
        turns_lister = getattr(self.transport, "list_thread_turns", None)
        if not callable(turns_lister):
            return None, False
        completed = 0
        scanned = 0
        cursor: str | None = None
        try:
            while scanned < self.OVERVIEW_ROUND_LIMIT:
                page_limit = min(self.OVERVIEW_ROUND_PAGE_SIZE, self.OVERVIEW_ROUND_LIMIT - scanned)
                response = turns_lister(
                    thread_id,
                    cursor=cursor,
                    limit=page_limit,
                    items_view="notLoaded",
                )
                turns = response.get("data") if isinstance(response, dict) else None
                if not isinstance(turns, list):
                    return None, False
                bounded_turns = turns[:page_limit]
                scanned += len(bounded_turns)
                completed += sum(
                    1
                    for turn in bounded_turns
                    if isinstance(turn, dict) and turn.get("status") == "completed"
                )
                next_cursor = response.get("nextCursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    return completed, True
                if scanned >= self.OVERVIEW_ROUND_LIMIT or not bounded_turns:
                    return completed, False
                cursor = next_cursor
        except Exception:
            return None, False
        return completed, False

    def session_recap(self, session_id: str) -> dict[str, Any]:
        registry = self.store.read().get("sessions", {})
        registered = registry.get(session_id)
        thread_id = registered.get("threadId") if isinstance(registered, dict) else None
        preview = ""
        lister = getattr(self.transport, "list_threads", None)
        if callable(lister):
            try:
                response = lister(limit=100)
                threads = response.get("data", []) if isinstance(response, dict) else []
                thread = next(
                    (
                        item for item in threads
                        if isinstance(item, dict)
                        and item.get("id") == (thread_id if isinstance(thread_id, str) else session_id)
                    ),
                    None,
                )
                if thread is None:
                    thread = next(
                        (
                            item for item in threads
                            if isinstance(item, dict) and item.get("sessionId") == session_id
                        ),
                        None,
                    )
                if isinstance(thread, dict):
                    if not isinstance(thread_id, str) or not thread_id:
                        thread_id = thread.get("id")
                    if isinstance(thread.get("preview"), str):
                        preview = thread["preview"]
            except Exception:
                pass
        if not isinstance(thread_id, str) or not thread_id:
            thread_id = session_id
        turns_lister = getattr(self.transport, "list_thread_turns", None)
        if callable(turns_lister):
            cursor: str | None = None
            try:
                for _page in range(2):
                    response = turns_lister(thread_id, cursor=cursor, limit=3)
                    turns = response.get("data") if isinstance(response, dict) else None
                    if not isinstance(turns, list):
                        break
                    for turn in turns:
                        if not isinstance(turn, dict) or turn.get("status") != "completed":
                            continue
                        messages = [
                            item.get("text") for item in turn.get("items", [])
                            if isinstance(item, dict)
                            and item.get("type") == "agentMessage"
                            and isinstance(item.get("text"), str)
                        ]
                        if messages:
                            return {
                                "sessionId": session_id,
                                "recap": messages[-1],
                                "source": "completedTurn",
                                "turnId": turn.get("id"),
                            }
                    cursor = response.get("nextCursor")
                    if not isinstance(cursor, str) or not cursor:
                        break
            except Exception:
                pass
        return {
            "sessionId": session_id,
            "recap": preview,
            "source": "preview" if preview else "empty",
            "turnId": None,
        }

    def session_workspace(self, session_id: str) -> dict[str, Any]:
        board = self.store.read()
        session = board["sessions"].get(session_id)
        if not isinstance(session, dict):
            raise GatewayError("session_not_found", "目标 Session 不存在", status=404)
        thread_id = session.get("threadId")
        cwd = session.get("cwd")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(cwd, str) or not cwd:
            raise GatewayError("workspace_unavailable", "Session 缺少 runtime workspace identity", status=409)
        roots = session.get("runtimeWorkspaceRoots")
        return {
            "sessionId": session_id,
            "threadId": thread_id,
            "cwd": cwd,
            "runtimeWorkspaceRoots": [
                root for root in roots if isinstance(root, str) and root
            ] if isinstance(roots, list) else [cwd],
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        board = self.store.read()
        session = board["sessions"].get(session_id)
        if not isinstance(session, dict):
            raise GatewayError("session_not_found", "目标 Session 不存在", status=404)
        pending = self._pending_for_session(board, session)
        return {
            "session": session_summary(session, board["runs"], pending_approval_count=len(pending)),
            "pendingApprovals": pending,
        }

    @staticmethod
    def _pending_for_session(board: dict[str, Any], session: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for pending in board.get("pendingApprovals", {}).values():
            if not isinstance(pending, dict) or pending.get("params", {}).get("threadId") != session.get("threadId"):
                continue
            params = pending.get("params", {})
            method = pending.get("method")
            available = params.get("availableDecisions") if isinstance(params, dict) else None
            if method in {
                "item/permissions/requestApproval",
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                exposed = _supported_approval_decisions(available)
            else:
                exposed = []
            result.append({"requestId": pending.get("requestId"), "method": method, "sessionId": session.get("sessionId"), "turnId": params.get("turnId"), "availableDecisions": exposed, "status": pending.get("status", "pending")})
        return result

    def handoff(self, session_id: str, payload: Any) -> dict[str, Any]:
        request = validate_session_control(payload)
        with self.store.locked() as board:
            session = board["sessions"].get(session_id)
            if not isinstance(session, dict):
                raise GatewayError("session_not_found", "目标 Session 不存在", status=404, request_id=request["requestId"])
            if session.get("lastHandoffRequestId") == request["requestId"]:
                return self._handoff_response(board, session_id, request["requestId"], idempotent=True, result=session.get("lastHandoffResult"))
            if session.get("handoffRequestId") == request["requestId"] and session.get("handoffStatus") in {"pending", "completed", "failed", "unknown"}:
                return self._handoff_response(board, session_id, request["requestId"], idempotent=True)
            if session.get("handoffStatus") in {"pending", "unknown"}:
                raise GatewayError("handoff_in_progress", "Session 正在处理另一个 handoff", status=409, request_id=request["requestId"])
            if session.get("controlMode", "managed") == "external":
                raise GatewayError("session_external", "Session 已由外部控制", status=409, request_id=request["requestId"])
            active = session.get("runtimeStatus") in {"active", "waitingOnApproval"} and session.get("activeTurnId")
            if not active:
                if session.get("runtimeStatus") != "idle":
                    raise GatewayError("session_not_idle", "只有 idle Session 可以交接", status=409, request_id=request["requestId"])
                result = {"status": "completed", "requestId": request["requestId"]}
                session.update({"controlMode": "external", "requestedControlMode": "external", "handoffStatus": "completed", "handoffRequestId": request["requestId"], "handoffResult": result, "lastHandoffRequestId": request["requestId"], "lastHandoffResult": result, "updatedAt": now_iso()})
                self.store.save(board)
                return {"session": session_summary(session, board["runs"]), "handoff": {"status": "completed", "requestId": request["requestId"]}}
            run = next((item for item in board["runs"] if item.get("sessionId") == session_id and item.get("status") in OCCUPYING_RUN_STATUSES), None)
            if not isinstance(run, dict) or not run.get("transportTurnId") or not isinstance(run.get("processId"), int):
                raise GatewayError("session_not_interruptible", "Session 缺少可交接的 active Run", status=409, request_id=request["requestId"])
            run.update({"handoffRequestId": request["requestId"], "status": "interrupting", "updatedAt": now_iso()})
            session.update({"requestedControlMode": "external", "handoffStatus": "pending", "handoffRequestId": request["requestId"], "updatedAt": now_iso()})
            params = {"transportTurnId": run["transportTurnId"], "processId": run["processId"], "threadId": session.get("threadId")}
            gateway_run_id = run["gatewayRunId"]
            self.store.save(board)
        try:
            self.transport.interrupt_turn(params)
        except TransportOutcomeUnknown as exc:
            with self.store.locked() as board:
                run = self._find_run(board, gateway_run_id)
                session = board["sessions"].get(session_id)
                if run.get("status") not in TERMINAL_RUN_STATUSES:
                    run.update({"status": "unknown", "updatedAt": now_iso(), "error": str(exc)[:1000]})
                if isinstance(session, dict):
                    if run.get("status") not in TERMINAL_RUN_STATUSES:
                        session.update({"runtimeStatus": "unknown", "handoffStatus": "unknown", "updatedAt": now_iso()})
                self.store.save(board)
                if run.get("status") in TERMINAL_RUN_STATUSES:
                    return self._handoff_response(board, session_id, request["requestId"])
            raise GatewayError("handoff_unknown", "Session 交接结果未知，已停止重试", status=502, details={"reason": str(exc)[:1000]}, request_id=request["requestId"]) from exc
        except Exception as exc:
            with self.store.locked() as board:
                run = self._find_run(board, gateway_run_id)
                if run.get("status") in TERMINAL_RUN_STATUSES:
                    return self._handoff_response(board, session_id, request["requestId"])
                if run.get("status") == "interrupting":
                    run.update({"status": "active", "updatedAt": now_iso()})
                session = board["sessions"].get(session_id)
                if isinstance(session, dict):
                    result = {"status": "failed", "requestId": request["requestId"]}
                    session.update({"controlMode": "managed", "requestedControlMode": "managed", "handoffStatus": "failed", "handoffRequestId": request["requestId"], "handoffResult": result, "lastHandoffRequestId": request["requestId"], "lastHandoffResult": result, "updatedAt": now_iso()})
                self.store.save(board)
            raise GatewayError("handoff_failed", "Session 交接打断失败", status=502, details={"reason": str(exc)[:1000]}, request_id=request["requestId"]) from exc
        detail = self.get_session(session_id)
        return detail | {"handoff": {"status": detail["session"].get("handoffStatus", "pending"), "requestId": request["requestId"]}}

    def reclaim(self, session_id: str, payload: Any) -> dict[str, Any]:
        request = validate_session_control(payload)
        with self.store.locked() as board:
            session = board["sessions"].get(session_id)
            if not isinstance(session, dict):
                raise GatewayError("session_not_found", "目标 Session 不存在", status=404, request_id=request["requestId"])
            if session.get("lastReclaimRequestId") == request["requestId"]:
                result = dict(session.get("lastReclaimResult") or {"status": "completed", "requestId": request["requestId"]})
                result["idempotent"] = True
                return {"session": session_summary(session, board["runs"]), "reclaim": result}
            if session.get("controlMode", "managed") != "external":
                raise GatewayError("session_not_external", "只有 external Session 可以 reclaim", status=409, request_id=request["requestId"])
            if session.get("runtimeStatus") != "idle":
                raise GatewayError("session_not_idle", "只有 idle external Session 可以 reclaim", status=409, request_id=request["requestId"])
            result = {"status": "completed", "requestId": request["requestId"]}
            session.update({"controlMode": "managed", "requestedControlMode": "managed", "handoffStatus": None, "handoffRequestId": None, "handoffResult": None, "lastReclaimRequestId": request["requestId"], "lastReclaimResult": result, "updatedAt": now_iso()})
            self.store.save(board)
            return {"session": session_summary(session, board["runs"]), "reclaim": result}

    def put_session(self, session_id: str, payload: Any) -> dict[str, Any]:
        record = validate_session_config(session_id, payload)
        minimal_runtime = "threadId" not in record
        if minimal_runtime:
            record.update(self._resolve_minimal_runtime(session_id))
        with self.store.locked() as board:
            if any(
                run.get("sessionId") == session_id and run.get("status") in OCCUPYING_RUN_STATUSES
                for run in board["runs"]
            ):
                raise GatewayError("session_busy", "运行中的 Session 配置不能修改", status=409)
            previous = board["sessions"].get(session_id)
            if isinstance(previous, dict):
                preserved = ("controlMode", "requestedControlMode", "handoffStatus", "handoffRequestId", "lastHandoffRequestId", "lastHandoffResult", "lastReclaimRequestId", "lastReclaimResult")
                if not minimal_runtime:
                    preserved += ("runtimeStatus", "activeTurnId", "effectivePolicy", "policySource", "policyUpdatedAt")
                for key in preserved:
                    if key in previous:
                        record[key] = previous[key]
                comparable = lambda item: {key: value for key, value in item.items() if key not in {"updatedAt", "runtimeStatus", "activeTurnId", "effectivePolicy", "policyUpdatedAt"}}
                if comparable(previous) == comparable(record) and (not minimal_runtime or previous.get("runtimeStatus") == record.get("runtimeStatus")):
                    return {"session": session_summary(previous, board["runs"]), "idempotent": True}
            if record.get("runtimeStatus") in {"active", "waitingOnApproval"}:
                record.update({"controlMode": "external", "requestedControlMode": "external"})
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
            if session.get("controlMode", "managed") == "external":
                raise GatewayError(
                    "session_external",
                    "目标 Session 当前由 CLI/GUI 控制",
                    status=409,
                    details={"controlMode": "external", "runtimeStatus": session.get("runtimeStatus", "unknown")},
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
            session_snapshot = dict(session)

        # App Server RPC must stay outside the non-reentrant store lock: the
        # reader can deliver a notification whose callback needs this lock.
        self._assert_runtime_idle(session_snapshot)

        with self.store.locked() as board:
            previous = next((run for run in board["runs"] if run.get("requestId") == request["requestId"]), None)
            if previous is not None:
                if previous.get("sessionId") != request["sessionId"] or previous.get("promptHash") != prompt_hash:
                    raise GatewayError("request_id_conflict", "requestId 已绑定到不同的 StartRun", status=409, request_id=request["requestId"])
                return {"run": run_status(previous), "idempotent": True}
            session = board["sessions"].get(request["sessionId"])
            if not isinstance(session, dict):
                raise GatewayError("session_not_found", "目标 Session 不存在", status=404, request_id=request["requestId"])
            if not session.get("enabled", True):
                raise GatewayError("session_unavailable", "目标 Session 当前不可调度", status=409, details={"reason": session.get("unavailableReason", "")}, request_id=request["requestId"])
            if session.get("controlMode", "managed") == "external":
                raise GatewayError("session_external", "目标 Session 当前由 CLI/GUI 控制", status=409, details={"controlMode": "external", "runtimeStatus": session.get("runtimeStatus", "unknown")}, request_id=request["requestId"])
            if session.get("runtimeStatus") in {"active", "waitingOnApproval", "unknown", "systemError"}:
                raise GatewayError("session_not_idle", "目标 Session 在 thread/read 后已不再 idle", status=409, details={"runtimeStatus": session.get("runtimeStatus")}, request_id=request["requestId"])
            occupying = next((run for run in board["runs"] if run.get("sessionId") == request["sessionId"] and run.get("status") in OCCUPYING_RUN_STATUSES), None)
            if occupying is not None:
                raise GatewayError("session_busy", "目标 Session 已有未终结 Run", status=409, details={"gatewayRunId": occupying["gatewayRunId"], "status": occupying["status"]}, request_id=request["requestId"])
            if session.get("threadId") != session_snapshot.get("threadId"):
                raise GatewayError("session_changed", "thread/read 期间 Session 绑定已变化", status=409, request_id=request["requestId"])
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
            runtime["requestedPolicy"] = session.get("requestedPolicy")
            runtime["runtimeWorkspaceRoots"] = session.get("runtimeWorkspaceRoots", [session["cwd"]])

        def worker_result(status: str, error: str | None) -> None:
            self._forget_live_run(gateway_run_id)
            if status == "unknown":
                self._set_unknown(gateway_run_id, error or "Codex SDK worker 状态失真")
            else:
                self._set_terminal(
                    gateway_run_id, status, error, execution_established=True
                )

        self._remember_live_run(gateway_run_id)
        try:
            result = self.transport.start_turn(
                {"gatewayRunId": gateway_run_id, "prompt": request["prompt"], **runtime}, worker_result
            )
        except TransportOutcomeUnknown as exc:
            self._forget_live_run(gateway_run_id)
            return {"run": self._set_unknown(gateway_run_id, str(exc)), "idempotent": False}
        except TransportRejected as exc:
            self._forget_live_run(gateway_run_id)
            self._set_terminal(gateway_run_id, "failed", str(exc))
            raise GatewayError(
                "run_start_failed",
                "Codex Run 启动失败",
                status=502,
                details={"gatewayRunId": gateway_run_id, "reason": str(exc)[:1000]},
                request_id=request["requestId"],
            ) from exc
        except Exception as exc:
            self._forget_live_run(gateway_run_id)
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
            terminal = run["status"] in TERMINAL_RUN_STATUSES or run["status"] == "unknown"
            if run["status"] == "starting" and not terminal:
                run.update({"status": "active", "startedAt": now_iso(), "updatedAt": now_iso()})
            session = board["sessions"].get(run["sessionId"])
            if isinstance(session, dict) and run["status"] not in TERMINAL_RUN_STATUSES | {"unknown"}:
                session.update({"runtimeStatus": "active", "activeTurnId": run.get("transportTurnId"), "updatedAt": now_iso()})
                if result.get("effectivePolicy") is not None:
                    session["effectivePolicy"] = result["effectivePolicy"]
                    session["policyUpdatedAt"] = now_iso()
            self.store.save(board)
            return {"run": run_status(run), "idempotent": False}

    def get_run(self, gateway_run_id: str) -> dict[str, Any]:
        board = self.store.read()
        run = self._find_run(board, gateway_run_id)
        if (
            run.get("status") in OCCUPYING_RUN_STATUSES
            and callable(getattr(self.transport, "list_thread_turns", None))
        ):
            self._schedule_run_reconcile(gateway_run_id)
        session = board["sessions"].get(run.get("sessionId"), {})
        thread_id = session.get("threadId") if isinstance(session, dict) else None
        return {"run": run_status(run, thread_id=thread_id)}

    def history(self, session_id: str) -> dict[str, Any]:
        board = self.store.read()
        session = board["sessions"].get(session_id)
        if not isinstance(session, dict):
            raise GatewayError("session_not_found", "目标 Session 不存在", status=404)
        reader = getattr(self.transport, "read_thread", None)
        if reader is None:
            raise GatewayError("transport_unsupported", "当前 transport 不支持历史读取", status=501)
        thread_id = session["threadId"]
        lister = getattr(self.transport, "list_thread_turns", None)
        try:
            # Codex 0.157 defaults durable local threads to paginated history.
            # Preserve the existing Gateway /history contract: complete turn
            # contents, complete reachable history, and chronological order.
            # Metadata stays on thread/read; turns are hydrated through the
            # dedicated paginated API, which also exists in deployed 0.154.
            if callable(lister):
                history = reader(thread_id, include_turns=False)
                thread = history.get("thread") if isinstance(history, dict) else None
                if not isinstance(thread, dict):
                    raise GatewayError("history_incompatible", "Session history 缺少 thread metadata", status=502)

                turns: list[Any] = []
                cursor: str | None = None
                seen_cursors: set[str] = set()
                while True:
                    page = lister(
                        thread_id,
                        cursor=cursor,
                        limit=100,
                        items_view="full",
                        sort_direction="asc",
                    )
                    if not isinstance(page, dict) or not isinstance(page.get("data", []), list):
                        raise GatewayError("history_incompatible", "Session history turn page 格式无效", status=502)
                    turns.extend(page.get("data", []))
                    next_cursor = page.get("nextCursor")
                    if next_cursor is None:
                        break
                    if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                        raise GatewayError("history_incompatible", "Session history cursor 无效", status=502)
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor

                projected = dict(history)
                projected_thread = dict(thread)
                projected_thread["turns"] = turns
                projected["thread"] = projected_thread
                return {"sessionId": session_id, "history": projected}
            # Compatibility fallback for non-App-Server transports.
            return {"sessionId": session_id, "history": reader(thread_id, include_turns=True)}
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError("history_unavailable", "Session 历史读取失败", status=502, details={"reason": str(exc)[:1000]}) from exc

    def steer_run(self, gateway_run_id: str, payload: Any) -> dict[str, Any]:
        request = validate_steer_run(payload)
        board = self.store.read()
        run = self._find_run(board, gateway_run_id)
        if run.get("status") != "active":
            raise GatewayError("run_not_active", "只有 active Run 可以 steer", status=409)
        steer = getattr(self.transport, "steer_turn", None)
        if steer is None:
            raise GatewayError("transport_unsupported", "当前 transport 不支持 steer", status=501)
        try:
            session = board["sessions"].get(run["sessionId"], {})
            result = steer({"threadId": session.get("threadId"), "expectedTurnId": run.get("transportTurnId"), "prompt": request["prompt"]})
        except Exception as exc:
            raise GatewayError("steer_failed", "Run steer 失败", status=502, details={"reason": str(exc)[:1000]}, request_id=request["requestId"]) from exc
        return {"run": run_status(run), "result": result}

    def approval_decision(self, request_id: str, payload: Any) -> dict[str, Any]:
        request = validate_approval_decision(payload)
        if request_id != request["requestId"]:
            raise GatewayError("validation_error", "路径 requestId 与请求体不一致", status=422)
        responder = getattr(self.transport, "respond_approval", None)
        if responder is None:
            raise GatewayError("transport_unsupported", "当前 transport 不支持审批", status=501)
        try:
            with self.store.locked() as board:
                pending = board.setdefault("pendingApprovals", {}).get(request_id)
                if not isinstance(pending, dict):
                    raise GatewayError("approval_not_found", "审批请求不存在或已解决", status=409, request_id=request_id)
                if pending.get("status") in {"responding", "responseUnknown"}:
                    raise GatewayError("approval_already_responding", "审批响应已发送，等待 App Server 确认", status=409, request_id=request_id)
                is_permissions = pending.get("method") == "item/permissions/requestApproval"
                available = pending.get("params", {}).get("availableDecisions") if isinstance(pending.get("params"), dict) else None
                supported_available = _supported_approval_decisions(available)
                if not is_permissions and isinstance(available, list) and request["decision"] not in supported_available:
                    raise GatewayError("approval_decision_unavailable", "当前审批请求不支持该决定", status=409, details={"availableDecisions": supported_available}, request_id=request_id)
                if is_permissions and request.get("decision") in {"accept", "acceptForSession"} and request.get("grantedPermissions") is None:
                    raise GatewayError("validation_error", "permissions 审批必须提供 grantedPermissions", status=422, request_id=request_id)
                if not is_permissions and request.get("grantedPermissions") is not None:
                    raise GatewayError("validation_error", "grantedPermissions 只适用于 permissions 审批", status=422, request_id=request_id)
                pending.update({"status": "responding", "decision": request["decision"], "requestedAt": now_iso()})
                self.store.save(board)
                pending_method = pending.get("method")
                pending_params = pending.get("params")
            responder(pending.get("requestId", request_id), request["decision"], permissions=request.get("grantedPermissions"), method=pending_method, params=pending_params)
        except GatewayError:
            raise
        except Exception as exc:
            with self.store.locked() as board:
                pending = board.setdefault("pendingApprovals", {}).get(request_id)
                if isinstance(pending, dict):
                    pending.update({"status": "responseUnknown", "error": str(exc)[:1000], "updatedAt": now_iso()})
                    self.store.save(board)
            raise GatewayError("approval_failed", "审批响应失败", status=502, details={"reason": str(exc)[:1000]}, request_id=request_id) from exc
        return {"requestId": request_id, "decision": request["decision"]}

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
            session = board["sessions"].get(run["sessionId"], {})
            params = {"transportTurnId": run["transportTurnId"], "processId": run["processId"], "threadId": session.get("threadId")}
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
        current = self.get_run(gateway_run_id)["run"]
        if current["status"] == "interrupting":
            return {"run": current, "idempotent": False}
        return {"run": current, "idempotent": False}

    def _set_unknown(self, gateway_run_id: str, error: str) -> dict[str, Any]:
        self._forget_live_run(gateway_run_id)
        with self.store.locked() as board:
            run = self._find_run(board, gateway_run_id)
            if run.get("status") not in TERMINAL_RUN_STATUSES:
                run.update({"status": "unknown", "error": error[:1000], "updatedAt": now_iso()})
                self.store.save(board)
            return run_status(run)

    def _handoff_response(self, board: dict[str, Any], session_id: str, request_id: str, *, idempotent: bool = False, result: dict[str, Any] | None = None) -> dict[str, Any]:
        session = board["sessions"].get(session_id, {})
        pending = self._pending_for_session(board, session)
        result = dict(result or session.get("handoffResult") or {"status": session.get("handoffStatus", "pending"), "requestId": request_id})
        result.setdefault("requestId", request_id)
        if idempotent:
            result["idempotent"] = True
        return {"session": session_summary(session, board["runs"], pending_approval_count=len(pending)), "pendingApprovals": pending, "handoff": result}

    def _set_terminal(
        self,
        gateway_run_id: str,
        status: str,
        error: str | None,
        *,
        execution_established: bool = False,
    ) -> dict[str, Any]:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"invalid terminal status: {status}")
        self._forget_live_run(gateway_run_id)
        self._forget_run_reconcile(gateway_run_id)
        with self.store.locked() as board:
            run = self._find_run(board, gateway_run_id)
            timestamp = now_iso()
            changed = False
            if execution_established and not run.get("startedAt"):
                run["startedAt"] = timestamp
                changed = True
            if run.get("status") not in TERMINAL_RUN_STATUSES:
                run.update({"status": status, "updatedAt": timestamp, "terminalAt": timestamp, "error": error})
                changed = True
                session = board["sessions"].get(run["sessionId"])
                if isinstance(session, dict) and session.get("activeTurnId") == run.get("transportTurnId"):
                    if run.get("handoffRequestId") and status == "interrupted":
                        result = {"status": "completed", "requestId": run.get("handoffRequestId")}
                        session.update({"controlMode": "external", "requestedControlMode": "external", "handoffStatus": "completed", "runtimeStatus": "idle", "activeTurnId": None, "handoffResult": result, "lastHandoffRequestId": run.get("handoffRequestId"), "lastHandoffResult": result, "updatedAt": timestamp})
                    elif run.get("handoffRequestId"):
                        result = {"status": "failed", "requestId": run.get("handoffRequestId")}
                        session.update({"controlMode": "managed", "requestedControlMode": "managed", "handoffStatus": "failed", "runtimeStatus": "idle", "activeTurnId": None, "handoffResult": result, "lastHandoffRequestId": run.get("handoffRequestId"), "lastHandoffResult": result, "updatedAt": timestamp})
                    else:
                        session.update({"runtimeStatus": "idle", "activeTurnId": None, "updatedAt": timestamp})
            if changed:
                self.store.save(board)
            return run_status(run)

    @staticmethod
    def _find_run(board: dict[str, Any], gateway_run_id: str) -> dict[str, Any]:
        run = next((item for item in board["runs"] if item.get("gatewayRunId") == gateway_run_id), None)
        if run is None:
            raise GatewayError("run_not_found", "Gateway Run 不存在", status=404)
        return run