"""Public C1 contracts and validation for better-subagent."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUN_STATUSES = {"starting", "active", "interrupting", "completed", "failed", "interrupted", "unknown"}
OCCUPYING_RUN_STATUSES = {"starting", "active", "interrupting", "unknown"}
TERMINAL_RUN_STATUSES = {"completed", "failed", "interrupted"}
APPROVAL_POLICIES = {"on-request", "never", "untrusted"}
SANDBOX_POLICIES = {"read-only", "workspace-write"}
OPERATION_POLICY_PRESETS = {"development", "workspace", "full-access"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
REPORT_VERDICTS = {"approved", "ready_for_review", "changes_requested", "blocked", "rejected"}

# The Board sends only sessionId, prompt and requestId. The structured final
# output contract is therefore a Gateway-owned execution invariant, not a
# caller-controlled SDK option.
STAGE_REPORT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "taskId", "stage", "role", "sessionId", "summary", "outcome", "verdict",
        "artifact", "findings", "implementation", "verification", "risks", "nextAction",
    ],
    "properties": {
        "taskId": {"type": "string"},
        "stage": {"type": "string"},
        "role": {"type": "string"},
        "sessionId": {"type": "string"},
        "summary": {"type": "string"},
        "outcome": {"type": "string", "enum": sorted(REPORT_VERDICTS)},
        "verdict": {"type": "string", "enum": sorted(REPORT_VERDICTS)},
        "artifact": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "implementation": {"type": "string"},
        "verification": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "nextAction": {"type": "string"},
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class GatewayError(Exception):
    """Stable error envelope returned by the Gateway HTTP API."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}
        self.request_id = request_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
            "requestId": self.request_id,
        }


