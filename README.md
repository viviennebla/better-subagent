# better-subagent

`better-subagent` 是面向 Codex Session 的轻量 Gateway。它把 Session 注册、单活 Run、启动、状态查询和打断从 Web Ledger 中抽离出来，让 Board 只处理 Task、人工调度和业务状态。

## 为什么需要 Better Subagent

Codex Multi-Agent V2 将 ThreadSpawn Subagent 的工作协调权交给 Parent Agent，但用户因此失去了直接向这些 Subagent 追加指令或修正正在执行的工作的入口；同时，Agent 间任务消息可能以加密载荷传递，本地历史无法始终还原“Root 到底向 Subagent 发送了什么”。这两项限制是本项目的核心动机之一：

- [openai/codex #33885 — 允许直接修正和 steer V2 Subagent](https://github.com/openai/codex/issues/33885)：`turn/start` / `turn/steer` 对 V2 的 Parent-owned child 被服务端拒绝，并非只缺少 GUI 输入框。
- [openai/codex #28058 — V2 加密消息使任务审计不可读](https://github.com/openai/codex/issues/28058)：`spawn_agent`、`send_message`、`followup_task` 的加密传输可能使历史记录缺少可读委托正文。加密和明文传输路径会随上游变化，不能默认所有消息都可读或全部不可读。

项目的目标不是另造一个 Codex Agent 执行引擎，而是**在复用 Codex 原生 Thread / Turn / Agent 生命周期的前提下，使用户能看见协作关系、审计可见的消息并显式介入 Subagent**。

当前能力是 Gateway 的 Thread/Run 管理及多设备运行时建设；**V2 Parent-owned Subagent 直接输入和加密消息解密尚未实现**。在 App Server 禁止直接输入时，不绕过权限，也不将经过 Root 转发的消息伪装成直接对话；无法读取的加密内容应明确标记为不可读。

## 核心模型

- **Codex Session Tree**：Root 与 Subagents 共享的会话树身份（上游 `thread.sessionId`）。
- **Codex Thread**：每个 Root / Subagent 独立的 `thread.id`，通过 `parentThreadId` 组成协作树；`forkedFromId` 是另一种来源关系，不代表 Parent。
- **Codex Turn / Item**：一次 Agent 执行及其消息、工具与事件；一个 Turn 可以包含多次模型请求。
- **Gateway Session**：与 Codex Thread 绑定的调度/控制记录（当前 API 的 `sessionId`），不等于整棵 Codex Session Tree。
- **Gateway Run**：一次调度执行，记录对应的上游 Turn；**Device Agent** 是设备侧运行时，不等于 Codex **Subagent**。

下一步优先让 Gateway 正确投影 V1/V2 的 Agent 关系、能力与消息可见性，再设计人类直接参与的控制入口。Multi-device Runtime 与这些能力可以在同一 Gateway 上演进，不以旧 API 兼容作为架构前提。
App Server C1 MVP 已在 Codex 0.154.0 的 GUI-managed Unix WebSocket 上完成真实 runtime calibration；当前已具备 history、start、steer、interrupt、运行时审批以及 managed/external handoff/reclaim，并进入 Phase 2 Board adapter 接入。SDK worker 保留为配置级回退；自动重连恢复、分页 history 和完整 settings/effective policy 投影继续延期。方案与校准边界见 [`docs/APP-SERVER-NEXT-ROUND-PLAN.md`](docs/APP-SERVER-NEXT-ROUND-PLAN.md)。

## 当前接口

- `GET /health`
- `GET /v1/sessions`
- `GET /v1/sessions/overview`（App Server thread 概览，含未注册 Session）
- `GET /v1/sessions/{sessionId}`（runtime/control、policy、pending approvals）
- `GET /v1/sessions/{sessionId}/recap`（最近完成 Turn 的最终回复，失败时降级 preview）
- `PUT /v1/sessions/{sessionId}`
- `GET /v1/sessions/{sessionId}/history`
- `POST /v1/sessions/{sessionId}/handoff`
- `POST /v1/sessions/{sessionId}/reclaim`
- `POST /v1/runs`
- `GET /v1/runs/{gatewayRunId}`
- `POST /v1/runs/{gatewayRunId}/steer`
- `POST /v1/runs/{gatewayRunId}/interrupt`
- `POST /v1/approvals/{requestId}/decision`

Session detail exposes only the control/runtime summary and a bounded approval projection (`requestId`, method, session/turn IDs, supported decisions, status). `handoff` is terminal-event driven: an active managed Session remains managed while interrupting and becomes external only after an `interrupted` terminal event. Unknown transport outcomes remain `unknown` and are not retried automatically.

完整合同见 [`docs/BETTER-SUBAGENT-CONTRACT.md`](docs/BETTER-SUBAGENT-CONTRACT.md)。

## 下一阶段：Multi-device Runtime

better-subagent 的下一阶段不是把机器管理塞回 Board，而是从单机 Session Gateway 演进为多设备 Agent Runtime：

- [Multi-device Better Subagent PRD](docs/MULTI-DEVICE-RUNTIME-PRD.md)
- [Multi-device Better Subagent 技术方案](docs/MULTI-DEVICE-RUNTIME-TECHNICAL-DESIGN.md)

首个业务验收场景是：开发机 Agent 完成开发并部署测试环境后，Board 将 Verification Task 调度给 `verifier@test`，better-subagent 把 Run 路由到测试设备上的 Device Agent / Codex App Server，并可靠回传终态。实现从 SQLite single-device parity 开始，不直接跳到远程协议。

## 本地运行

```bash
npm ci
python3 -m better_subagent --host 127.0.0.1 --port 1999
```

默认运行数据写入 `data/better-subagent.json`，运行日志和数据不进入 Git。

## 验证

```bash
python3 -m unittest discover -s tests -v
node --check scripts/codex-sdk-worker.mjs
```

## 与 Web Ledger 的边界

- Gateway 是 Session runtime、Run 和控制方的事实源。
- Ledger 是 Task、阶段报告、人工调度和业务决策的事实源。
- workflow skill 规定角色协作方式，不保存运行态，也不替代 Gateway 的控制权判断。
