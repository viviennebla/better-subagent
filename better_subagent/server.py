"""Minimal HTTP server for the better-subagent C1 Gateway contract."""

from __future__ import annotations

import argparse
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .contracts import GatewayError
from .gateway import GatewayService, JsonGatewayStore
from .transport import AppServerTransport, CodexSdkWorkerTransport


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "better-subagent/0.1"

    @property
    def gateway(self) -> GatewayService:
        return self.server.gateway  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._json(HTTPStatus.OK, {"ok": True, "service": "better-subagent"})
            return
        if path == "/v1/sessions":
            self._handle(lambda _payload: self.gateway.list_sessions(), HTTPStatus.OK, body=False)
            return
        prefix = "/v1/runs/"
        if path.startswith(prefix) and len(path) > len(prefix) and "/" not in path[len(prefix):]:
            run_id = unquote(path[len(prefix):])
            self._handle(lambda _payload: self.gateway.get_run(run_id), HTTPStatus.OK, body=False)
            return
        session_prefix = "/v1/sessions/"
        if path.startswith(session_prefix) and path.endswith("/history"):
            session_id = unquote(path[len(session_prefix):-len("/history")].rstrip("/"))
            self._handle(lambda _payload: self.gateway.history(session_id), HTTPStatus.OK, body=False)
            return
        self._json(HTTPStatus.NOT_FOUND, GatewayError("not_found", "接口不存在", status=404).to_dict())

    def do_PUT(self) -> None:
        prefix = "/v1/sessions/"
        path = urlparse(self.path).path
        if path.startswith(prefix) and len(path) > len(prefix):
            session_id = unquote(path[len(prefix):])
            self._handle(lambda payload: self.gateway.put_session(session_id, payload), HTTPStatus.OK)
            return
        self._json(HTTPStatus.NOT_FOUND, GatewayError("not_found", "接口不存在", status=404).to_dict())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/runs":
            self._handle(lambda payload: self.gateway.start_run(payload), HTTPStatus.CREATED)
            return
        prefix = "/v1/runs/"
        suffix = "/interrupt"
        if path.startswith(prefix) and path.endswith(suffix):
            run_id = unquote(path[len(prefix):-len(suffix)].rstrip("/"))
            self._handle(lambda payload: self.gateway.interrupt_run(run_id, payload), HTTPStatus.OK)
            return
        steer_suffix = "/steer"
        if path.startswith(prefix) and path.endswith(steer_suffix):
            run_id = unquote(path[len(prefix):-len(steer_suffix)].rstrip("/"))
            self._handle(lambda payload: self.gateway.steer_run(run_id, payload), HTTPStatus.OK)
            return
        approval_prefix = "/v1/approvals/"
        approval_suffix = "/decision"
        if path.startswith(approval_prefix) and path.endswith(approval_suffix):
            request_id = unquote(path[len(approval_prefix):-len(approval_suffix)].rstrip("/"))
            self._handle(lambda payload: self.gateway.approval_decision(request_id, payload), HTTPStatus.OK)
            return
        self._json(HTTPStatus.NOT_FOUND, GatewayError("not_found", "接口不存在", status=404).to_dict())

    def _handle(self, action: Any, status: HTTPStatus, *, body: bool = True) -> None:
        try:
            payload: dict[str, Any] = {}
            if body:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 256_000:
                    raise GatewayError("validation_error", "请求体大小无效", status=422)
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise GatewayError("validation_error", "请求体必须是 JSON 对象", status=422)
            self._json(status, action(payload))
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, GatewayError("invalid_json", "JSON 格式无效").to_dict())
        except GatewayError as exc:
            self._json(HTTPStatus(exc.status), exc.to_dict())
        except Exception:
            logging.exception("better-subagent request failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, GatewayError("internal_error", "服务内部错误", status=500).to_dict())

    def _json(self, status: HTTPStatus, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: Any) -> None:
        logging.info("%s - %s", self.address_string(), format_string % args)


class GatewayHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], gateway: GatewayService) -> None:
        self.gateway = gateway
        super().__init__(address, GatewayHandler)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1999)
    parser.add_argument("--data", type=Path, default=root / "data" / "better-subagent.json")
    parser.add_argument("--transport", choices=("sdk-worker", "app-server"), default="app-server")
    parser.add_argument("--app-server-socket", type=Path, default=Path.home() / ".codex/app-server-control/app-server-control.sock")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    transport = (
        AppServerTransport(args.app_server_socket)
        if args.transport == "app-server"
        else CodexSdkWorkerTransport()
    )
    gateway = GatewayService(JsonGatewayStore(args.data), transport)
    server = GatewayHttpServer((args.host, args.port), gateway)
    logging.info("better-subagent listening on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
