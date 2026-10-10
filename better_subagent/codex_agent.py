"""Lossless, capability-aware projection of Codex Thread identity.

This is not a second Agent runtime. The App Server owns the truth: unknown
capabilities stay unknown, fork ancestry is not parent-child ownership, and
encrypted content is not inferred from metadata.
"""

from __future__ import annotations

from typing import Any


def project_thread_agent(thread: dict[str, Any]) -> dict[str, Any]:
    def optional_string(key: str) -> str | None:
        value = thread.get(key)
        return value if isinstance(value, str) and value else None

    thread_id = optional_string("id")
    capability = thread.get("canAcceptDirectInput")
    can_accept = capability if isinstance(capability, bool) else None
    status = "unknown" if can_accept is None else ("allowed" if can_accept else "denied")

    # A fork is not a Subagent. A missing parent may reflect older/unloaded
    # threads; never guess an Agent ownership relationship from the session ID.
    return {
        "sessionTreeId": optional_string("sessionId") or thread_id,
        "parentThreadId": optional_string("parentThreadId"),
        "forkedFromId": optional_string("forkedFromId"),
        "agentRole": optional_string("agentRole"),
        "agentNickname": optional_string("agentNickname"),
        "threadSource": thread.get("threadSource"),
        "canAcceptDirectInput": can_accept,
        "directInputStatus": status,
    }
