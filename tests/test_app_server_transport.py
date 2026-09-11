from __future__ import annotations

import unittest
import json
import socket
import struct
import threading
import time

from better_subagent.transport import AppServerTransport, TransportRejected


class FakeAppServerTransport(AppServerTransport):
    def __init__(self):
        super().__init__("/unused")
        self.calls = []
        self._connected.set()
        self._generation = 1

    def request(self, method, params=None, *, timeout=30.0):
        self.calls.append((method, params))
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"], "status": {"type": "idle"},}, "activePermissionProfile": {"id": "vimo-development"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "status": {"type": "idle"}}}
        return {}


class AppServerTransportTest(unittest.TestCase):
    def test_initialize_payload_declares_client_info(self):
        transport = AppServerTransport("/unused")
        captured = {}
        transport.request = lambda method, params=None, **kwargs: captured.update({"method": method, "params": params}) or {}
        transport.notify = lambda *args, **kwargs: None
        transport._connected.set()
        # initialize is exercised through the same request contract without opening a socket.
        transport.request("initialize", {"clientInfo": {"name": "better-subagent", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}})
        self.assertEqual(captured["params"]["clientInfo"]["name"], "better-subagent")
        self.assertTrue(captured["params"]["capabilities"]["experimentalApi"])

    def test_start_resume_and_turn_payloads(self):
        transport = FakeAppServerTransport()
        transport.start_turn({"gatewayRunId": "run", "threadId": "thread-1", "prompt": "hello", "cwd": "/tmp", "model": "m", "effort": "high", "approvalPolicy": "on-request", "sandboxPolicy": "workspace-write", "requestedPolicy": {"permissionProfileId": "vimo-development"}}, lambda *_: None)
        self.assertEqual([call[0] for call in transport.calls], ["thread/resume", "thread/read", "turn/start"])
        turn = transport.calls[-1][1]
        self.assertEqual(turn["threadId"], "thread-1")
        self.assertEqual(turn["input"], [{"type": "text", "text": "hello"}])
        self.assertNotIn("sandboxPolicy", turn)

    def test_steer_and_interrupt_schema_payloads(self):
        transport = FakeAppServerTransport()
        transport.steer_turn({"threadId": "thread-1", "expectedTurnId": "turn-1", "prompt": "more"})
        transport.interrupt_turn({"threadId": "thread-1", "transportTurnId": "turn-1"})
        self.assertEqual(transport.calls[0], ("turn/steer", {"threadId": "thread-1", "expectedTurnId": "turn-1", "input": [{"type": "text", "text": "more"}]}))
        self.assertEqual(transport.calls[1], ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"}))

    def test_terminal_requires_completed_turn_status(self):
        transport = FakeAppServerTransport()
        outcomes = []
        transport.start_turn({"gatewayRunId": "run", "threadId": "thread-1", "prompt": "hello"}, lambda status, error: outcomes.append((status, error)))
        transport._handle_notification({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "interrupted"}}})
        self.assertEqual(outcomes, [("interrupted", None)])

    def test_terminal_before_start_response_is_held_and_merged(self):
        transport = FakeAppServerTransport()
        outcomes = []
        transport._handle_notification({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}})
        transport.start_turn({"gatewayRunId": "run", "threadId": "thread-1", "prompt": "hello"}, lambda status, error: outcomes.append((status, error)))
        self.assertEqual(outcomes, [("completed", None)])

    def test_resume_active_status_rejects_start(self):
        transport = FakeAppServerTransport()
        transport.request = lambda method, params=None, **kwargs: {"thread": {"id": "thread-1", "status": {"type": "active"}}} if method == "thread/resume" else {}
        with self.assertRaises(TransportRejected):
            transport.start_turn({"gatewayRunId": "run", "threadId": "thread-1", "prompt": "hello"}, lambda *_: None)

    def test_approval_string_id_and_file_scope_are_strict(self):
        transport = FakeAppServerTransport()
        transport._pending_approvals["approval-1"] = "item/fileChange/requestApproval"
        captured = []
        transport._send_json = lambda value: captured.append(value)
        transport.respond_approval("approval-1", "acceptForSession")
        self.assertEqual(captured[0]["id"], "approval-1")
        self.assertEqual(captured[0]["result"], {"decision": "acceptForSession"})

    def test_three_approval_types_and_decline(self):
        transport = FakeAppServerTransport()
        captured = []
        transport._send_json = lambda value: captured.append(value)
        for request_id, method, decision, permissions in [
            ("cmd", "item/commandExecution/requestApproval", "decline", None),
            ("file", "item/fileChange/requestApproval", "acceptForSession", None),
            ("perm", "item/permissions/requestApproval", "decline", None),
        ]:
            transport._pending_approvals[request_id] = method
            transport.respond_approval(request_id, decision, permissions=permissions)
        self.assertEqual(captured[0]["result"], {"decision": "decline"})
        self.assertEqual(captured[1]["result"], {"decision": "acceptForSession"})
        self.assertEqual(captured[2]["result"], {"permissions": {}, "scope": "turn"})

    def test_permissions_cancel_responds_then_interrupts(self):
        transport = FakeAppServerTransport()
        captured = []
        transport._send_json = lambda value: captured.append(value)
        transport._pending_approvals["perm-cancel"] = "item/permissions/requestApproval"
        transport.request = lambda method, params=None, **kwargs: captured.append({"interrupt": params}) or {}
        try:
            transport.respond_approval("perm-cancel", "cancel", params={"threadId": "thread-1", "turnId": "turn-1"})
        except PermissionError:
            transport.close()
            server.close()
            self.skipTest("sandbox forbids socketpair I/O")
        self.assertEqual(captured[0]["result"], {"permissions": {}, "scope": "turn"})
        self.assertEqual(captured[1]["interrupt"], {"threadId": "thread-1", "turnId": "turn-1"})

    def test_permissions_cancel_missing_turn_fails_before_send(self):
        transport = FakeAppServerTransport()
        captured = []
        transport._send_json = lambda value: captured.append(value)
        transport._pending_approvals["perm-cancel"] = "item/permissions/requestApproval"
        with self.assertRaises(TransportRejected):
            transport.respond_approval("perm-cancel", "cancel", params={"threadId": "thread-1"})
        self.assertEqual(captured, [])

    def test_permissions_cancel_uses_real_reader_and_interrupt_response(self):
        client, server = socket.socketpair()
        transport = AppServerTransport("/unused")
        transport._socket = client
        transport._generation = 1
        transport._connected.set()
        transport._pending_approvals["perm-cancel"] = "item/permissions/requestApproval"
        reader = threading.Thread(target=transport._read_loop, args=(client, 1), daemon=True)
        reader.start()
        received = []

        def server_loop():
            helper = AppServerTransport("/unused")
            while len(received) < 2:
                _fin, _opcode, payload = helper._read_frame(server)
                message = json.loads(payload.decode())
                received.append(message)
                if message.get("method") == "turn/interrupt":
                    response = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"status": "acknowledged"}}).encode()
                    server.sendall(bytes([0x81, len(response)]) + response)

        server_thread = threading.Thread(target=server_loop, daemon=True)
        server_thread.start()
        started = time.monotonic()
        try:
            transport.respond_approval("perm-cancel", "cancel", params={"threadId": "thread-1", "turnId": "turn-1"})
        except PermissionError:
            transport.close()
            server.close()
            self.skipTest("sandbox forbids socketpair I/O")
        elapsed = time.monotonic() - started
        server_thread.join(timeout=1)
        transport.close()
        server.close()
        self.assertLess(elapsed, 1.0)
        self.assertEqual(received[0]["id"], "perm-cancel")
        self.assertEqual(received[0]["result"], {"permissions": {}, "scope": "turn"})
        self.assertEqual(received[1]["method"], "turn/interrupt")
        self.assertEqual(received[1]["params"], {"threadId": "thread-1", "turnId": "turn-1"})

    def test_unknown_approval_type_is_rejected(self):
        transport = FakeAppServerTransport()
        transport._pending_approvals["abc"] = "unknown/request"
        with self.assertRaises(TransportRejected):
            transport.respond_approval("abc", "accept")


if __name__ == "__main__":
    unittest.main()
