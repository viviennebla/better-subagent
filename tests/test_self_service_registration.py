from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from better_subagent.contracts import GatewayError, validate_start_run
from better_subagent.gateway import GatewayService, JsonGatewayStore
from better_subagent.transport import TransportRejected


SESSION_ID = "11111111-1111-4111-8111-111111111111"


class RuntimeTransport:
    def __init__(self, *, include_runtime: bool = True, status: str = "idle", pages: list[dict] | None = None, reasoning_effort_only: bool = False) -> None:
        self.include_runtime = include_runtime
        self.status = status
        self.pages = pages
        self.reasoning_effort_only = reasoning_effort_only
        self.list_calls: list[dict] = []
        self.read_calls: list[dict] = []

    def list_threads(self, *, cursor: str | None = None, limit: int = 100):
        self.list_calls.append({"cursor": cursor, "limit": limit})
        if self.pages is not None:
            return self.pages[0] if cursor is None else next(page for page in self.pages if page.get("cursor") == cursor)
        return {"data": [{"id": "thread-real", "sessionId": SESSION_ID}], "nextCursor": None}

    def read_thread(self, thread_id: str, *, include_turns: bool = True):
        self.read_calls.append({"threadId": thread_id, "includeTurns": include_turns})
        if self.pages is not None and thread_id == SESSION_ID:
            raise TransportRejected(f"no rollout found for thread id {thread_id}")
        thread = {"id": "thread-real", "status": {"type": self.status}}
        if self.include_runtime:
            effort = {"reasoningEffort": "medium"} if self.reasoning_effort_only else {"effort": "medium"}
            thread.update({"cwd": "/srv/real-worktree", "model": "gpt-5.6-luna", **effort, "approvalPolicy": "on-request", "sandboxPolicy": "workspace-write"})
        return {"thread": thread, "activePermissionProfile": {"id": ":workspace"}}

    def start_turn(self, params, on_terminal):
        return {"transportTurnId": "gateway-turn-1"}