def text(value: Any, field: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise GatewayError("validation_error", f"{field} 必须是字符串", status=422)
    normalized = value.strip()
    if required and not normalized:
        raise GatewayError("validation_error", f"{field} 不能为空", status=422)
    if len(normalized) > maximum:
        raise GatewayError("validation_error", f"{field} 不能超过 {maximum} 个字符", status=422)
    return normalized


SESSION_CONFIG_FIELDS = {
    "sessionId",
    "owner",
    "role",
    "threadId",
    "cwd",
    "model",
    "effort",
    "approvalPolicy",
    "sandboxPolicy",
    "enabled",
    "unavailableReason",
    "operationPolicy",
    "runtimeWorkspaceRoots",
    "controlMode",
}


def validate_session_config(session_id: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GatewayError("validation_error", "Session 配置必须是对象", status=422)
    unknown = set(value) - SESSION_CONFIG_FIELDS
    if unknown:
        raise GatewayError("validation_error", f"Session 配置不支持字段: {', '.join(sorted(unknown))}", status=422)
    normalized_id = text(session_id, "sessionId", 160)
    body_id = text(value.get("sessionId", ""), "session.sessionId", 160)
    if body_id != normalized_id:
        raise GatewayError("validation_error", "路径 sessionId 与配置不一致", status=422)
    cwd = text(value.get("cwd", ""), "session.cwd", 500)
    if not Path(cwd).is_absolute() or "\x00" in cwd:
        raise GatewayError("validation_error", "session.cwd 必须是绝对路径", status=422)
    effort = text(value.get("effort", ""), "session.effort", 40)
    if effort not in EFFORTS:
        raise GatewayError("validation_error", f"不支持的 effort: {effort}", status=422)
    approval = text(value.get("approvalPolicy", ""), "session.approvalPolicy", 80)
    if approval not in APPROVAL_POLICIES:
        raise GatewayError("validation_error", f"不支持的 approvalPolicy: {approval}", status=422)
    sandbox = text(value.get("sandboxPolicy", ""), "session.sandboxPolicy", 80)
    if sandbox not in SANDBOX_POLICIES:
        raise GatewayError("validation_error", f"不支持的 sandboxPolicy: {sandbox}", status=422)
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise GatewayError("validation_error", "session.enabled 必须是布尔值", status=422)
    unavailable_reason = text(
        value.get("unavailableReason", ""), "session.unavailableReason", 500, required=False
    )
    operation_policy = value.get("operationPolicy", {"preset": "development"})
    if isinstance(operation_policy, str):
        operation_policy = {"preset": operation_policy}
    if not isinstance(operation_policy, dict):
        raise GatewayError("validation_error", "session.operationPolicy 必须是对象", status=422)
    unknown_policy = set(operation_policy) - {
        "preset", "permissionProfileId", "approvalPolicy", "approvalsReviewer",
        "allowSessionApproval", "runtimeWorkspaceRoots",
    }
    if unknown_policy:
        raise GatewayError("validation_error", f"operationPolicy 不支持字段: {', '.join(sorted(unknown_policy))}", status=422)
    preset = operation_policy.get("preset", "development")
    if preset not in OPERATION_POLICY_PRESETS:
        raise GatewayError("validation_error", f"不支持的 operationPolicy preset: {preset}", status=422)
    roots = value.get("runtimeWorkspaceRoots", operation_policy.get("runtimeWorkspaceRoots", [cwd]))
    if not isinstance(roots, list) or not all(isinstance(root, str) and Path(root).is_absolute() for root in roots):
        raise GatewayError("validation_error", "runtimeWorkspaceRoots 必须是绝对路径数组", status=422)
    control_mode = value.get("controlMode", "managed")
    if control_mode not in {"managed", "external"}:
        raise GatewayError("validation_error", "controlMode 必须是 managed 或 external", status=422)
    if not enabled and not unavailable_reason:
        raise GatewayError("validation_error", "不可用 Session 必须提供 unavailableReason", status=422)
    return {
        "sessionId": normalized_id,
        "owner": text(value.get("owner", ""), "session.owner", 120),
        "role": text(value.get("role", ""), "session.role", 80),
        "threadId": text(value.get("threadId", ""), "session.threadId", 160),
        "cwd": cwd,
        "model": text(value.get("model", ""), "session.model", 120),
        "effort": effort,
        "approvalPolicy": approval,
        "sandboxPolicy": sandbox,
        "enabled": enabled,
        "unavailableReason": unavailable_reason,
        "controlMode": control_mode,
        "runtimeStatus": "notLoaded",
        "activeTurnId": None,
        "runtimeWorkspaceRoots": roots,
        "requestedPolicy": {
            "preset": preset,
            "permissionProfileId": operation_policy.get("permissionProfileId", "vimo-development" if preset == "development" else preset),
            "approvalPolicy": operation_policy.get("approvalPolicy", approval),
            "approvalsReviewer": operation_policy.get("approvalsReviewer", "auto_review"),
            "allowSessionApproval": bool(operation_policy.get("allowSessionApproval", True)),
            "runtimeWorkspaceRoots": roots,
        },
        "effectivePolicy": value.get("effectivePolicy"),
        "policySource": value.get("policySource", "default"),
        "policyUpdatedAt": value.get("policyUpdatedAt"),
        "updatedAt": now_iso(),
    }


def validate_start_run(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise GatewayError("validation_error", "StartRun 必须是对象", status=422)
    unknown = set(value) - {"requestId", "sessionId", "prompt"}
    if unknown:
        raise GatewayError("validation_error", f"StartRun 不支持字段: {', '.join(sorted(unknown))}", status=422)
    return {
        "requestId": text(value.get("requestId", ""), "requestId", 160),
        "sessionId": text(value.get("sessionId", ""), "sessionId", 160),
        "prompt": text(value.get("prompt", ""), "prompt", 128_000),
    }


def validate_interrupt_run(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise GatewayError("validation_error", "InterruptRun 必须是对象", status=422)
    unknown = set(value) - {"requestId"}
    if unknown:
        raise GatewayError("validation_error", f"InterruptRun 不支持字段: {', '.join(sorted(unknown))}", status=422)
    return {"requestId": text(value.get("requestId", ""), "requestId", 160)}


def validate_steer_run(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise GatewayError("validation_error", "SteerRun 必须是对象", status=422)
    unknown = set(value) - {"requestId", "prompt"}
    if unknown:
        raise GatewayError("validation_error", f"SteerRun 不支持字段: {', '.join(sorted(unknown))}", status=422)
    return {"requestId": text(value.get("requestId", ""), "requestId", 160), "prompt": text(value.get("prompt", ""), "prompt", 128_000)}


def validate_approval_decision(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GatewayError("validation_error", "ApprovalDecision 必须是对象", status=422)
    unknown = set(value) - {"requestId", "decision", "grantedPermissions"}
    if unknown:
        raise GatewayError("validation_error", f"ApprovalDecision 不支持字段: {', '.join(sorted(unknown))}", status=422)
    decision = text(value.get("decision", ""), "decision", 40)
    if decision not in {"accept", "acceptForSession", "decline", "cancel"}:
        raise GatewayError("validation_error", f"不支持的审批决定: {decision}", status=422)
    permissions = value.get("grantedPermissions")
    if permissions is not None and not isinstance(permissions, dict):
        raise GatewayError("validation_error", "grantedPermissions 必须是对象", status=422)
    return {"requestId": text(value.get("requestId", ""), "requestId", 160), "decision": decision, "grantedPermissions": permissions}


def session_summary(session: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    busy = any(
        run.get("sessionId") == session.get("sessionId") and run.get("status") in OCCUPYING_RUN_STATUSES
        for run in runs
    )
    enabled = bool(session.get("enabled", True))
    return {
        "sessionId": session["sessionId"],
        "owner": session["owner"],
        "role": session["role"],
        "status": "external" if session.get("controlMode") == "external" and session.get("runtimeStatus") == "active" else "busy" if busy else "idle" if enabled else "unavailable",
        "busy": busy,
        "unavailableReason": "" if enabled else session.get("unavailableReason", ""),
        "updatedAt": session["updatedAt"],
        "controlMode": session.get("controlMode", "managed"),
        "runtimeStatus": session.get("runtimeStatus", "notLoaded"),
        "activeTurnId": session.get("activeTurnId"),
        "requestedPolicy": session.get("requestedPolicy"),
        "effectivePolicy": session.get("effectivePolicy"),
        "policySource": session.get("policySource", "default"),
        "policyUpdatedAt": session.get("policyUpdatedAt"),
    }


def run_status(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "gatewayRunId": run["gatewayRunId"],
        "requestId": run["requestId"],
        "sessionId": run["sessionId"],
        "status": run["status"],
        "createdAt": run["createdAt"],
        "updatedAt": run["updatedAt"],
        "startedAt": run.get("startedAt"),
        "terminalAt": run.get("terminalAt"),
        "error": run.get("error"),
    }
