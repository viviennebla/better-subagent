"""Read-only Multi-Agent V2 projections; no live Codex process required."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from better_subagent.codex_agent import build_agent_tree_page, project_collaboration_entry, project_transcript_turn
from better_subagent.contracts import GatewayError
from better_subagent.gateway import GatewayService
from better_subagent.server import GatewayHttpServer
from better_subagent.storage import SqliteGatewayStore


class FakeCodex:
    def __init__(self):
        self.thread_calls = []
        self.item_calls = []
        self.threads = [
            {"id": "child", "sessionId": "root", "parentThreadId": "root",
             "agentRole": "reviewer", "agentNickname": "Lovelace",
             "canAcceptDirectInput": False, "status": {"type": "idle"}},
            {"id": "fork", "sessionId": "fork", "forkedFromId": "root",
             "canAcceptDirectInput": None, "status": {"type": "notLoaded"}},
        ]
        self.item_pages = {
            None: {"data": [
                {"turnId": "turn-1", "startedAtMs": 700, "completedAtMs": 900,
                 "item": {"type": "collabAgentToolCall", "id": "c1", "tool": "sendMessage",
                          "senderThreadId": "root", "receiverThreadIds": ["child"],
                          "prompt": "check the tests", "status": "completed"}},
                {"turnId": "turn-1", "startedAtMs": 600, "completedAtMs": 700,
                 "item": {"type": "collabAgentToolCall", "id": "c2", "tool": "spawnAgent",
                          "senderThreadId": "root", "receiverThreadIds": ["child"],
                          "prompt": None}},
                {"turnId": "turn-1", "startedAtMs": 550, "completedAtMs": 580,
                 "item": {"type": "subAgentActivity", "id": "a1", "kind": "spawned",
                          "agentThreadId": "child", "agentPath": "/root/reviewer"}},
                {"turnId": "turn-1", "startedAtMs": 500, "completedAtMs": 520,
                 "item": {"type": "agentMessage", "id": "msg", "text": "ordinary response"}},
            ], "nextCursor": "older"},
            "older": {"data": [], "nextCursor": None},
        }

    def list_threads(self, *, limit=100, cursor=None):
        self.thread_calls.append((limit, cursor))
        if cursor == "older":
            return {"data": [{"id": "root", "sessionId": "root", "status": {"type": "idle"}}],
                    "nextCursor": None}
        return {"data": list(self.threads)[:limit], "nextCursor": "older"}

    def list_thread_items(self, thread_id, *, cursor=None, limit=50, sort_direction="desc"):
        self.item_calls.append((thread_id, cursor, limit, sort_direction))
        return self.item_pages.get(cursor, {"data": [], "nextCursor": None})


class AgentProjectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.transport = FakeCodex()
        self.gateway = GatewayService(
            SqliteGatewayStore(Path(self.temp.name) / "runtime.sqlite3"), self.transport
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_tree_distinguishes_child_and_fork_with_missing_parent(self):
        result = self.gateway.codex_agent_tree(limit=2)
        self.assertEqual(result["nextCursor"], "older")
        self.assertTrue(result["partial"])
        trees = {t["sessionTreeId"]: t for t in result["trees"]}
        self.assertEqual(trees["root"]["rootThreadIds"], [])
        self.assertEqual(trees["root"]["unresolvedParentThreadIds"], ["root"])
        child = trees["root"]["threads"][0]
        self.assertEqual(child["agentRole"], "reviewer")
        self.assertEqual(child["directInputStatus"], "denied")
        fork = trees["fork"]["threads"][0]
        self.assertEqual(fork["forkedFromId"], "root")
        self.assertIsNone(fork["parentThreadId"])
        self.assertEqual(fork["directInputStatus"], "unknown")
        second = self.gateway.codex_agent_tree(cursor="older", limit=1)
        self.assertEqual(second["trees"][0]["rootThreadIds"], ["root"])
        self.assertTrue(second["partial"])
        self.assertIsNone(second["nextCursor"])
        self.assertEqual(self.transport.thread_calls, [(2, None), (1, "older")])

    def test_communication_only_projects_native_collaboration_items(self):
        page = self.gateway.codex_communications("root", limit=4)
        self.assertEqual(page["scannedItems"], 4)
        self.assertEqual(page["nextCursor"], "older")
        self.assertEqual(self.transport.item_calls[0], ("root", None, 4, "desc"))
        self.assertEqual(len(page["events"]), 3)
        a, b, c = page["events"]
        self.assertEqual((a["messageVisibility"], a["message"]), ("readable", "check the tests"))
        self.assertEqual((b["messageVisibility"], b["message"]), ("unavailable", None))
        self.assertNotIn("encrypted", str(b))
        self.assertEqual(c["messageVisibility"], "notApplicable")
        self.assertEqual(c["agentPath"], "/root/reviewer")
        self.assertEqual(self.gateway.codex_communications("root", cursor="older")["events"], [])

    def test_unknown_payloads_are_not_misrepresented(self):
        self.assertIsNone(project_collaboration_entry({"turnId": "t", "item": {
            "type": "agentMessage", "id": "msg", "text": "response"
        }}))
        self.assertIsNone(project_collaboration_entry({"turnId": "t", "item": {
            "type": "collabAgentToolCall", "prompt": "missing identity"
        }}))
        self.assertEqual(build_agent_tree_page([{"id": "root", "sessionId": "root"},
                                                {"id": "child", "sessionId": "root",
                                                 "parentThreadId": "root"}])[0]["threads"][0]["childrenThreadIds"], ["child"])

    def test_read_unregistered_subagent_transcript_with_native_turn_pagination(self):
        def list_turns(thread_id, *, cursor=None, limit=5, items_view="full", sort_direction="desc"):
            self.assertEqual(thread_id, "child")
            self.assertEqual(items_view, "full")
            self.assertEqual(sort_direction, "desc")
            self.assertEqual(limit, 5)
            self.assertIsNone(cursor)
            return {"data": [{
                "id": "turn-1", "status": "completed", "itemsView": "full",
                "items": [
                    {"id": "u", "type": "userMessage", "content": [{"type": "text", "text": "Please review"}]},
                    {"id": "a", "type": "agentMessage", "text": "Review complete"},
                    {"id": "collab", "type": "collabAgentToolCall", "tool": "sendMessage",
                     "senderThreadId": "child", "receiverThreadIds": ["root"], "prompt": None},
                    {"id": "exec", "type": "commandExecution", "command": "pytest -q",
                     "aggregatedOutput": "SECRET_UNSAFE_TO_COPY", "status": "completed"},
                ],
            }], "nextCursor": "older"}
        self.transport.list_thread_turns = list_turns
        result = self.gateway.codex_thread_transcript("child")
        self.assertEqual(result["threadId"], "child")
        self.assertEqual(result["nextCursor"], "older")
        items = result["turns"][0]["items"]
        self.assertEqual(items[0]["text"], "Please review")
        self.assertEqual(items[1]["text"], "Review complete")
        self.assertEqual(items[2]["visibility"], "unavailable")
        self.assertIsNone(items[2]["text"])
        self.assertEqual(items[3]["text"], "pytest -q")
        self.assertNotIn("SECRET_UNSAFE_TO_COPY", str(result))

    def test_transcript_rejects_invalid_page_and_does_not_expose_reasoning(self):
        turn = {"id": "t", "items": [
            {"id": "r", "type": "reasoning", "content": ["private thoughts"]},
            {"id": "x", "type": "other", "secret": "opaque-data"}
        ]}
        projected = project_transcript_turn(turn)
        self.assertIsNone(projected["items"][0]["text"])
        self.assertNotIn("private thoughts", str(projected))
        self.assertNotIn("opaque-data", str(projected))
        with self.assertRaises(GatewayError):
            self.gateway.codex_thread_transcript("child", limit=11)
        with self.assertRaises(GatewayError):
            self.gateway.codex_thread_transcript("child", cursor="")

    def test_invalid_query_and_cursor_fail_explicitly(self):
        with self.assertRaises(GatewayError) as cm:
            self.gateway.codex_agent_tree(limit=0)
        self.assertEqual(cm.exception.status, 422)
        with self.assertRaises(GatewayError):
            self.gateway.codex_communications("root", cursor="")
        self.transport.item_pages[None] = {"data": [], "nextCursor": ""}
        with self.assertRaises(GatewayError) as cm:
            self.gateway.codex_communications("root")
        self.assertEqual(cm.exception.code, "communication_incompatible")

    def test_http_read_routes_and_query_validation(self):
        server = GatewayHttpServer(("127.0.0.1", 0), self.gateway)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            def get(path):
                try:
                    with urlopen(f"http://127.0.0.1:{server.server_port}" + path, timeout=2) as response:
                        return response.status, json.load(response)
                except HTTPError as exc:
                    return exc.code, json.load(exc)
            status, tree = get("/v1/codex/agent-tree?limit=2")
            self.assertEqual(status, 200)
            self.assertEqual(tree["trees"][0]["sessionTreeId"], "root")
            status, comms = get("/v1/codex/threads/root/communications?limit=5")
            self.assertEqual(status, 200)
            self.assertEqual(comms["events"][1]["messageVisibility"], "unavailable")
            self.assertEqual(get("/v1/codex/agent-tree?limit=bad")[0], 422)
            self.assertEqual(get("/v1/codex/threads/root/communications?cursor=")[0], 422)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
