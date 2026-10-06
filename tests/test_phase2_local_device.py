from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from better_subagent.gateway import GatewayService
from better_subagent.device_agent import LocalDeviceAgent
from better_subagent.storage import SqliteGatewayStore


class FakeTransport:
    def __init__(self) -> None:
        self.starts = []
        self.runtime_thread_id = None
        self.runtime_status = "idle"

    def read_thread(self, thread_id: str, *, include_turns: bool = True):
        return {
            "thread": {
                "id": self.runtime_thread_id or thread_id,
                "status": {"type": self.runtime_status},
                "cwd": "/tmp/worktree",
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "approvalPolicy": "on-request",
                "sandboxPolicy": "workspace-write",
            }
        }

    def start_turn(self, params, on_terminal):
        self.starts.append(params)
        return {"transportTurnId": "turn-local", "processId": 1}

    def interrupt_turn(self, params):
        return {"status": "terminated"}


class Phase2LocalDeviceTest(unittest.TestCase):
    def test_gateway_registers_local_device_agent_and_freezes_run_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            agent = LocalDeviceAgent(
                transport,
                device_id="dev-local-01",
                agent_id="runtime@dev-local-01",
                environment="dev",
            )
            gateway = GatewayService(
                SqliteGatewayStore(Path(directory) / "runtime.sqlite3"), agent
            )

            gateway.put_session("session-1", {
                "sessionId": "session-1",
                "owner": "coder",
                "role": "coder",
                "threadId": "thread-1",
                "cwd": "/tmp/worktree",
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "approvalPolicy": "on-request",
                "sandboxPolicy": "workspace-write",
            })
            session = gateway.get_session("session-1")["session"]
            self.assertEqual(session["deviceId"], "dev-local-01")
            self.assertEqual(session["agentId"], "runtime@dev-local-01")
            self.assertEqual(session["environment"], "dev")
            self.assertEqual(session["controlGeneration"], 1)

            started = gateway.start_run({
                "requestId": "run-once",
                "sessionId": "session-1",
                "prompt": "do it",
            })["run"]
            self.assertEqual(started["targetDeviceId"], "dev-local-01")
            self.assertEqual(started["targetAgentId"], "runtime@dev-local-01")
            self.assertEqual(started["controlGeneration"], 1)
            self.assertEqual(len(transport.starts), 1)

            self.assertEqual(gateway.list_devices()["devices"][0]["deviceId"], "dev-local-01")
            self.assertEqual(gateway.list_agents()["agents"][0]["agentId"], "runtime@dev-local-01")

    def test_control_generation_changes_when_thread_or_runtime_owner_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            agent = LocalDeviceAgent(
                transport,
                device_id="dev-local-01",
                agent_id="runtime@dev-local-01",
                environment="dev",
            )
            gateway = GatewayService(
                SqliteGatewayStore(Path(directory) / "runtime.sqlite3"), agent
            )
            gateway.put_session("session-1", {
                "sessionId": "session-1",
                "owner": "coder",
                "role": "coder",
                "threadId": "thread-1",
                "cwd": "/tmp/worktree",
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "approvalPolicy": "on-request",
                "sandboxPolicy": "workspace-write",
            })

            transport.runtime_thread_id = "thread-2"
            rebound = gateway.put_session("session-1", {
                "sessionId": "session-1", "owner": "coder", "role": "coder"
            })["session"]
            self.assertEqual(rebound["controlGeneration"], 2)
            self.assertEqual(gateway.store.read()["sessions"]["session-1"]["threadId"], "thread-2")

            transport.runtime_status = "active"
            external = gateway.put_session("session-1", {
                "sessionId": "session-1", "owner": "coder", "role": "coder"
            })["session"]
            self.assertEqual(external["controlMode"], "external")
            self.assertEqual(external["controlGeneration"], 3)

    def test_control_generation_changes_on_idle_handoff_and_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = LocalDeviceAgent(
                FakeTransport(),
                device_id="dev-local-01",
                agent_id="runtime@dev-local-01",
                environment="dev",
            )
            gateway = GatewayService(
                SqliteGatewayStore(Path(directory) / "runtime.sqlite3"), agent
            )
            gateway.put_session("session-1", {
                "sessionId": "session-1",
                "owner": "coder",
                "role": "coder",
                "threadId": "thread-1",
                "cwd": "/tmp/worktree",
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "approvalPolicy": "on-request",
                "sandboxPolicy": "workspace-write",
            })
            with gateway.store.locked() as board:
                board["sessions"]["session-1"]["runtimeStatus"] = "idle"
                gateway.store.save(board)

            handed = gateway.handoff("session-1", {"requestId": "handoff-1"})
            self.assertEqual(handed["session"]["controlGeneration"], 2)
            reclaimed = gateway.reclaim("session-1", {"requestId": "reclaim-1"})
            self.assertEqual(reclaimed["session"]["controlGeneration"], 3)


if __name__ == "__main__":
    unittest.main()