class NotificationBeforeResponseTransport(RuntimeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.gateway = None
        self.notify = False

    def read_thread(self, thread_id: str, *, include_turns: bool = True):
        if self.notify and self.gateway is not None:
            self.gateway._on_transport_notification({"method": "turn/started", "params": {"threadId": thread_id, "turnId": "external-turn"}})
        return super().read_thread(thread_id, include_turns=include_turns)


class SelfServiceRegistrationTest(unittest.TestCase):
    def test_minimal_registration_reads_thread_runtime_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = RuntimeTransport()
            gateway = GatewayService(JsonGatewayStore(Path(directory) / "gateway.json"), transport)
            payload = {"sessionId": SESSION_ID, "owner": "validator", "name": "Validation", "role": "validator"}
            first = gateway.put_session(SESSION_ID, payload)
            second = gateway.put_session(SESSION_ID, payload)
            stored = gateway.store.read()["sessions"][SESSION_ID]
            self.assertTrue(second["idempotent"])
            self.assertEqual(stored["threadId"], "thread-real")
            self.assertEqual(stored["cwd"], "/srv/real-worktree")
            self.assertEqual(stored["model"], "gpt-5.6-luna")
            self.assertEqual(stored["effort"], "medium")
            self.assertEqual(stored["requestedPolicy"]["permissionProfileId"], ":workspace")
            self.assertEqual(transport.read_calls[0], {"threadId": SESSION_ID, "includeTurns": False})
            self.assertEqual(transport.list_calls, [])
            self.assertEqual(first["session"]["name"], "Validation")

    def test_subagent_exact_read_registers_but_is_not_schedulable(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = RuntimeTransport()
            original_read = transport.read_thread

            def read_subagent(thread_id: str, *, include_turns: bool = True):
                result = original_read(thread_id, include_turns=include_turns)
                result["thread"]["canAcceptDirectInput"] = False
                return result

            transport.read_thread = read_subagent
            gateway = GatewayService(JsonGatewayStore(Path(directory) / "gateway.json"), transport)
            gateway.put_session(SESSION_ID, {"sessionId": SESSION_ID, "owner": "reviewer", "role": "reviewer"})
            summary = gateway.list_sessions()["sessions"][0]
            self.assertEqual(summary["status"], "unavailable")
            self.assertIn("不接受直接输入", summary["unavailableReason"])
            with self.assertRaises(GatewayError) as raised:
                gateway.start_run({"requestId": "subagent-start", "sessionId": SESSION_ID, "prompt": "不得启动"})
            self.assertEqual(raised.exception.code, "session_unavailable")

    def test_active_registration_is_external_and_busy_and_start_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = RuntimeTransport(status="active")
            gateway = GatewayService(JsonGatewayStore(Path(directory) / "gateway.json"), transport)
            gateway.put_session(SESSION_ID, {"sessionId": SESSION_ID, "owner": "validator", "role": "validator"})
            summary = gateway.list_sessions()["sessions"][0]
            self.assertEqual(summary["controlMode"], "external")
            self.assertEqual(summary["status"], "external")
            self.assertTrue(summary["busy"])
            with self.assertRaisesRegex(GatewayError, "CLI/GUI|external"):
                gateway.start_run({"requestId": "active-start", "sessionId": SESSION_ID, "prompt": "不得启动"})
            self.assertEqual(gateway.store.read()["runs"], [])

    def test_minimal_registration_accepts_app_server_reasoning_effort(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = RuntimeTransport(reasoning_effort_only=True)
            gateway = GatewayService(JsonGatewayStore(Path(directory) / "gateway.json"), transport)
            gateway.put_session(SESSION_ID, {"sessionId": SESSION_ID, "owner": "validator", "role": "validator"})
            self.assertEqual(gateway.store.read()["sessions"][SESSION_ID]["effort"], "medium")

    def test_start_rechecks_real_thread_status_before_creating_run(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = RuntimeTransport()
            gateway = GatewayService(JsonGatewayStore(Path(directory) / "gateway.json"), transport)
            gateway.put_session(SESSION_ID, {"sessionId": SESSION_ID, "owner": "validator", "role": "validator"})
            transport.status = "active"
            with self.assertRaisesRegex(GatewayError, "runtime 状态"):
                gateway.start_run({"requestId": "runtime-active-start", "sessionId": SESSION_ID, "prompt": "不得启动"})
            self.assertEqual(gateway.store.read()["runs"], [])

    def test_external_active_terminal_idle_reclaim_and_dispatch_converge(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = RuntimeTransport(status="active")
            gateway = GatewayService(JsonGatewayStore(Path(directory) / "gateway.json"), transport)
            gateway.put_session(SESSION_ID, {"sessionId": SESSION_ID, "owner": "validator", "role": "validator"})
            gateway._on_transport_notification({"method": "turn/completed", "params": {"threadId": "thread-real", "turn": {"id": "unknown-external-turn"}}})
            self.assertEqual(gateway.list_sessions()["sessions"][0]["runtimeStatus"], "idle")
            reclaimed = gateway.reclaim(SESSION_ID, {"requestId": "reclaim-after-terminal"})
            self.assertEqual(reclaimed["session"]["controlMode"], "managed")
            transport.status = "idle"
            started = gateway.start_run({"requestId": "dispatch-after-reclaim", "sessionId": SESSION_ID, "prompt": "可以启动"})
            self.assertEqual(started["run"]["status"], "active")

    def test_notification_before_thread_read_response_does_not_invert_store_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = NotificationBeforeResponseTransport()
            gateway = GatewayService(JsonGatewayStore(Path(directory) / "gateway.json"), transport)
            gateway.put_session(SESSION_ID, {"sessionId": SESSION_ID, "owner": "validator", "role": "validator"})
            transport.gateway = gateway
            transport.notify = True
            with self.assertRaisesRegex(GatewayError, "CLI/GUI|external"):
                gateway.start_run({"requestId": "notification-before-read", "sessionId": SESSION_ID, "prompt": "不得启动"})
            self.assertEqual(gateway.store.read()["runs"], [])

    def test_thread_list_finds_session_on_bounded_second_page(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = RuntimeTransport(pages=[
                {"data": [{"id": "other-thread", "sessionId": "other"}], "nextCursor": "page-2"},
                {"cursor": "page-2", "data": [{"id": "thread-real", "sessionId": SESSION_ID}], "nextCursor": None},
            ])
            gateway = GatewayService(JsonGatewayStore(Path(directory) / "gateway.json"), transport)
            gateway.put_session(SESSION_ID, {"sessionId": SESSION_ID, "owner": "validator", "role": "validator"})
            self.assertEqual(gateway.store.read()["sessions"][SESSION_ID]["threadId"], "thread-real")
            self.assertEqual(transport.read_calls[0], {"threadId": SESSION_ID, "includeTurns": False})
            self.assertEqual(transport.list_calls, [{"cursor": None, "limit": 100}, {"cursor": "page-2", "limit": 100}])

    def test_thread_list_not_found_and_page_limit_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            not_found = RuntimeTransport(pages=[{"data": [], "nextCursor": None}])
            gateway = GatewayService(JsonGatewayStore(Path(directory) / "not-found.json"), not_found)
            with self.assertRaisesRegex(GatewayError, "未找到"):
                gateway.put_session(SESSION_ID, {"sessionId": SESSION_ID, "owner": "validator", "role": "validator"})
            pages = [{"data": [], "nextCursor": f"page-{index + 1}"} for index in range(gateway.THREAD_LOOKUP_MAX_PAGES)]
            for index in range(1, len(pages)):
                pages[index]["cursor"] = f"page-{index}"
            limited = RuntimeTransport(pages=pages)
            limited_gateway = GatewayService(JsonGatewayStore(Path(directory) / "limited.json"), limited)
            with self.assertRaisesRegex(GatewayError, "有界查找"):
                limited_gateway.put_session(SESSION_ID, {"sessionId": SESSION_ID, "owner": "validator", "role": "validator"})

    def test_missing_model_or_effort_requires_configured_server_default(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=False):
            for key in ("BETTER_SUBAGENT_DEFAULT_MODEL", "BETTER_SUBAGENT_DEFAULT_EFFORT", "CODEX_MODEL", "CODEX_EFFORT"):
                os.environ.pop(key, None)
            gateway = GatewayService(JsonGatewayStore(Path(directory) / "gateway.json"), RuntimeTransport(include_runtime=False))
            with self.assertRaises(GatewayError) as raised:
                gateway.put_session(SESSION_ID, {"sessionId": SESSION_ID, "owner": "validator", "role": "validator"})
            self.assertEqual(raised.exception.code, "runtime_defaults_unavailable")

    def test_final_prompt_keeps_whitespace(self):
        prompt = "  final prompt\n  "
        self.assertEqual(validate_start_run({"requestId": "r", "sessionId": SESSION_ID, "prompt": prompt})["prompt"], prompt)


if __name__ == "__main__":
    unittest.main()
