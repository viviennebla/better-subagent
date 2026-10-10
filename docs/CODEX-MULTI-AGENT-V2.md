# Codex Multi-Agent V2：Gateway 概念与能力映射

目的：支持用户观察 Root/Subagent 协作，最终实现明确授权的人工介入。**不重实现 Codex Agent runtime**。

## 身份边界

| 名称 | 来源 | 含义 |
| --- | --- | --- |
| `threadId` | Codex `Thread.id` | 唯一对话/执行身份；Root 与 Subagent 不共用 |
| `sessionTreeId` | Codex `Thread.sessionId` | 同一 Agent 树共享的分组身份 |
| `parentThreadId` | Codex `Thread.parentThreadId` | Subagent 的控制父节点；没有值不能推断一定是 Root |
| `forkedFromId` | Codex `Thread.forkedFromId` | Fork 的历史来源，不等于 Subagent Parent |
| `agentRole` / `agentNickname` | Codex Thread | 角色和显示昵称，均可能缺失 |
| Gateway `sessionId` | 本项目 Session Registry | 单个 Thread 的调度记录，不等于整棵 Agent 树 |
| `agentId` | 本项目 Device Agent Registry | 设备运行时的 Agent，不是 Codex Subagent |
| `gatewayRunId` | 本项目 Run Store | 一次 Gateway 调度；`transportTurnId` 才是 Codex Turn |

`AgentPath`（例如 `/root/reviewer`）在 Codex Core 存在，但当前公开 Thread Schema 没有稳定映射；**不从昵称或 Thread ID 猜测路径**。

## V2 能力与观察

- `canAcceptDirectInput: true` → `directInputStatus=allowed`。
- `canAcceptDirectInput: false` → `directInputStatus=denied`；即使手工注册 Gateway Session 也不能直接 `turn/start`。
- `canAcceptDirectInput: null/缺失` → `directInputStatus=unknown`；不把 unloaded Thread 误判成可写或不可写。真正请求仍以 App Server 校验为准。
- `thread/list` 的 overview 现在暴露 `sessionTreeId`、`parentThreadId`、`forkedFromId`、角色/昵称、`threadSource` 和直接输入能力。列表当前限于第一页，不能当作完整 Agent Tree。
- `thread/read` 是持久历史读取，不等同于获得该 Thread 的控制权；转到 `turn/start` 前仍需正常 resume/ownership 协调。

## 消息边界

- V2 中 `spawn_agent` / `send_message` / `followup_task` 可能经加密的 `agent_message` 传递。上游也有支持特定明文路径的变更；不能按版本号固定假定所有内容都可读。
- 只有原始可读正文才展示为可读审计记录；密文只能标记不可读，不能用模型总结冒充原文。
- 不把 Root 代理转发消息记录成用户直接对 Child 发的消息。
- 目前只建立身份/能力投影；统一的通信时间线和人工直聊仍是待开发功能。

## 相关上游问题

- [#33885：V2 Subagent 不允许直接修正/steer](https://github.com/openai/codex/issues/33885)
- [#28058：加密的 Agent 间消息缺少可读审计记录](https://github.com/openai/codex/issues/28058)

下一步：基于 App Server 的 paginated Turn/Item 形成可区分可读/不可读的协作时间线；再讨论 Human Intervention。不能绕过服务端已拒绝的直接输入权限。
