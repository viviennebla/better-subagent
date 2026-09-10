# App Server C0 协议校准

日期：2026-09-10；目标版本：Codex CLI/App Server 0.154.0。本文是只读校准记录，不代表已实现 Gateway。

## 环境基线与证据

- `codex --version`：`codex-cli 0.154.0`。
- `codex app-server daemon version`（对现有 control socket 的只读请求）：`status=running`，CLI/App Server/managed Codex 均为 `0.154.0`；socket 为 `/home/feiyan/.codex/app-server-control/app-server-control.sock`。
- `stat`：control socket 所在目录 `0700`，socket `0600`，均属 `feiyan`。这是同一 OS 用户内的隔离，不是 VIMO owner/role 授权。
- `codex app-server --help` 证实有 `daemon start|restart|stop|version`、`proxy --sock`、`generate-json-schema --experimental`；本次未调用 start/restart/stop。
- 以 `generate-json-schema --experimental --out /tmp/c0-schema` 生成本机 0.154.0 schema（临时目录）。Schema 直接证实下述字段与枚举。
- 官方 [Codex App Server 文档](https://learn.chatgpt.com/zh-Hans/docs/app-server)确认 `thread/read/resume`、turn 生命周期、审批和 `serverRequest/resolved` 等公开语义；本机生成的 0.154.0 schema 用于校准当前安装版本的具体字段。

## 事实矩阵

| 项目 | 证据/可复现命令 | 结论 |
|---|---|---|
| daemon 与 GUI 使用的版本 | `codex app-server daemon version` 返回 running、四个版本均 `0.154.0` | **已证实**当前 control socket 有正在运行的 daemon；`proxy` 是官方提供的连接入口。 |
| proxy 实际连接/initialize | 一次性 stdin 探针与保持 stdin 开放的完整双向探针均未收到 initialize 响应；后者未调用 thread/turn 写方法 | **未验证**协议会话是否已完成。现象与 managed daemon 尚未启用 remote-control 一致，但在启用前只能视为推断。 |
| thread/read | schema：`ThreadReadParams {threadId, includeTurns}`；响应含完整 `thread`。`includeTurns=true` 的说明是历史 hydration，分页 API 更推荐 | **协议已证实**只读历史路径；多 client 实际一致性未验证。Gateway 应保存 threadId，并把完整历史读取与 live subscription 分开。 |
| thread/resume | schema：`threadId` 必填，可显式覆盖 `permissions`、`approvalPolicy`、`approvalsReviewer`、`cwd`、`runtimeWorkspaceRoots`；响应含 `activePermissionProfile`、`sandbox`、effective 审批字段 | **协议已证实**冷 resume 可显式提交策略；跨 client resume 的锁/并发行为未验证。 |
| 多 client subscription | schema 存在 `thread/status/changed`、`turn/started`、`turn/completed`、`serverRequest/resolved` 等通知，以及 `thread/unsubscribe` | **协议已证实有连接级事件流**；同一 thread 两 client 是否都收到完整事件、是否需各自 subscribe，**未验证**。 |
| 外部 client 启动 turn 的观察 | `TurnStartParams` 要求 `threadId,input`；`ThreadStatus` 枚举 `notLoaded|idle|active|systemError`，active 带 `activeFlags` | **可设计为观察状态**；本机未在临时 C0 thread 上启动模型 turn，故外部 writer 识别和事件时序**未验证**。 |
| 未决审批由后接入 client 响应 | `PermissionsRequestApprovalParams` 含 `threadId/turnId/itemId`；响应 scope 枚举 `turn|session`，schema 无 client ownership 字段 | **不能证明可跨 client 接管**。Phase 1 必须采用“打断后移交”，除非专门实验成功。 |
| daemon/Gateway 重连 | daemon version 只证明 daemon 存活；未停止/重启任何服务，未制造断线 | thread/turn/pending request 的重连恢复**未验证**；Gateway 不得把 reconnect 后未知状态伪造为 done。 |

## 权限能力矩阵（0.154.0 本机 schema）

| 能力 | 真实字段/值 | C0 结论 |
|---|---|---|
| named profile | `thread/start`、`thread/resume`、`turn/start` 使用 `permissions`；与 `sandbox`/`sandboxPolicy` 互斥 | **已证实协议字段**，`vimo-development` 是否宿主允许仍未验证。 |
| profile 枚举 | `permissionProfile/list {cwd,limit,cursor}` → `{id,allowed,description}` | Gateway 必须先筛 `allowed=true`；不要硬编码“存在即可用”。 |
| effective profile | start/resume/settings 通知含 `activePermissionProfile.id`（可含 `extends`） | requested/effective 必须分开记录；只有响应/`thread/settings/updated` 后更新 effective。 |
| 审批策略 | `approvalPolicy`: `untrusted|on-request|never`（也支持 granular 对象） | `on-request` 是协议合法值。 |
| 审批 reviewer | `user|auto_review|guardian_subagent` | `auto_review` 是本机 config 当前值（`~/.codex/config.toml`），但 thread 实际 effective 尚未经独立 thread response 验证。 |
| acceptForSession | 审批响应 `scope: "session"`；另有 `turn` | Session 级授权是协议能力，但跨 client/持久性/重连语义**未验证**；不得宣称全局永久授权。 |
| settings 更新 | `thread/settings/update` 可改 `permissions`、`approvalPolicy`、`approvalsReviewer`、`sandboxPolicy`；`thread/settings/updated` 返回完整 `threadSettings` | Phase 1 只在通知到达后投影 effective policy。 |

## 人工查看与移交边界

已证实：查看可以建模为 `thread/read(includeTurns=true)`；thread/read 不含 turn start 或 control 参数。协议有 `turn/interrupt`、`turn/steer` 与 terminal `turn/completed`，因此“由 Gateway 主动打断后移交”有明确 RPC 基础。

未证实：GUI/CLI 只读是否一定不改变 Gateway subscription；后接入 client 能否响应前一 client 的 pending approval；外部 active turn 的完整通知时序；daemon/Gateway 断线后 pending request 是否仍可由新连接处理。首版不要做“等待审批时无缝移交”。

## 对 Phase 1 接口的最小修订

1. transport adapter 必须实现连接初始化（`capabilities.experimentalApi=true`）、请求/响应关联、通知 fan-out、重连和未知状态；proxy 无响应的现象要在独立 C0 client 中复测并纳入错误分类。
2. `thread/read` 与订阅是两个能力：history 读取不能代替事件订阅；每个连接应明确订阅/取消订阅及 cursor 策略。
3. 统一内部 `operationPolicy`，但 adapter 映射为 `permissions`（start/resume/turn/settings）和 `sandboxPolicy`（settings/turn）；永不同时发送互斥字段。
4. approval action 先支持当前 client 的 turn/session scope；跨 client pending request 未证实，handoff 默认 `interrupt → turn/completed(interrupted) → external`。
5. reconnect 后先重新 resume/read 并等待 status；`active` 或 pending 状态未知时进入恢复中，不提交新 turn。

## C0 判定与需用户决定

**C0：不通过（部分协议校准通过，运行时行为校准未完成）。** 已确认版本、socket、命令面、schema、权限字段及 terminal/approval 类型；多 client、外部 turn、跨 client approval、重连恢复均未取得安全的运行时证据。

追加诊断表明：完整双向 `proxy` client 仍收不到 initialize 响应。本机 CLI 提供 `codex app-server daemon enable-remote-control`，其帮助说明会为未来启动以及当前 managed daemon 启用远程控制。该动作会持续改变 daemon 的客户端接入边界，因此尚未执行。下一步需要用户明确批准该命令；批准后先重复只读 initialize + `thread/list`，成功后才创建一个最小 C0 临时 thread 验证剩余项。全过程不重启 daemon，不触碰现有 Session。

复现所用只读命令：

```bash
codex --version
codex app-server daemon version
codex app-server --help
codex app-server generate-json-schema --experimental --out /tmp/c0-schema
stat -c '%A %a %U:%G %n' ~/.codex/app-server-control ~/.codex/app-server-control/app-server-control.sock
```
