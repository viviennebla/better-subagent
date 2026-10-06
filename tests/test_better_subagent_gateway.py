from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from better_subagent.contracts import GatewayError
from better_subagent.gateway import GatewayService
from better_subagent.storage import SqliteGatewayStore
from better_subagent.server import GatewayHttpServer
from better_subagent.transport import (
    CodexSdkWorkerTransport,
    TransportOutcomeUnknown,
    TransportRejected,
)


SESSION_ID = "00000000-0000-0000-0000-000000000001"


class FakeTransport:
    def __init__(self) -> None:
        self.starts: list[dict] = []
        self.interrupts: list[dict] = []
        self.callbacks: dict[str, object] = {}
        self.thread_read_calls: list[dict] = []
        self.thread_list_calls: list[dict] = []
        self.turn_list_calls: list[dict] = []
        self.threads: list[dict] = [{
            "id": "thread-coder-1",
            "sessionId": SESSION_ID,
            "name": "Registered session",
            "preview": "registered preview",
            "status": {"type": "idle"},
            "cwd": "/tmp/worktree",
            "source": "appServer",
            "updatedAt": 100,
        }]
        self.turn_pages: dict[str | None, dict] = {None: {"data": [], "nextCursor": None}}

    def start_turn(self, params, on_terminal):
        self.starts.append(params)
        turn_id = f"fake-{len(self.starts)}"
        self.callbacks[turn_id] = on_terminal
        return {"transportTurnId": turn_id, "processId": len(self.starts)}

    def interrupt_turn(self, params):
        self.interrupts.append(params)
        callback = self.callbacks[params["transportTurnId"]]
        callback("interrupted", None)
        return {"status": "terminated"}

    def complete(self, turn_id: str) -> None:
        self.callbacks[turn_id]("completed", None)

    def read_thread(self, thread_id: str, *, include_turns: bool = True):
        self.thread_read_calls.append({"threadId": thread_id, "includeTurns": include_turns})
        return {"thread": {"id": thread_id, "status": {"type": "idle"}, "turns": [] if include_turns else None}}

    def list_threads(self, *, limit: int = 100):
        self.thread_list_calls.append({"limit": limit})
        return {"data": list(self.threads), "nextCursor": None}

    def list_thread_turns(
        self,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int = 3,
        items_view: str = "summary",
        sort_direction: str = "desc",
    ):
        self.turn_list_calls.append({"threadId": thread_id, "cursor": cursor, "limit": limit, "itemsView": items_view, "sortDirection": sort_direction})
        return self.turn_pages.get(cursor, {"data": [], "nextCursor": None})


class BlockingInterruptTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.interrupt_started = threading.Event()
        self.release_interrupt = threading.Event()

    def interrupt_turn(self, params):
        self.interrupts.append(params)
        self.interrupt_started.set()
        if not self.release_interrupt.wait(2):
            raise RuntimeError("test interrupt was not released")
        callback = self.callbacks[params["transportTurnId"]]
        callback("interrupted", None)
        return {"status": "terminated"}


class ImmediateTerminalTransport(FakeTransport):
    def __init__(self, status="completed") -> None:
        super().__init__()
        self.status = status

    def start_turn(self, params, on_terminal):
        turn_id = "fast-1"
        self.callbacks[turn_id] = on_terminal
        error = "synthetic runtime failure" if self.status == "failed" else None
        on_terminal(self.status, error)
        return {"transportTurnId": turn_id, "processId": 1}


class TurnStartedBeforeResponseTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.listeners: list[object] = []

    def add_notification_listener(self, callback) -> None:
        self.listeners.append(callback)

    def start_turn(self, params, on_terminal):
        self.starts.append(params)
        turn_id = "early-turn"
        self.callbacks[turn_id] = on_terminal
        for listener in self.listeners:
            listener({
                "method": "turn/started",
                "params": {
                    "threadId": params["threadId"],
                    "turn": {"id": turn_id, "status": "inProgress"},
                },
            })
        return {"transportTurnId": turn_id, "processId": 1}


class BlockingReconcileTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.reconcile_started = threading.Event()
        self.release_reconcile = threading.Event()
        self.reconcile_calls = 0

    def list_thread_turns(
        self,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int = 3,
        items_view: str = "summary",
    ):
        self.reconcile_calls += 1
        self.reconcile_started.set()
        if not self.release_reconcile.wait(2):
            raise RuntimeError("test reconcile was not released")
        return {"data": [], "nextCursor": None}


class RejectBeforeStartTransport(FakeTransport):
    def start_turn(self, params, on_terminal):
        raise TransportRejected("synthetic pre-start rejection")


class DeferredInterruptTransport(FakeTransport):
    def interrupt_turn(self, params):
        self.interrupts.append(params)
        return {"status": "acknowledged"}


class UnknownInterruptTransport(DeferredInterruptTransport):
    def interrupt_turn(self, params):
        self.interrupts.append(params)
        raise TransportOutcomeUnknown("interrupt outcome unknown")


class CallbackThenRaiseTransport(FakeTransport):
    def interrupt_turn(self, params):
        self.interrupts.append(params)
        self.callbacks[params["transportTurnId"]]("interrupted", None)
        raise RuntimeError("late transport error")


def session_config() -> dict:
    return {
        "sessionId": SESSION_ID,
        "owner": "coder-1",
        "role": "coder",
        "threadId": "thread-coder-1",
        "cwd": "/tmp/worktree",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "approvalPolicy": "on-request",
        "sandboxPolicy": "workspace-write",
        "enabled": True,
        "unavailableReason": "",
    }


class GatewayServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.transport = FakeTransport()
        self.gateway = GatewayService(
            SqliteGatewayStore(Path(self.temporary.name) / "runtime.sqlite3"), self.transport
        )
        self.gateway.put_session(SESSION_ID, session_config())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def seed_orphaned_run(
        self,
        *,
        gateway_run_id: str = "run-orphaned",
        transport_turn_id: str = "turn-managed",
        status: str = "active",
    ) -> None:
        with self.gateway.store.locked() as board:
            timestamp = "2026-09-14T10:00:00+08:00"
            board["runs"].append({
                "gatewayRunId": gateway_run_id,
                "requestId": f"request-{gateway_run_id}",
                "sessionId": SESSION_ID,
                "promptHash": "0" * 64,
                "status": status,
                "transportTurnId": transport_turn_id,
                "processId": 1,
                "createdAt": timestamp,
                "startedAt": timestamp,
                "updatedAt": timestamp,
            })
            board["sessions"][SESSION_ID].update({
                "controlMode": "managed",
                "requestedControlMode": "managed",
                "runtimeStatus": "active",
                "activeTurnId": transport_turn_id,
            })
            self.gateway.store.save(board)

    def wait_for_stored_run_status(
        self, gateway_run_id: str, expected: str
    ) -> dict:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            run = next(
                item
                for item in self.gateway.store.read()["runs"]
                if item["gatewayRunId"] == gateway_run_id
            )
            if run["status"] == expected:
                return run
            time.sleep(0.01)
        raise AssertionError(f"run {gateway_run_id} did not reach {expected}")

    def test_start_is_idempotent_and_runtime_comes_only_from_registry(self) -> None:
        payload = {"requestId": "action-1", "sessionId": SESSION_ID, "prompt": "完整报告"}
        first = self.gateway.start_run(payload)
        second = self.gateway.start_run(payload)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["run"]["gatewayRunId"], second["run"]["gatewayRunId"])
        self.assertEqual(len(self.transport.starts), 1)
        self.assertEqual(self.transport.starts[0]["threadId"], "thread-coder-1")
        self.assertEqual(self.transport.starts[0]["cwd"], "/tmp/worktree")
        self.assertEqual(self.gateway.list_sessions()["sessions"][0]["status"], "busy")

        with self.assertRaisesRegex(GatewayError, "不同的 StartRun"):
            self.gateway.start_run({**payload, "prompt": "被替换的报告"})

    def test_history_pages_full_turns_and_preserves_chronological_contract(self) -> None:
        self.transport.turn_pages[None] = {
            "data": [
                {"id": "turn-1", "status": "completed", "items": [{"type": "agentMessage", "text": "first"}]},
                {"id": "turn-2", "status": "completed", "items": [{"type": "agentMessage", "text": "second"}]},
            ],
            "nextCursor": "page-2",
        }
        self.transport.turn_pages["page-2"] = {
            "data": [
                {"id": "turn-3", "status": "completed", "items": [{"type": "agentMessage", "text": "third"}]},
            ],
            "nextCursor": None,
        }

        result = self.gateway.history(SESSION_ID)

        self.assertEqual(
            self.transport.thread_read_calls[-1],
            {"threadId": "thread-coder-1", "includeTurns": False},
        )
        self.assertEqual(
            self.transport.turn_list_calls,
            [
                {
                    "threadId": "thread-coder-1",
                    "cursor": None,
                    "limit": 100,
                    "itemsView": "full",
                    "sortDirection": "asc",
                },
                {
                    "threadId": "thread-coder-1",
                    "cursor": "page-2",
                    "limit": 100,
                    "itemsView": "full",
                    "sortDirection": "asc",
                },
            ],
        )
        self.assertEqual(
            [turn["id"] for turn in result["history"]["thread"]["turns"]],
            ["turn-1", "turn-2", "turn-3"],
        )
        self.assertEqual(
            [turn["items"][0]["text"] for turn in result["history"]["thread"]["turns"]],
            ["first", "second", "third"],
        )
        self.assertNotIn("turnsPage", result["history"])

    def test_history_rejects_repeated_pagination_cursor(self) -> None:
        self.transport.turn_pages[None] = {"data": [], "nextCursor": "repeat"}
        self.transport.turn_pages["repeat"] = {"data": [], "nextCursor": "repeat"}

        with self.assertRaisesRegex(GatewayError, "cursor"):
            self.gateway.history(SESSION_ID)

    def test_session_workspace_returns_exact_runtime_identity(self) -> None:
        workspace = self.gateway.session_workspace(SESSION_ID)
        self.assertEqual(workspace["sessionId"], SESSION_ID)
        self.assertEqual(workspace["threadId"], "thread-coder-1")
        self.assertEqual(workspace["cwd"], "/tmp/worktree")
        self.assertEqual(workspace["runtimeWorkspaceRoots"], ["/tmp/worktree"])

    def test_session_detail_projects_policy_and_pending_decisions(self) -> None:
        with self.gateway.store.locked() as board:
            board["sessions"][SESSION_ID]["effectivePolicy"] = {"sandboxPolicy": "workspace-write"}
            board["pendingApprovals"] = {"29": {"requestId": 29, "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread-coder-1", "turnId": "turn-1", "availableDecisions": ["accept", {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["touch", "/tmp/example"]}}, "cancel"]}}}
            self.gateway.store.save(board)
        detail = self.gateway.get_session(SESSION_ID)
        self.assertEqual(detail["session"]["requestedPolicy"]["permissionProfileId"], ":workspace")
        self.assertEqual(detail["session"]["effectivePolicy"], {"sandboxPolicy": "workspace-write"})
        self.assertEqual(detail["pendingApprovals"][0]["requestId"], 29)
        self.assertEqual(detail["pendingApprovals"][0]["availableDecisions"], ["accept", "cancel"])
        self.assertEqual(self.gateway.list_sessions()["sessions"][0]["pendingApprovalCount"], 1)

    def test_sessions_overview_merges_registry_and_normalizes_display_status(self) -> None:
        self.transport.threads = [
            {
                "id": SESSION_ID, "sessionId": SESSION_ID, "name": "Primary work",
                "preview": "older prompt", "status": {"type": "active", "activeFlags": []},
                "cwd": "/tmp/worktree", "source": "appServer", "updatedAt": 300,
            },
            {
                "id": "unregistered-idle", "sessionId": "unregistered-idle", "name": None,
                "preview": "Investigate the latest runtime behavior", "status": {"type": "idle"},
                "cwd": "/tmp/idle", "source": "vscode", "updatedAt": 200,
            },
            {
                "id": "unregistered-not-loaded", "sessionId": "unregistered-not-loaded", "name": "",
                "preview": "x" * 160, "status": {"type": "notLoaded"},
                "cwd": "/tmp/stored", "source": "cli", "updatedAt": 100,
            },
        ]
        result = self.gateway.sessions_overview()
        self.assertEqual([item["sessionId"] for item in result["sessions"]], [
            SESSION_ID, "unregistered-idle", "unregistered-not-loaded",
        ])
        registered, idle, not_loaded = result["sessions"]
        self.assertEqual(registered["status"], "active")
        self.assertEqual(registered["runtimeStatus"], "active")
        self.assertEqual(registered["mainWork"], "Primary work")
        self.assertTrue(registered["registered"])
        self.assertEqual(registered["owner"], "coder-1")
        self.assertEqual(registered["role"], "coder")
        self.assertEqual(idle["status"], "idle")
        self.assertFalse(idle["registered"])
        self.assertEqual(idle["controlMode"], "external")
        self.assertEqual(idle["name"], "Investigate the latest runtime behavior")
        self.assertEqual(not_loaded["status"], "idle")
        self.assertEqual(not_loaded["runtimeStatus"], "notLoaded")
        self.assertEqual(len(not_loaded["mainWork"]), 120)

    def test_sessions_overview_deduplicates_thread_id_and_keeps_newest_row(self) -> None:
        self.transport.threads = [
            {
                "id": "thread-duplicate", "sessionId": "session-root", "name": "newest",
                "preview": "new", "status": {"type": "active", "activeFlags": []},
                "cwd": "/tmp/new", "source": "vscode", "updatedAt": 300,
            },
            {
                "id": "thread-other", "sessionId": "thread-other", "name": "other",
                "preview": "other", "status": {"type": "idle"},
                "cwd": "/tmp/other", "source": "cli", "updatedAt": 250,
            },
            {
                "id": "thread-duplicate", "sessionId": "session-root", "name": "stale",
                "preview": "old", "status": {"type": "idle"},
                "cwd": "/tmp/old", "source": "vscode", "updatedAt": 100,
            },
        ]
        sessions = self.gateway.sessions_overview()["sessions"]
        self.assertEqual([item["sessionId"] for item in sessions], ["thread-duplicate", "thread-other"])
        self.assertEqual(sessions[0]["sessionRootId"], "session-root")
        self.assertEqual(sessions[0]["name"], "newest")
        self.assertEqual(sessions[0]["status"], "active")

    def test_sessions_overview_normalizes_app_server_timestamps(self) -> None:
        common = {
            "preview": "",
            "status": {"type": "idle"},
            "cwd": "/tmp",
            "source": "cli",
        }
        self.transport.threads = [
            {
                **common,
                "id": "recency",
                "sessionId": "recency",
                "name": "recency",
                "updatedAt": 1789110000,
                "recencyAt": 1789111217,
            },
            {
                **common,
                "id": "fallback",
                "sessionId": "fallback",
                "name": "fallback",
                "updatedAt": 1789111217,
                "recencyAt": None,
            },
            {
                **common,
                "id": "existing-iso",
                "sessionId": "existing-iso",
                "name": "existing-iso",
                "updatedAt": "2026-09-11T15:20:17+08:00",
                "recencyAt": "invalid",
            },
            {
                **common,
                "id": "invalid",
                "sessionId": "invalid",
                "name": "invalid",
                "updatedAt": "2026-09-11T07:20:17",
                "recencyAt": 1789111217000,
            },
            {
                **common,
                "id": "null",
                "sessionId": "null",
                "name": "null",
                "updatedAt": None,
                "recencyAt": None,
            },
        ]

        sessions = {
            item["sessionId"]: item
            for item in self.gateway.sessions_overview()["sessions"]
        }
        self.assertEqual(sessions["recency"]["updatedAt"], "2026-09-11T07:00:00+00:00")
        self.assertEqual(sessions["recency"]["lastActivated"], "2026-09-11T07:20:17+00:00")
        self.assertEqual(sessions["fallback"]["updatedAt"], "2026-09-11T07:20:17+00:00")
        self.assertEqual(sessions["fallback"]["lastActivated"], "2026-09-11T07:20:17+00:00")
        self.assertEqual(sessions["existing-iso"]["updatedAt"], "2026-09-11T15:20:17+08:00")
        self.assertEqual(sessions["existing-iso"]["lastActivated"], "2026-09-11T15:20:17+08:00")
        self.assertIsNone(sessions["invalid"]["updatedAt"])
        self.assertIsNone(sessions["invalid"]["lastActivated"])
        self.assertIsNone(sessions["null"]["updatedAt"])
        self.assertIsNone(sessions["null"]["lastActivated"])

    def test_sessions_overview_rounds_are_bounded_and_fail_independently(self) -> None:
        class RoundTransport(FakeTransport):
            def list_thread_turns(
                self,
                thread_id,
                *,
                cursor=None,
                limit=3,
                items_view="summary",
            ):
                self.turn_list_calls.append({
                    "threadId": thread_id,
                    "cursor": cursor,
                    "limit": limit,
                    "itemsView": items_view,
                })
                if thread_id == "round-failed":
                    raise RuntimeError("round metadata unavailable")
                if thread_id == "round-zero":
                    return {"data": [{"id": "ignored", "status": "interrupted"}], "nextCursor": None}
                if thread_id == "round-one":
                    return {
                        "data": [
                            {"id": "one", "status": "completed"},
                            {"id": "active", "status": "inProgress"},
                        ],
                        "nextCursor": None,
                    }
                if thread_id == "round-multiple":
                    if cursor is None:
                        return {
                            "data": [
                                {"id": "m1", "status": "completed"},
                                {"id": "m2", "status": "interrupted"},
                                {"id": "m3", "status": "completed"},
                            ],
                            "nextCursor": "more",
                        }
                    return {"data": [{"id": "m4", "status": "completed"}], "nextCursor": None}
                return {
                    "data": [
                        {"id": f"cap-{cursor}-{index}", "status": "completed"}
                        for index in range(limit)
                    ],
                    "nextCursor": str(int(cursor or "0") + 1),
                }

        transport = RoundTransport()
        transport.threads = [
            {
                "id": thread_id, "sessionId": thread_id, "name": thread_id,
                "preview": "", "status": {"type": "idle"}, "cwd": "/tmp",
                "source": "cli", "updatedAt": index * 100,
                **({"recencyAt": 999} if thread_id == "round-zero" else {}),
            }
            for index, thread_id in enumerate((
                "round-zero", "round-one", "round-multiple", "round-capped", "round-failed",
            ), start=1)
        ]
        gateway = GatewayService(self.gateway.store, transport)
        sessions = {item["sessionId"]: item for item in gateway.sessions_overview()["sessions"]}
        self.assertEqual((sessions["round-zero"]["round"], sessions["round-zero"]["roundExact"]), (0, True))
        self.assertEqual((sessions["round-one"]["round"], sessions["round-one"]["roundExact"]), (1, True))
        self.assertEqual((sessions["round-multiple"]["round"], sessions["round-multiple"]["roundExact"]), (3, True))
        self.assertEqual((sessions["round-capped"]["round"], sessions["round-capped"]["roundExact"]), (1000, False))
        self.assertEqual((sessions["round-failed"]["round"], sessions["round-failed"]["roundExact"]), (None, False))
        self.assertEqual(sessions["round-zero"]["lastActivated"], "1970-01-01T00:16:39+00:00")
        self.assertEqual(sessions["round-one"]["lastActivated"], "1970-01-01T00:03:20+00:00")
        self.assertTrue(all(call["itemsView"] == "notLoaded" for call in transport.turn_list_calls))
        capped_calls = [call for call in transport.turn_list_calls if call["threadId"] == "round-capped"]
        self.assertEqual(len(capped_calls), 5)
        self.assertTrue(all(call["limit"] == 200 for call in capped_calls))

    def test_session_recap_skips_active_turn_and_reads_one_more_page(self) -> None:
        self.transport.turn_pages = {
            None: {
                "data": [{"id": "turn-active", "status": "inProgress", "items": []}],
                "nextCursor": "older-page",
            },
            "older-page": {
                "data": [{
                    "id": "turn-complete", "status": "completed",
                    "items": [
                        {"id": "user-1", "type": "userMessage", "content": []},
                        {"id": "agent-1", "type": "agentMessage", "text": "draft"},
                        {"id": "agent-2", "type": "agentMessage", "text": "final result"},
                    ],
                }],
                "nextCursor": "unused-third-page",
            },
        }
        result = self.gateway.session_recap(SESSION_ID)
        self.assertEqual(result, {
            "sessionId": SESSION_ID,
            "recap": "final result",
            "source": "completedTurn",
            "turnId": "turn-complete",
        })
        self.assertEqual(self.transport.turn_list_calls, [
            {"threadId": "thread-coder-1", "cursor": None, "limit": 3, "itemsView": "summary", "sortDirection": "desc"},
            {"threadId": "thread-coder-1", "cursor": "older-page", "limit": 3, "itemsView": "summary", "sortDirection": "desc"},
        ])
        self.assertEqual(self.transport.thread_read_calls, [])

    def test_session_recap_falls_back_to_preview_or_empty(self) -> None:
        class BrokenRecapTransport(FakeTransport):
            def list_thread_turns(self, thread_id, *, cursor=None, limit=3, items_view="summary"):
                raise RuntimeError("turn list unavailable")

        transport = BrokenRecapTransport()
        gateway = GatewayService(self.gateway.store, transport)
        preview = gateway.session_recap(SESSION_ID)
        self.assertEqual(preview["recap"], "registered preview")
        self.assertEqual(preview["source"], "preview")
        transport.threads[0]["preview"] = ""
        empty = gateway.session_recap(SESSION_ID)
        self.assertEqual(empty["recap"], "")
        self.assertEqual(empty["source"], "empty")

    def test_handoff_request_id_is_idempotent_and_unknown_is_fail_closed(self) -> None:
        transport = DeferredInterruptTransport()
        gateway = GatewayService(self.gateway.store, transport)
        gateway.start_run({"requestId": "handoff-start", "sessionId": SESSION_ID, "prompt": "交接"})
        first = gateway.handoff(SESSION_ID, {"requestId": "handoff-1"})
        second = gateway.handoff(SESSION_ID, {"requestId": "handoff-1"})
        self.assertEqual(first["handoff"]["status"], "pending")
        self.assertTrue(second["handoff"]["idempotent"])
        self.assertEqual(len(transport.interrupts), 1)
        with self.assertRaisesRegex(GatewayError, "另一个 handoff"):
            gateway.handoff(SESSION_ID, {"requestId": "handoff-2"})

        unknown_transport = UnknownInterruptTransport()
        unknown_store = SqliteGatewayStore(Path(self.temporary.name) / "unknown.sqlite3")
        unknown_gateway = GatewayService(unknown_store, unknown_transport)
        unknown_gateway.put_session(SESSION_ID, session_config())
        unknown_gateway.start_run({"requestId": "unknown-start", "sessionId": SESSION_ID, "prompt": "未知"})
        with self.assertRaisesRegex(GatewayError, "结果未知"):
            unknown_gateway.handoff(SESSION_ID, {"requestId": "handoff-3"})
        self.assertEqual(unknown_gateway.get_session(SESSION_ID)["session"]["status"], "unknown")
        with self.assertRaisesRegex(GatewayError, "另一个 handoff"):
            unknown_gateway.handoff(SESSION_ID, {"requestId": "handoff-4"})

    def test_handoff_active_waits_for_terminal_then_reclaim(self) -> None:
        transport = DeferredInterruptTransport()
        gateway = GatewayService(self.gateway.store, transport)
        started = gateway.start_run({"requestId": "handoff-start", "sessionId": SESSION_ID, "prompt": "交接"})
        run_id = started["run"]["gatewayRunId"]
        pending = gateway.handoff(SESSION_ID, {"requestId": "handoff-1"})
        self.assertEqual(pending["handoff"]["status"], "pending")
        self.assertEqual(gateway.get_session(SESSION_ID)["session"]["controlMode"], "managed")
        transport.callbacks["fake-1"]("interrupted", None)
        detail = gateway.get_session(SESSION_ID)
        self.assertEqual(detail["session"]["controlMode"], "external")
        self.assertEqual(detail["session"]["runtimeStatus"], "idle")
        reclaimed = gateway.reclaim(SESSION_ID, {"requestId": "reclaim-1"})
        self.assertEqual(reclaimed["session"]["controlMode"], "managed")
        self.assertEqual(gateway.get_run(run_id)["run"]["status"], "interrupted")

    def test_natural_terminal_clears_pending_handoff_without_external_claim(self) -> None:
        transport = DeferredInterruptTransport()
        gateway = GatewayService(self.gateway.store, transport)
        gateway.start_run({"requestId": "natural-start", "sessionId": SESSION_ID, "prompt": "自然终止"})
        gateway.handoff(SESSION_ID, {"requestId": "natural-handoff"})
        transport.callbacks["fake-1"]("completed", None)
        detail = gateway.get_session(SESSION_ID)["session"]
        self.assertEqual(detail["controlMode"], "managed")
        self.assertEqual(detail["requestedControlMode"], "managed")
        self.assertEqual(detail["handoffStatus"], "failed")

    def test_callback_success_wins_over_late_interrupt_exception(self) -> None:
        transport = CallbackThenRaiseTransport()
        gateway = GatewayService(self.gateway.store, transport)
        gateway.start_run({"requestId": "late-start", "sessionId": SESSION_ID, "prompt": "late"})
        result = gateway.handoff(SESSION_ID, {"requestId": "late-handoff"})
        self.assertEqual(result["session"]["controlMode"], "external")
        self.assertEqual(gateway.get_session(SESSION_ID)["session"]["handoffStatus"], "completed")

    def test_registry_put_preserves_authoritative_external_runtime(self) -> None:
        with self.gateway.store.locked() as board:
            board["sessions"][SESSION_ID].update({"controlMode": "external", "runtimeStatus": "idle", "requestedControlMode": "external", "handoffStatus": "completed", "effectivePolicy": {"sandboxPolicy": "read-only"}})
            self.gateway.store.save(board)
        self.gateway.put_session(SESSION_ID, session_config())
        detail = self.gateway.get_session(SESSION_ID)["session"]
        self.assertEqual(detail["controlMode"], "external")
        self.assertEqual(detail["runtimeStatus"], "idle")
        self.assertEqual(detail["effectivePolicy"], {"sandboxPolicy": "read-only"})

    def test_handoff_idle_and_reclaim_active_are_explicit(self) -> None:
        with self.gateway.store.locked() as board:
            board["sessions"][SESSION_ID]["runtimeStatus"] = "idle"
            self.gateway.store.save(board)
        completed = self.gateway.handoff(SESSION_ID, {"requestId": "handoff-idle"})
        self.assertEqual(completed["handoff"]["status"], "completed")
        retry = self.gateway.handoff(SESSION_ID, {"requestId": "handoff-idle"})
        self.assertTrue(retry["handoff"]["idempotent"])
        self.assertEqual(retry["handoff"]["status"], "completed")
        reclaimed = self.gateway.reclaim(SESSION_ID, {"requestId": "reclaim-idle"})
        self.assertEqual(reclaimed["session"]["controlMode"], "managed")
        reclaim_retry = self.gateway.reclaim(SESSION_ID, {"requestId": "reclaim-idle"})
        self.assertTrue(reclaim_retry["reclaim"]["idempotent"])
        self.gateway.put_session(SESSION_ID, session_config())
        late_handoff = self.gateway.handoff(SESSION_ID, {"requestId": "handoff-idle"})
        self.assertTrue(late_handoff["handoff"]["idempotent"])
        self.assertEqual(late_handoff["session"]["controlMode"], "managed")
        with self.gateway.store.locked() as board:
            board["sessions"][SESSION_ID].update({"controlMode": "external", "runtimeStatus": "active", "activeTurnId": "external-turn"})
            self.gateway.store.save(board)
        with self.assertRaisesRegex(GatewayError, "只有 idle"):
            self.gateway.reclaim(SESSION_ID, {"requestId": "reclaim-active"})

    def test_synchronous_terminal_proves_execution_started(self) -> None:
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                transport = ImmediateTerminalTransport(status)
                gateway = GatewayService(self.gateway.store, transport)
                result = gateway.start_run({
                    "requestId": f"fast-{status}",
                    "sessionId": SESSION_ID,
                    "prompt": f"快速终结：{status}",
                })
                self.assertEqual(result["run"]["status"], status)
                self.assertIsNotNone(result["run"]["startedAt"])
                self.assertIsNotNone(result["run"]["terminalAt"])
                self.assertEqual(gateway.list_sessions()["sessions"][0]["status"], "idle")

    def test_pre_start_rejection_has_no_started_at(self) -> None:
        gateway = GatewayService(self.gateway.store, RejectBeforeStartTransport())
        with self.assertRaisesRegex(GatewayError, "Codex Run 启动失败"):
            gateway.start_run({
                "requestId": "pre-start-failure",
                "sessionId": SESSION_ID,
                "prompt": "启动前失败",
            })
        run = gateway.store.read()["runs"][-1]
        self.assertEqual(run["status"], "failed")
        self.assertIsNone(run.get("startedAt"))
        self.assertIsNotNone(run.get("terminalAt"))

    def test_resolved_only_updates_matching_waiting_session(self) -> None:
        second = "00000000-0000-0000-0000-000000000002"
        self.gateway.put_session(second, {**session_config(), "sessionId": second, "threadId": "thread-2"})
        self.gateway._on_approval_request("a", {"threadId": "thread-coder-1", "_requestMethod": "item/commandExecution/requestApproval"})
        self.gateway._on_approval_request("b", {"threadId": "thread-2", "_requestMethod": "item/commandExecution/requestApproval"})
        self.gateway._on_transport_notification({"method": "serverRequest/resolved", "params": {"requestId": "a", "threadId": "thread-coder-1"}})
        board = self.gateway.store.read()
        self.assertEqual(board["sessions"][SESSION_ID]["runtimeStatus"], "active")
        self.assertEqual(board["sessions"][second]["runtimeStatus"], "waitingOnApproval")

    def test_approval_available_decisions_and_resolved_fact(self) -> None:
        class ApprovalTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.responses = []
            def respond_approval(self, *args, **kwargs):
                self.responses.append((args, kwargs))

        transport = ApprovalTransport()
        gateway = GatewayService(self.gateway.store, transport)
        with self.gateway.store.locked() as board:
            board["pendingApprovals"] = {"approval-1": {"requestId": "approval-1", "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread-coder-1", "availableDecisions": ["accept", {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["touch", "/tmp/example"]}}, "cancel"]}}}
            self.gateway.store.save(board)
        with self.assertRaisesRegex(GatewayError, "不支持") as raised:
            gateway.approval_decision("approval-1", {"requestId": "approval-1", "decision": "acceptForSession"})
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(transport.responses, [])
        gateway.approval_decision("approval-1", {"requestId": "approval-1", "decision": "accept"})
        self.assertEqual(len(transport.responses), 1)
        self.assertEqual(gateway.store.read()["pendingApprovals"]["approval-1"]["status"], "responding")
        gateway._on_transport_notification({"method": "serverRequest/resolved", "params": {"requestId": "approval-1", "threadId": "thread-coder-1"}})
        self.assertNotIn("approval-1", gateway.store.read()["pendingApprovals"])

    def test_responding_approval_cannot_be_sent_twice_or_resolved_cross_thread(self) -> None:
        class ApprovalTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.responses = 0
            def respond_approval(self, *args, **kwargs):
                self.responses += 1
        transport = ApprovalTransport()
        gateway = GatewayService(self.gateway.store, transport)
        gateway._on_approval_request("approval-2", {"threadId": "thread-coder-1", "_requestMethod": "item/commandExecution/requestApproval"})
        gateway.approval_decision("approval-2", {"requestId": "approval-2", "decision": "accept"})
        with self.assertRaisesRegex(GatewayError, "已发送"):
            gateway.approval_decision("approval-2", {"requestId": "approval-2", "decision": "accept"})
        self.assertEqual(transport.responses, 1)
        gateway._on_transport_notification({"method": "serverRequest/resolved", "params": {"requestId": "approval-2", "threadId": "thread-other"}})
        self.assertIn("approval-2", gateway.store.read()["pendingApprovals"])

    def test_approval_claim_is_atomic_under_concurrent_requests_and_failure_is_closed(self) -> None:
        class BlockingApprovalTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()
                self.responses = 0
            def respond_approval(self, *args, **kwargs):
                self.responses += 1
                self.entered.set()
                self.release.wait(1)
                raise RuntimeError("synthetic response failure")
        transport = BlockingApprovalTransport()
        gateway = GatewayService(self.gateway.store, transport)
        gateway._on_approval_request("approval-3", {"threadId": "thread-coder-1", "_requestMethod": "item/commandExecution/requestApproval"})
        outcomes = []
        def first():
            try:
                gateway.approval_decision("approval-3", {"requestId": "approval-3", "decision": "accept"})
            except Exception as exc:
                outcomes.append(exc)
        worker = threading.Thread(target=first)
        worker.start()
        self.assertTrue(transport.entered.wait(1))
        with self.assertRaisesRegex(GatewayError, "已发送"):
            gateway.approval_decision("approval-3", {"requestId": "approval-3", "decision": "accept"})
        transport.release.set()
        worker.join(timeout=1)
        self.assertEqual(transport.responses, 1)
        self.assertEqual(gateway.store.read()["pendingApprovals"]["approval-3"]["status"], "responseUnknown")
        self.assertEqual(len(outcomes), 1)

    def test_approval_uses_original_jsonrpc_request_id_type(self) -> None:
        class ApprovalTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.received = []
            def respond_approval(self, *args, **kwargs):
                self.received.append(args[0])
        transport = ApprovalTransport()
        gateway = GatewayService(self.gateway.store, transport)
        gateway._on_approval_request(29, {"threadId": "thread-coder-1", "_requestMethod": "item/commandExecution/requestApproval"})
        gateway.approval_decision("29", {"requestId": "29", "decision": "accept"})
        self.assertEqual(transport.received, [29])

    def test_external_matching_completed_turn_becomes_idle_without_reclaim(self) -> None:
        session = self.gateway.store.read()["sessions"][SESSION_ID]
        with self.gateway.store.locked() as board:
            board["sessions"][SESSION_ID].update({"controlMode": "external", "runtimeStatus": "active", "activeTurnId": "turn-external"})
            self.gateway.store.save(board)
        self.gateway._on_transport_notification({"method": "turn/completed", "params": {"threadId": session["threadId"], "turn": {"id": "turn-external", "status": "completed"}}})
        current = self.gateway.store.read()["sessions"][SESSION_ID]
        self.assertEqual(current["runtimeStatus"], "idle")
        self.assertEqual(current["controlMode"], "external")
        self.assertIsNone(current["activeTurnId"])
        with self.gateway.store.locked() as board:
            board["sessions"][SESSION_ID].update({"runtimeStatus": "active", "activeTurnId": "turn-external"})
            self.gateway.store.save(board)
        self.gateway._on_transport_notification({"method": "turn/completed", "params": {"threadId": session["threadId"], "turn": {"id": "other-turn", "status": "completed"}}})
        current = self.gateway.store.read()["sessions"][SESSION_ID]
        self.assertEqual(current["activeTurnId"], "turn-external")

    def test_startup_reconciles_persisted_run_from_exact_terminal_turn(self) -> None:
        self.seed_orphaned_run()
        self.transport.turn_pages = {
            None: {
                "data": [{"id": "turn-managed", "status": "completed"}],
                "nextCursor": None,
            }
        }

        restarted = GatewayService(self.gateway.store, self.transport)

        self.wait_for_stored_run_status("run-orphaned", "completed")
        run = restarted.get_run("run-orphaned")["run"]
        session = restarted.get_session(SESSION_ID)["session"]
        self.assertEqual(run["status"], "completed")
        self.assertIsNotNone(run["terminalAt"])
        self.assertEqual(session["runtimeStatus"], "idle")
        self.assertIsNone(session["activeTurnId"])

    def test_run_read_reconciles_terminal_and_projects_newer_external_turn(self) -> None:
        self.seed_orphaned_run(status="interrupting")
        self.transport.turn_pages = {
            None: {
                "data": [
                    {"id": "turn-external-new", "status": "inProgress"},
                    {
                        "id": "turn-managed",
                        "status": "failed",
                        "error": {"message": "managed turn failed"},
                    },
                ],
                "nextCursor": None,
            }
        }

        self.gateway.get_run("run-orphaned")
        self.wait_for_stored_run_status("run-orphaned", "failed")
        run = self.gateway.get_run("run-orphaned")["run"]

        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"], "managed turn failed")
        session = self.gateway.get_session(SESSION_ID)["session"]
        self.assertEqual(session["controlMode"], "external")
        self.assertEqual(session["runtimeStatus"], "active")
        self.assertEqual(session["activeTurnId"], "turn-external-new")

    def test_terminal_event_wins_over_earlier_active_inventory_snapshot(self) -> None:
        self.seed_orphaned_run()
        self.transport.turn_pages = {
            None: {
                "data": [
                    {"id": "turn-external-new", "status": "inProgress"},
                    {"id": "turn-managed", "status": "completed"},
                ],
                "nextCursor": None,
            }
        }
        managed_turn, active_snapshot = self.gateway._find_runtime_turn(
            "thread-coder-1", "turn-managed"
        )
        self.gateway._on_transport_notification({
            "method": "turn/completed",
            "params": {
                "threadId": "thread-coder-1",
                "turn": {"id": "turn-external-new", "status": "completed"},
            },
        })
        terminal = self.gateway._turn_terminal_fact(managed_turn)
        self.assertIsNotNone(terminal)
        self.gateway._set_terminal("run-orphaned", terminal[0], terminal[1])

        self.gateway._project_external_turn("thread-coder-1", active_snapshot)

        session = self.gateway.get_session(SESSION_ID)["session"]
        self.assertNotEqual(session["runtimeStatus"], "active")
        self.assertIsNone(session["activeTurnId"])

    def test_reconciliation_without_exact_terminal_evidence_keeps_run_occupying(self) -> None:
        self.seed_orphaned_run()
        self.transport.turn_pages = {
            None: {
                "data": [{"id": "turn-external-new", "status": "inProgress"}],
                "nextCursor": None,
            }
        }

        restarted = GatewayService(self.gateway.store, self.transport)

        deadline = time.monotonic() + 2
        while not self.transport.turn_list_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        calls_after_startup = len(self.transport.turn_list_calls)
        self.assertEqual(restarted.get_run("run-orphaned")["run"]["status"], "active")
        self.assertEqual(len(self.transport.turn_list_calls), calls_after_startup)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            session = restarted.get_session(SESSION_ID)["session"]
            if session["controlMode"] == "external":
                break
            time.sleep(0.01)
        self.assertTrue(session["busy"])
        self.assertEqual(session["controlMode"], "external")
        self.assertEqual(session["activeTurnId"], "turn-external-new")

    def test_reconciliation_is_single_flight_and_retries_after_completion(self) -> None:
        self.seed_orphaned_run()
        transport = BlockingReconcileTransport()
        gateway = GatewayService(self.gateway.store, transport)
        gateway.RUN_RECONCILE_RETRY_SECONDS = 0.05
        self.assertTrue(transport.reconcile_started.wait(1))

        time.sleep(0.07)
        gateway.get_run("run-orphaned")
        self.assertEqual(transport.reconcile_calls, 1)

        transport.release_reconcile.set()
        deadline = time.monotonic() + 1
        while "run-orphaned" in gateway._run_reconcile_inflight and time.monotonic() < deadline:
            time.sleep(0.01)
        gateway.get_run("run-orphaned")
        self.assertEqual(transport.reconcile_calls, 1)

        time.sleep(0.06)
        gateway.get_run("run-orphaned")
        deadline = time.monotonic() + 1
        while transport.reconcile_calls < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(transport.reconcile_calls, 2)
        deadline = time.monotonic() + 1
        while "run-orphaned" in gateway._run_reconcile_inflight and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_global_terminal_event_closes_exact_managed_run_after_callback_loss(self) -> None:
        started = self.gateway.start_run({
            "requestId": "event-terminal",
            "sessionId": SESSION_ID,
            "prompt": "等待全局 terminal 事件",
        })
        self.gateway._on_transport_notification({
            "method": "turn/completed",
            "params": {
                "threadId": "thread-coder-1",
                "turn": {"id": "fake-1", "status": "completed"},
            },
        })

        run = self.gateway.get_run(started["run"]["gatewayRunId"])["run"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.gateway.get_session(SESSION_ID)["session"]["runtimeStatus"], "idle")

    def test_new_external_turn_is_not_hidden_by_stale_managed_run(self) -> None:
        started = self.gateway.start_run({
            "requestId": "stale-managed",
            "sessionId": SESSION_ID,
            "prompt": "旧 managed turn",
        })
        self.gateway._on_transport_notification({
            "method": "turn/started",
            "params": {
                "threadId": "thread-coder-1",
                "turn": {"id": "turn-external-new", "status": "inProgress"},
            },
        })

        run = self.gateway.get_run(started["run"]["gatewayRunId"])["run"]
        self.assertEqual(run["status"], "active")
        session = self.gateway.get_session(SESSION_ID)["session"]
        self.assertEqual(session["controlMode"], "external")
        self.assertEqual(session["runtimeStatus"], "active")
        self.assertEqual(session["activeTurnId"], "turn-external-new")

    def test_managed_turn_started_before_start_response_stays_managed(self) -> None:
        transport = TurnStartedBeforeResponseTransport()
        gateway = GatewayService(self.gateway.store, transport)
        requested_control_mode = gateway.get_session(SESSION_ID)["session"][
            "requestedControlMode"
        ]

        started = gateway.start_run({
            "requestId": "early-start-notification",
            "sessionId": SESSION_ID,
            "prompt": "通知先于 start response",
        })

        self.assertEqual(started["run"]["status"], "active")
        session = gateway.get_session(SESSION_ID)["session"]
        self.assertEqual(session["controlMode"], "managed")
        self.assertEqual(session["requestedControlMode"], requested_control_mode)
        self.assertTrue(session["busy"])
        self.assertEqual(session["activeTurnId"], "early-turn")

    def test_terminal_event_with_wrong_thread_does_not_close_managed_run(self) -> None:
        started = self.gateway.start_run({
            "requestId": "wrong-terminal-thread",
            "sessionId": SESSION_ID,
            "prompt": "校验 terminal identity",
        })
        self.gateway._on_transport_notification({
            "method": "turn/completed",
            "params": {
                "threadId": "other-thread",
                "turn": {"id": "fake-1", "status": "completed"},
            },
        })

        self.assertEqual(
            self.gateway.get_run(started["run"]["gatewayRunId"])["run"]["status"],
            "active",
        )

    def test_duplicate_start_after_coordinator_restart_returns_same_run(self) -> None:
        payload = {"requestId": "restart-duplicate", "sessionId": SESSION_ID, "prompt": "do once"}
        first = self.gateway.start_run(payload)
        self.assertEqual(len(self.transport.starts), 1)

        restarted = GatewayService(
            SqliteGatewayStore(Path(self.temporary.name) / "runtime.sqlite3"),
            self.transport,
        )
        second = restarted.start_run(payload)

        self.assertTrue(second["idempotent"])
        self.assertEqual(second["run"]["gatewayRunId"], first["run"]["gatewayRunId"])
        self.assertEqual(len(self.transport.starts), 1)

    def test_single_active_run_and_terminal_callback_release_session(self) -> None:
        first = self.gateway.start_run({"requestId": "action-1", "sessionId": SESSION_ID, "prompt": "报告一"})
        started_at = first["run"]["startedAt"]
        with self.assertRaisesRegex(GatewayError, "已有未终结 Run"):
            self.gateway.start_run({"requestId": "action-2", "sessionId": SESSION_ID, "prompt": "报告二"})
        self.transport.complete("fake-1")
        completed = self.gateway.get_run(first["run"]["gatewayRunId"])["run"]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["startedAt"], started_at)
        second = self.gateway.start_run({"requestId": "action-2", "sessionId": SESSION_ID, "prompt": "报告二"})
        self.assertEqual(second["run"]["status"], "active")

    def test_interrupt_waits_for_transport_fact_and_is_idempotent(self) -> None:
        started = self.gateway.start_run({"requestId": "action-1", "sessionId": SESSION_ID, "prompt": "报告"})
        run_id = started["run"]["gatewayRunId"]
        first = self.gateway.interrupt_run(run_id, {"requestId": "interrupt-1"})
        second = self.gateway.interrupt_run(run_id, {"requestId": "interrupt-1"})
        self.assertEqual(first["run"]["status"], "interrupted")
        self.assertTrue(second["idempotent"])
        self.assertEqual(len(self.transport.interrupts), 1)

    def test_public_contract_rejects_execution_parameter_injection(self) -> None:
        with self.assertRaisesRegex(GatewayError, "不支持字段"):
            self.gateway.start_run({
                "requestId": "action-1", "sessionId": SESSION_ID, "prompt": "报告", "cwd": "/unsafe"
            })

    def test_concurrent_same_interrupt_request_is_idempotent(self) -> None:
        transport = BlockingInterruptTransport()
        gateway = GatewayService(self.gateway.store, transport)
        started = gateway.start_run({"requestId": "action-1", "sessionId": SESSION_ID, "prompt": "报告"})
        run_id = started["run"]["gatewayRunId"]
        outcomes: list[object] = []

        def first_interrupt() -> None:
            try:
                outcomes.append(gateway.interrupt_run(run_id, {"requestId": "interrupt-1"}))
            except Exception as exc:  # pragma: no cover - asserted below
                outcomes.append(exc)

        worker = threading.Thread(target=first_interrupt)
        worker.start()
        self.assertTrue(transport.interrupt_started.wait(1))
        concurrent = gateway.interrupt_run(run_id, {"requestId": "interrupt-1"})
        self.assertTrue(concurrent["idempotent"])
        self.assertEqual(concurrent["run"]["status"], "interrupting")
        self.assertEqual(len(transport.interrupts), 1)

        transport.release_interrupt.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], dict)
        self.assertEqual(outcomes[0]["run"]["status"], "interrupted")
        terminal_retry = gateway.interrupt_run(run_id, {"requestId": "interrupt-1"})
        self.assertTrue(terminal_retry["idempotent"])
        self.assertEqual(terminal_retry["run"]["status"], "interrupted")
        self.assertEqual(len(transport.interrupts), 1)

class GatewayHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        transport = FakeTransport()
        gateway = GatewayService(SqliteGatewayStore(Path(self.temporary.name) / "runtime.sqlite3"), transport)
        gateway.put_session(SESSION_ID, session_config())
        self.server = GatewayHttpServer(("127.0.0.1", 0), gateway)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path: str, *, method: str = "GET", payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base + path, data=body, method=method, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def test_http_start_status_interrupt_and_error_envelope(self) -> None:
        status, updated = self.request(
            f"/v1/sessions/{SESSION_ID}", method="PUT", payload=session_config()
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["session"]["sessionId"], SESSION_ID)
        status, detail = self.request(f"/v1/sessions/{SESSION_ID}")
        self.assertEqual(status, 200)
        self.assertIn("requestedPolicy", detail["session"])
        self.assertEqual(detail["pendingApprovals"], [])
        status, history = self.request(f"/v1/sessions/{SESSION_ID}/history")
        self.assertEqual(status, 200)
        self.assertEqual(history["sessionId"], SESSION_ID)
        status, error = self.request("/v1/sessions/missing")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"], "session_not_found")
        status, sessions = self.request("/v1/sessions")
        self.assertEqual(status, 200)
        self.assertNotIn("threadId", sessions["sessions"][0])
        status, overview = self.request("/v1/sessions/overview")
        self.assertEqual(status, 200)
        self.assertEqual(overview["sessions"][0]["sessionId"], "thread-coder-1")
        self.assertEqual(overview["sessions"][0]["sessionRootId"], SESSION_ID)
        self.assertTrue(overview["sessions"][0]["registered"])
        self.assertEqual(overview["sessions"][0]["round"], 0)
        self.assertTrue(overview["sessions"][0]["roundExact"])
        self.assertEqual(overview["sessions"][0]["lastActivated"], "1970-01-01T00:01:40+00:00")
        status, recap = self.request(f"/v1/sessions/{SESSION_ID}/recap")
        self.assertEqual(status, 200)
        self.assertEqual(recap["recap"], "registered preview")
        self.assertEqual(recap["source"], "preview")
        status, started = self.request("/v1/runs", method="POST", payload={
            "requestId": "http-action", "sessionId": SESSION_ID, "prompt": "交接"
        })
        self.assertEqual(status, 201)
        run_id = started["run"]["gatewayRunId"]
        self.assertEqual(self.request(f"/v1/runs/{run_id}")[1]["run"]["status"], "active")
        status, interrupted = self.request(
            f"/v1/runs/{run_id}/interrupt", method="POST", payload={"requestId": "http-interrupt"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(interrupted["run"]["status"], "interrupted")
        status, error = self.request("/v1/runs/missing")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"], "run_not_found")
        self.assertIn("details", error)


class CodexSdkWorkerTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required for fake-SDK worker tests")
        self.node_command = (node,)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        if hasattr(self, "temporary"):
            self.temporary.cleanup()

    def write_worker(self, mode: str) -> Path:
        worker = self.root / f"fake-sdk-{mode}.mjs"
        source = r'''
import readline from "node:readline";
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let requestSeen = false;
function finish() {
  input.close();
  setImmediate(() => process.exit(0));
}
input.on("line", (line) => {
  if (!requestSeen) {
    requestSeen = true;
    const request = JSON.parse(line);
    console.log(JSON.stringify({type: "request.echo", request}));
    if ("MODE" === "invalid-json-start") console.log("not-json");
    if ("MODE" === "startup-error-start") {
      console.log(JSON.stringify({type: "dispatch.error", message: "synthetic startup error"}));
    }
    const threadId = "MODE" === "wrong-thread-complete" ? "wrong-thread" : request.threadId;
    console.log(JSON.stringify({type: "thread.started", thread_id: threadId}));
    console.log(JSON.stringify({type: "turn.started"}));
    if ([
      "rapid-complete", "wrong-thread-complete", "invalid-json-start", "startup-error-start"
    ].includes("MODE")) {
      console.log(JSON.stringify({type: "turn.completed"}));
      finish();
    }
    return;
  }
  if (line.trim() === "interrupt") {
    console.log(JSON.stringify({type: "dispatch.interrupted"}));
    finish();
  }
  if (line.trim() === "release-post-authority-error") {
    if ("MODE" === "post-authority-invalid-json") console.log("not-json");
    if ("MODE" === "post-authority-identity") {
      console.log(JSON.stringify({type: "thread.started", thread_id: "wrong-thread"}));
    }
    console.log(JSON.stringify({type: "turn.completed"}));
    finish();
  }
  if (line.trim() === "release-failed") {
    console.log(JSON.stringify({type: "error", error: {message: "synthetic schema failure"}}));
    console.log(JSON.stringify({type: "turn.failed", error: {message: "duplicate terminal"}}));
    finish();
  }
});
'''.replace('"MODE"', json.dumps(mode))
        worker.write_text(source, encoding="utf-8")
        return worker

    def make_gateway(self, mode: str) -> tuple[GatewayService, CodexSdkWorkerTransport]:
        transport = CodexSdkWorkerTransport(
            worker_path=self.write_worker(mode),
            node_command=self.node_command,
            log_dir=self.root / "logs",
            startup_timeout_seconds=2,
        )
        gateway = GatewayService(SqliteGatewayStore(self.root / f"{mode}.sqlite3"), transport)
        gateway.put_session(SESSION_ID, session_config())
        return gateway, transport

    @staticmethod
    def wait_for_status(gateway: GatewayService, run_id: str, expected: str) -> dict:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            run = gateway.get_run(run_id)["run"]
            if run["status"] == expected:
                return run
            time.sleep(0.01)
        raise AssertionError(f"run {run_id} did not reach {expected}")

    def test_fake_sdk_rapid_completion_is_not_misreported_as_start_failure(self) -> None:
        gateway, transport = self.make_gateway("rapid-complete")
        result = gateway.start_run({"requestId": "rapid", "sessionId": SESSION_ID, "prompt": "快速完成"})
        run = self.wait_for_status(gateway, result["run"]["gatewayRunId"], "completed")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(gateway.list_sessions()["sessions"][0]["status"], "idle")
        lines = next((self.root / "logs").glob("*.stdout.log")).read_text(encoding="utf-8").splitlines()
        echoed = next(json.loads(line) for line in lines if json.loads(line).get("type") == "request.echo")
        self.assertNotIn("outputSchema", echoed["request"])
        deadline = time.monotonic() + 2
        while transport._children and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(transport._children, {})

    def test_fake_sdk_active_worker_can_be_interrupted(self) -> None:
        gateway, transport = self.make_gateway("active")
        started = gateway.start_run({"requestId": "active", "sessionId": SESSION_ID, "prompt": "保持运行"})
        self.assertEqual(started["run"]["status"], "active")
        run = gateway.interrupt_run(
            started["run"]["gatewayRunId"], {"requestId": "interrupt-active"}
        )["run"]
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(transport._children, {})

    def test_fake_sdk_error_after_authoritative_start_fails_and_releases_session(self) -> None:
        gateway, transport = self.make_gateway("active-fail")
        started = gateway.start_run({
            "requestId": "active-fail", "sessionId": SESSION_ID, "prompt": "触发严格 Schema 错误"
        })
        self.assertEqual(started["run"]["status"], "active")
        child = next(iter(transport._children.values()))
        child["stdin"].write("release-failed\n")
        child["stdin"].flush()

        run = self.wait_for_status(gateway, started["run"]["gatewayRunId"], "failed")
        self.assertEqual(run["error"], "synthetic schema failure")
        self.assertEqual(gateway.list_sessions()["sessions"][0]["status"], "idle")

    def test_wrong_thread_terminal_stays_unknown_and_keeps_session_busy(self) -> None:
        gateway, transport = self.make_gateway("wrong-thread-complete")
        terminal_callbacks: list[tuple[str, str | None]] = []
        with self.assertRaisesRegex(TransportOutcomeUnknown, "不一致的 thread id"):
            transport.start_turn(
                {
                    "gatewayRunId": "direct-wrong-thread",
                    "prompt": "错误 identity",
                    "threadId": "thread-coder-1",
                    "cwd": "/tmp/worktree",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "approvalPolicy": "on-request",
                    "sandboxPolicy": "workspace-write",
                },
                lambda status, error: terminal_callbacks.append((status, error)),
            )
        self.assertEqual(terminal_callbacks, [])

        result = gateway.start_run({
            "requestId": "wrong-thread", "sessionId": SESSION_ID, "prompt": "错误 identity"
        })
        self.assertEqual(result["run"]["status"], "unknown")
        self.assertIn("不一致的 thread id", result["run"]["error"])
        self.assertEqual(gateway.list_sessions()["sessions"][0]["status"], "busy")
        with self.assertRaisesRegex(GatewayError, "已有未终结 Run"):
            gateway.start_run({"requestId": "next", "sessionId": SESSION_ID, "prompt": "不得启动"})
        logs = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (self.root / "logs").glob("*.stdout.log")
        )
        self.assertIn('"type":"turn.completed"', logs)
        deadline = time.monotonic() + 2
        while transport._children and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(transport._children, {})

    def test_startup_errors_cannot_be_washed_out_by_later_valid_events(self) -> None:
        for mode, expected_error in (
            ("invalid-json-start", "非法事件"),
            ("startup-error-start", "synthetic startup error"),
        ):
            with self.subTest(mode=mode):
                gateway, transport = self.make_gateway(mode)
                terminal_callbacks: list[tuple[str, str | None]] = []
                with self.assertRaisesRegex(TransportOutcomeUnknown, expected_error):
                    transport.start_turn(
                        {
                            "gatewayRunId": f"direct-{mode}",
                            "prompt": "错误后伪装合法启动",
                            "threadId": "thread-coder-1",
                            "cwd": "/tmp/worktree",
                            "model": "gpt-5.6-sol",
                            "effort": "high",
                            "approvalPolicy": "on-request",
                            "sandboxPolicy": "workspace-write",
                        },
                        lambda status, error: terminal_callbacks.append((status, error)),
                    )
                self.assertEqual(terminal_callbacks, [])

                result = gateway.start_run({
                    "requestId": f"request-{mode}",
                    "sessionId": SESSION_ID,
                    "prompt": "错误后伪装合法启动",
                })
                self.assertEqual(result["run"]["status"], "unknown")
                self.assertIn(expected_error, result["run"]["error"])
                self.assertEqual(gateway.list_sessions()["sessions"][0]["status"], "busy")
                self.assertEqual(transport._children, {})

    def test_post_authority_errors_are_persisted_as_unknown_before_worker_exit(self) -> None:
        for mode, expected_error in (
            ("post-authority-invalid-json", "非法事件"),
            ("post-authority-identity", "不一致的 thread id"),
        ):
            with self.subTest(mode=mode):
                gateway, transport = self.make_gateway(mode)
                started = gateway.start_run({
                    "requestId": f"request-{mode}",
                    "sessionId": SESSION_ID,
                    "prompt": "authority 后状态失真",
                })
                self.assertEqual(started["run"]["status"], "active")
                child = next(iter(transport._children.values()))
                child["stdin"].write("release-post-authority-error\n")
                child["stdin"].flush()

                run = self.wait_for_status(
                    gateway, started["run"]["gatewayRunId"], "unknown"
                )
                self.assertIn(expected_error, run["error"])
                deadline = time.monotonic() + 2
                while transport._children and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(transport._children, {})
                self.assertEqual(gateway.list_sessions()["sessions"][0]["status"], "busy")


if __name__ == "__main__":
    unittest.main()