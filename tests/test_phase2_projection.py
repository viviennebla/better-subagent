import tempfile
import unittest
from pathlib import Path

from better_subagent.gateway import GatewayService
from better_subagent.storage import SqliteGatewayStore


class Transport:
    def start_turn(self, params, on_terminal):
        return {"transportTurnId": "actual-turn-1", "processId": 7}


class Phase2ProjectionTest(unittest.TestCase):
    def test_run_read_exposes_actual_transport_turn_and_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteGatewayStore(Path(directory) / "runtime.sqlite3")
            gateway = GatewayService(store, Transport())
            gateway.put_session("session-1", {
                "sessionId": "session-1", "owner": "coder", "role": "coder", "threadId": "thread-1",
                "cwd": "/tmp/worktree", "model": "gpt-5", "effort": "high",
                "approvalPolicy": "on-request", "sandboxPolicy": "workspace-write",
            })
            with store.locked() as board:
                board["runs"].append({"gatewayRunId": "run-1", "requestId": "req-1", "sessionId": "session-1",
                                      "status": "active", "transportTurnId": "actual-turn-1",
                                      "createdAt": "2026-09-18T00:00:00+00:00", "updatedAt": "2026-09-18T00:00:00+00:00"})
                store.save(board)
            run = gateway.get_run("run-1")["run"]
            self.assertEqual(run["threadId"], "thread-1")
            self.assertEqual(run["transportTurnId"], "actual-turn-1")

    def test_unknown_transport_turn_is_null_not_gateway_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteGatewayStore(Path(directory) / "runtime.sqlite3")
            gateway = GatewayService(store, Transport())
            with store.locked() as board:
                board["sessions"]["session-1"] = {"sessionId": "session-1", "threadId": "thread-1"}
                board["runs"].append({"gatewayRunId": "run-alias", "requestId": "req-1", "sessionId": "session-1",
                                      "status": "completed", "createdAt": "2026-09-18T00:00:00+00:00", "updatedAt": "2026-09-18T00:00:00+00:00"})
                store.save(board)
            run = gateway.get_run("run-alias")["run"]
            self.assertIsNone(run["transportTurnId"])
            self.assertNotEqual(run["transportTurnId"], run["gatewayRunId"])


if __name__ == "__main__":
    unittest.main()