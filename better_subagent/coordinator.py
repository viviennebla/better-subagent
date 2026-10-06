"""Single-device Coordinator for the Phase 2 local Device Agent model."""

from __future__ import annotations

from typing import Any

from .contracts import now_iso, session_summary
from .device_agent import LocalDeviceAgent
from .gateway import GatewayService
from .storage import SqliteGatewayStore


class CoordinatorService(GatewayService):
    def __init__(self, store: SqliteGatewayStore, device_agent: LocalDeviceAgent) -> None:
        self.device_agent = device_agent
        self._ensure_local_runtime_projection(store, device_agent)
        super().__init__(store, device_agent)

    @staticmethod
    def _ensure_local_runtime_projection(
        store: SqliteGatewayStore, device_agent: LocalDeviceAgent
    ) -> None:
        store.upsert_device(device_agent.device_record())
        store.upsert_agent(device_agent.agent_record())
        with store.locked() as board:
            changed = False
            for session in board["sessions"].values():
                if "deviceId" not in session:
                    session["deviceId"] = device_agent.device_id
                    changed = True
                if "agentId" not in session:
                    session["agentId"] = device_agent.agent_id
                    changed = True
                if "environment" not in session:
                    session["environment"] = device_agent.environment
                    changed = True
                if "controlGeneration" not in session:
                    session["controlGeneration"] = 1
                    changed = True
            for run in board["runs"]:
                session = board["sessions"].get(run.get("sessionId"), {})
                if not isinstance(session, dict):
                    continue
                defaults = {
                    "targetDeviceId": session.get("deviceId", device_agent.device_id),
                    "targetAgentId": session.get("agentId", device_agent.agent_id),
                    "controlGeneration": session.get("controlGeneration", 1),
                }
                for key, value in defaults.items():
                    if key not in run:
                        run[key] = value
                        changed = True
            if changed:
                store.save(board)

    def list_devices(self) -> dict[str, Any]:
        return {"devices": self.store.list_devices()}

    def list_agents(self) -> dict[str, Any]:
        return {"agents": self.store.list_agents()}

    def put_session(self, session_id: str, payload: Any) -> dict[str, Any]:
        result = super().put_session(session_id, payload)
        with self.store.locked() as board:
            session = board["sessions"].get(session_id)
            changed = False
            if isinstance(session, dict):
                defaults = {
                    "deviceId": self.device_agent.device_id,
                    "agentId": self.device_agent.agent_id,
                    "environment": self.device_agent.environment,
                    "controlGeneration": 1,
                }
                for key, value in defaults.items():
                    if key not in session:
                        session[key] = value
                        changed = True
                if changed:
                    session["updatedAt"] = now_iso()
                    self.store.save(board)
                response = {"session": session_summary(session, board["runs"])}
                if result.get("idempotent"):
                    response["idempotent"] = True
                return response
        return result
