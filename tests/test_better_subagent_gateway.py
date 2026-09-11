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

from better_subagent.contracts import GatewayError, STAGE_REPORT_OUTPUT_SCHEMA
from better_subagent.gateway import GatewayService, JsonGatewayStore
from better_subagent.server import GatewayHttpServer
from better_subagent.transport import CodexSdkWorkerTransport, TransportOutcomeUnknown


SESSION_ID = "00000000-0000-0000-0000-000000000001"


class FakeTransport:
    def __init__(self) -> None:
        self.starts: list[dict] = []
        self.interrupts: list[dict] = []
        self.callbacks: dict[str, object] = {}

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
    def start_turn(self, params, on_terminal):
        turn_id = "fast-1"
        self.callbacks[turn_id] = on_terminal
        on_terminal("completed", None)
        return {"transportTurnId": turn_id, "processId": 1}


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
            JsonGatewayStore(Path(self.temporary.name) / "gateway.json"), self.transport
        )
        self.gateway.put_session(SESSION_ID, session_config())

    def tearDown(self) -> None:
        self.temporary.cleanup()

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

    def test_fast_terminal_does_not_reactivate_session(self) -> None:
        transport = ImmediateTerminalTransport()
        gateway = GatewayService(self.gateway.store, transport)
        result = gateway.start_run({"requestId": "fast", "sessionId": SESSION_ID, "prompt": "快速完成"})
        self.assertEqual(result["run"]["status"], "completed")
        self.assertEqual(gateway.list_sessions()["sessions"][0]["status"], "idle")

    def test_resolved_only_updates_matching_waiting_session(self) -> None:
        second = "00000000-0000-0000-0000-000000000002"
        self.gateway.put_session(second, {**session_config(), "sessionId": second, "threadId": "thread-2"})
        self.gateway._on_approval_request("a", {"threadId": "thread-coder-1", "_requestMethod": "item/commandExecution/requestApproval"})
        self.gateway._on_approval_request("b", {"threadId": "thread-2", "_requestMethod": "item/commandExecution/requestApproval"})
        self.gateway._on_transport_notification({"method": "serverRequest/resolved", "params": {"requestId": "a", "threadId": "thread-coder-1"}})
        board = self.gateway.store.read()
        self.assertEqual(board["sessions"][SESSION_ID]["runtimeStatus"], "active")
        self.assertEqual(board["sessions"][second]["runtimeStatus"], "waitingOnApproval")

    def test_single_active_run_and_terminal_callback_release_session(self) -> None:
        first = self.gateway.start_run({"requestId": "action-1", "sessionId": SESSION_ID, "prompt": "报告一"})
        with self.assertRaisesRegex(GatewayError, "已有未终结 Run"):
            self.gateway.start_run({"requestId": "action-2", "sessionId": SESSION_ID, "prompt": "报告二"})
        self.transport.complete("fake-1")
        self.assertEqual(self.gateway.get_run(first["run"]["gatewayRunId"])["run"]["status"], "completed")
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

    def test_production_output_schema_is_strict_recursively(self) -> None:
        def assert_strict(node: object, path: str = "$") -> None:
            if isinstance(node, dict):
                self.assertNotIn("oneOf", node, path)
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, path)
                    properties = node.get("properties")
                    required = node.get("required")
                    self.assertIsInstance(properties, dict, path)
                    self.assertIsInstance(required, list, path)
                    self.assertEqual(set(properties), set(required), path)
                for key, value in node.items():
                    assert_strict(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    assert_strict(value, f"{path}[{index}]")

        assert_strict(STAGE_REPORT_OUTPUT_SCHEMA)
        self.assertNotIn("reportedStage", STAGE_REPORT_OUTPUT_SCHEMA["properties"])
        self.assertNotIn("transitionSuggestion", STAGE_REPORT_OUTPUT_SCHEMA["properties"])


class GatewayHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        transport = FakeTransport()
        gateway = GatewayService(JsonGatewayStore(Path(self.temporary.name) / "gateway.json"), transport)
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
        status, sessions = self.request("/v1/sessions")
        self.assertEqual(status, 200)
        self.assertNotIn("threadId", sessions["sessions"][0])
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
        gateway = GatewayService(JsonGatewayStore(self.root / f"{mode}.json"), transport)
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
        self.assertEqual(echoed["request"]["outputSchema"], STAGE_REPORT_OUTPUT_SCHEMA)
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
