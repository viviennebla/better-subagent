"""Local Device Agent runtime boundary.

Phase 2 keeps Coordinator and Device Agent in one process while making runtime
ownership explicit.  The Coordinator only sees this adapter; the adapter owns
the concrete App Server transport.  Phase 3 can replace this local boundary
with the durable remote protocol without changing the public Gateway contract.
"""

from __future__ import annotations

import platform
import socket
from pathlib import Path
from typing import Any, Callable

from .transport import AppServerTransport, CodexSdkWorkerTransport, GatewayTransport


class LocalDeviceAgent:
    """One local runtime Agent bound to one Device and one concrete transport."""

    def __init__(
        self,
        transport: GatewayTransport,
        *,
        device_id: str,
        agent_id: str,
        environment: str,
        role: str = "runtime",
        capabilities: tuple[str, ...] = ("codex",),
    ) -> None:
        self._transport = transport
        self.device_id = device_id
        self.agent_id = agent_id
        self.environment = environment
        self.role = role
        self.capabilities = capabilities

    @classmethod
    def from_app_server(
        cls,
        socket_path: Path,
        *,
        device_id: str | None = None,
        agent_id: str | None = None,
        environment: str = "local",
    ) -> "LocalDeviceAgent":
        resolved_device = device_id or socket.gethostname()
        return cls(
            AppServerTransport(socket_path),
            device_id=resolved_device,
            agent_id=agent_id or f"local@{resolved_device}",
            environment=environment,
        )

    @classmethod
    def from_sdk_worker(
        cls,
        *,
        device_id: str | None = None,
        agent_id: str | None = None,
        environment: str = "local",
    ) -> "LocalDeviceAgent":
        resolved_device = device_id or socket.gethostname()
        return cls(
            CodexSdkWorkerTransport(),
            device_id=resolved_device,
            agent_id=agent_id or f"local@{resolved_device}",
            environment=environment,
        )

    def device_record(self) -> dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "name": self.device_id,
            "environment": self.environment,
            "platform": platform.system().lower(),
            "status": "online",
            "capabilities": list(self.capabilities),
        }

    def agent_record(self) -> dict[str, Any]:
        return {
            "agentId": self.agent_id,
            "deviceId": self.device_id,
            "role": self.role,
            "capabilities": list(self.capabilities),
            "enabled": True,
        }

    def __getattr__(self, name: str) -> Any:
        # Preserve exact optional capability detection from the concrete
        # transport. getattr(agent, name, None) must remain None when the local
        # App Server / SDK transport does not implement that capability.
        return getattr(self._transport, name)

    def start_turn(self, params: dict[str, Any], on_terminal: Callable[[str, str | None], None]) -> dict[str, Any]:
        return self._transport.start_turn(params, on_terminal)

    def interrupt_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._transport.interrupt_turn(params)

