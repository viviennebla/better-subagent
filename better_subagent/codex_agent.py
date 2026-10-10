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

def build_agent_tree_page(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group only the returned page; do not invent unseen parents or root nodes."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for thread in threads:
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str) or not thread["id"]:
            continue
        projected = project_thread_agent(thread)
        node = {
            "threadId": thread["id"],
            **projected,
            "name": thread.get("name") if isinstance(thread.get("name"), str) else None,
            "status": thread.get("status"),
        }
        groups.setdefault(projected["sessionTreeId"], []).append(node)

    trees = []
    for session_id, nodes in groups.items():
        visible_ids = {node["threadId"] for node in nodes}
        roots = []
        missing_parents = set()
        for node in nodes:
            parent = node["parentThreadId"]
            node["parentInPage"] = bool(parent and parent in visible_ids)
            node["childrenThreadIds"] = [
                candidate["threadId"] for candidate in nodes
                if candidate["parentThreadId"] == node["threadId"]
            ]
            if parent is None:
                roots.append(node["threadId"])
            elif parent not in visible_ids:
                missing_parents.add(parent)
        trees.append({
            "sessionTreeId": session_id,
            "threads": nodes,
            "rootThreadIds": roots,
            "unresolvedParentThreadIds": sorted(missing_parents),
        })
    return trees


def project_collaboration_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Only project explicit Codex collaboration items from paginated history.

    An absent prompt is NOT proof of encryption: other V2 paths can omit it.
    Do not interpret ordinary assistant messages as inter-agent communication.
    """
    item = entry.get("item")
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if kind not in {"collabAgentToolCall", "subAgentActivity"}:
        return None
    item_id = item.get("id")
    turn_id = entry.get("turnId")
    if not isinstance(item_id, str) or not item_id or not isinstance(turn_id, str) or not turn_id:
        return None

    base: dict[str, Any] = {
        "itemId": item_id,
        "turnId": turn_id,
        "kind": kind,
        "startedAtMs": entry.get("startedAtMs"),
        "completedAtMs": entry.get("completedAtMs"),
    }
    if kind == "subAgentActivity":
        base.update({
            "activityKind": item.get("kind"),
            "agentThreadId": item.get("agentThreadId"),
            "agentPath": item.get("agentPath"),
            "messageVisibility": "notApplicable",
            "message": None,
        })
        return base

    text = item.get("prompt")
    readable = isinstance(text, str)
    # No ciphertext or tool arguments are included in this response.
    base.update({
        "tool": item.get("tool"),
        "status": item.get("status"),
        "senderThreadId": item.get("senderThreadId"),
        "receiverThreadIds": item.get("receiverThreadIds") if isinstance(item.get("receiverThreadIds"), list) else [],
        "messageVisibility": "readable" if readable else "unavailable",
        "message": text if readable else None,
    })
    return base
