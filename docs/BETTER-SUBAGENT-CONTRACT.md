# better-subagent C1 合同

状态：已接入 Board 的 MVP 合同。Board 使用轻量 HTTP client，不再直接持有 SDK worker。

## 边界

better-subagent 是 Codex Session Gateway。它独占 Session runtime 配置、Session 单活、Gateway Run、SDK worker 与 interrupt；Board 只传 `sessionId`、完整 Prompt 和稳定 `requestId`。Browser 与 Agent 不直接访问 Gateway。

本轮继续使用 `@openai/codex-sdk` 0.153.4 与 `scripts/codex-sdk-worker.mjs`，使用原子 JSON 文件，不包含登录/RBAC、SQLite、App Server、运行时审批转发、HA 或跨进程恢复。

## SessionSummary

`GET /v1/sessions` 只返回候选信息，不泄露 thread、cwd、model 或权限参数：

```json
{
  "sessionId": "00000000-0000-0000-0000-000000000001",
  "owner": "better-subagent-coder",
  "role": "coder",
  "status": "idle",
  "busy": false,
  "unavailableReason": "",
  "updatedAt": "2026-09-10T12:00:00+08:00"
}
```

`status` 为 `idle | busy | unavailable`。`busy` 由 Gateway Run 推导，调用方不能写入。

## StartRun

```http
POST /v1/runs
```

```json
{
  "requestId": "board-action-123",
  "sessionId": "00000000-0000-0000-0000-000000000001",
  "prompt": "服务端构造的完整阶段交接 Prompt"
}
```

同一 `requestId` 与相同 `sessionId + prompt` 重试返回同一个 Run；复用 `requestId` 提交不同内容返回 `409 request_id_conflict`。请求不得包含 cwd、threadId、model、权限或 SDK method；这些参数只从 Gateway Session registry 解析。

Gateway 固定向 SDK 传入 StageReport `outputSchema`；调用方不能覆盖该执行约束。

## RunStatus

`POST /v1/runs` 与 `GET /v1/runs/{gatewayRunId}` 返回：

```json
{
  "gatewayRunId": "run-...",
  "requestId": "board-action-123",
  "sessionId": "01a0...",
  "status": "active",
  "createdAt": "2026-09-10T12:00:00+08:00",
  "updatedAt": "2026-09-10T12:00:01+08:00",
  "startedAt": "2026-09-10T12:00:01+08:00",
  "terminalAt": null,
  "error": null
}
```

状态流：

```text
starting → active → interrupting → interrupted
                  ├──────────────→ completed
                  └──────────────→ failed
starting → failed | unknown
```

`starting | active | interrupting | unknown` 均占用 Session。`unknown` fail closed，不自动重放 Prompt。

Gateway 只在匹配的 `thread.started` 与 `turn.started` 均已出现、且此前没有 startup/identity/事件顺序错误时接受 terminal 事件并返回 worker handle；错误后出现的合法事件不能重新建立 authority，已观察 Turn 的结果保持 `unknown`。authority 建立后的协议或 identity 失真也必须立即投影为 `unknown`，worker 清理后不得残留不可打断的 `active`。

## InterruptRun

```http
POST /v1/runs/{gatewayRunId}/interrupt
```

```json
{"requestId":"board-interrupt-456"}
```

只有 Gateway 当前持有 worker 的 `active` Run 可以打断。相同 interrupt `requestId` 的并发重试不会再次调用 worker：首个请求仍在处理中时返回当前 `interrupting` 且 `idempotent=true`，终结后重试返回同一终态。Gateway 收到 worker 的真实终止事实后，首个请求才返回 `interrupted`。User-only 业务授权仍由 Board 执行。

## GatewayError

```json
{
  "error": "session_busy",
  "message": "目标 Session 已有未终结 Run",
  "details": {"gatewayRunId": "run-...", "status": "active"},
  "requestId": "board-action-124"
}
```

固定错误包括 `validation_error`、`session_not_found`、`session_unavailable`、`session_busy`、`request_id_conflict`、`run_not_found`、`run_not_active`、`run_not_interruptible`、`run_start_failed`、`interrupt_in_progress` 和 `interrupt_failed`。

## Session 配置入口

`PUT /v1/sessions/{sessionId}` 是 Board/迁移工具使用的内部入口，提交完整的 `sessionId/owner/role/threadId/cwd/model/effort/approvalPolicy/sandboxPolicy/enabled/unavailableReason`。存在占用 Run 时返回 `409 session_busy`。

## 已知延后

- Gateway 重启后的 worker attach/reconcile；当前 `unknown` 需要人工处理。
- SDK 原生审批转发和 App Server transport。后续迁移采用“开发效率优先、宽基础权限 + 可控提权”的[App Server 权限调查与建议](./APP-SERVER-PERMISSIONS-INVESTIGATION.md)，不把 coder 默认限制为 read-only 或逐命令人工审批。
- 登录、不可伪造身份、细粒度 RBAC、日志治理、SQLite、HA 和部署。
