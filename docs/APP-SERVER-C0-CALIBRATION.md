# App Server C0 协议校准

日期：2026-09-11（追加运行时校准）；目标版本：Codex CLI/App Server 0.154.0。本文是只读校准记录，不代表已实现 Gateway。

## 环境基线与证据

- `codex --version`：`codex-cli 0.154.0`。
- `codex app-server daemon version`（对现有 control socket 的只读请求）：`status=running`，CLI/App Server/managed Codex 均为 `0.154.0`；socket 为 `/home/feiyan/.codex/app-server-control/app-server-control.sock`。
- `stat`：control socket 所在目录 `0700`，socket `0600`，均属 `feiyan`。这是同一 OS 用户内的隔离，不是 VIMO owner/role 授权。
- `codex app-server --help` 证实有 `daemon start|restart|stop|version`、`proxy --sock`、`generate-json-schema --experimental`；本次未调用 start/restart/stop。
- 以 `generate-json-schema --experimental --out /tmp/c0-schema` 生成本机 0.154.0 schema（临时目录）。Schema 直接证实下述字段与枚举。
- 官方 [Codex App Server 文档](https://learn.chatgpt.com/zh-Hans/docs/app-server)确认 `thread/read/resume`、turn 生命周期、审批和 `serverRequest/resolved` 等公开语义；本机生成的 0.154.0 schema 用于校准当前安装版本的具体字段。当前 daemon 是 GUI/SSH 启动的 `codex ... app-server --listen unix://`（PID 208055，用户提供的进程证据），不是 `codex app-server daemon` managed instance。

## 事实矩阵

| 项目 | 证据/可复现命令 | 结论 |
|---|---|---|
| daemon 与 GUI 使用的版本 | `codex app-server daemon version` 返回 running、四个版本均 `0.154.0` | **已证实**当前 control socket 有正在运行的 daemon；`proxy` 是官方提供的连接入口。 |
| proxy 实际连接/initialize | 用户批准执行 `codex app-server daemon enable-remote-control`，返回 `app server is running but is not managed by codex app-server daemon`，未改变配置。随后直接 Unix WebSocket Upgrade（101）成功；完整 JSON-RPC client 的 `initialize`、`thread/list` 成功，并收到 `remoteControl/status/changed` | **已证实** GUI-managed 实例应走 direct Unix WebSocket；`codex app-server proxy` helper 不适配该启动拓扑，不能作为 transport 结论。 |
| thread/read | 双 WebSocket client 对同一 C0 ephemeral thread 执行 `thread/read(includeTurns=false)` 均可返回 metadata；`includeTurns=true` 对 ephemeral 明确报 `ephemeral threads do not support includeTurns` | **已证实**多 client metadata read 可用；ephemeral 不支持 full-history hydration，生产 Gateway 必须使用 durable thread 或 paginated history。 |
| thread/resume | 对 ephemeral thread 的后接入 client 返回 `no rollout found for thread id ...`；schema 仍证实可显式覆盖权限/cwd 等字段 | **已证实**ephemeral thread 不可作为跨 client resume 载体；durable thread 的并发 resume 仍未验证。 |
| 多 client subscription | schema 存在 `thread/status/changed`、`turn/started`、`turn/completed`、`serverRequest/resolved` 等通知，以及 `thread/unsubscribe` | **协议已证实有连接级事件流**；同一 thread 两 client 是否都收到完整事件、是否需各自 subscribe，**未验证**。 |
| 外部 client 启动 turn 的观察 | 在 C0 ephemeral thread 上由 client B 发出一次极短 `turn/start`；client A/B 未取得可归档的 `turn/started`/`turn/completed` 响应，连接关闭后 thread 变为 not loaded | **未验证**外部 writer 事件时序；本次未获得 turn ID，也未确认模型调用产生。停止扩大实验。 |
| 未决审批由后接入 client 响应 | `PermissionsRequestApprovalParams` 含 `threadId/turnId/itemId`；响应 scope 枚举 `turn|session`，schema 无 client ownership 字段 | **不能证明可跨 client 接管**。Phase 1 必须采用“打断后移交”，除非专门实验成功。 |
| daemon/Gateway 重连 | 未停止/重启任何服务；对 idle C0 WebSocket 断开后重新连接，ephemeral thread 后续 read 返回 `thread not loaded` | **仅证实** ephemeral runtime 不在新连接中保持 loaded；durable thread/turn/pending request 的恢复**未验证**。 |

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

已证实：查看可以建模为 `thread/read`；durable thread 可使用 `includeTurns=true`，ephemeral thread 明确不支持该参数。thread/read 不含 turn start 或 control 参数。协议有 `turn/interrupt`、`turn/steer` 与 terminal `turn/completed`，因此“由 Gateway 主动打断后移交”有明确 RPC 基础。

未证实：GUI/CLI 只读是否一定不改变 Gateway subscription；后接入 client 能否响应前一 client 的 pending approval；外部 active turn 的完整通知时序；daemon/Gateway 断线后 pending request 是否仍可由新连接处理。首版不要做“等待审批时无缝移交”。

## 对 Phase 1 接口的最小修订

1. transport adapter 必须直接支持 Unix WebSocket（GUI-managed 拓扑），实现连接初始化（`capabilities.experimentalApi=true`）、请求/响应关联、通知 fan-out、重连和未知状态；`codex app-server proxy` 只作为 daemon-managed 拓扑的可选 helper。
2. `thread/read` 与订阅是两个能力：history 读取不能代替事件订阅；每个连接应明确订阅/取消订阅及 cursor 策略。
3. 统一内部 `operationPolicy`，但 adapter 映射为 `permissions`（start/resume/turn/settings）和 `sandboxPolicy`（settings/turn）；永不同时发送互斥字段。
4. approval action 先支持当前 client 的 turn/session scope；跨 client pending request 未证实，handoff 默认 `interrupt → turn/completed(interrupted) → external`。
5. reconnect 后先重新 resume/read 并等待 status；`active` 或 pending 状态未知时进入恢复中，不提交新 turn。

## C0 判定与需用户决定

### C0 临时实验记录

- WebSocket 101、`initialize`、`thread/list`：已成功；通知包含 `remoteControl/status/changed`。
- C0 临时 cwd：`/tmp/better-subagent-c0`。实验产生两个 ephemeral thread ID：`01a08e30-6244-7ac1-8887-d1b02772a1e6`、`01a08e30-d08a-7fa3-ae4a-8235608953d9`。第二个 thread 的 metadata 曾返回 `status=idle`、`model=gpt-5.6-luna`、`reasoningEffort=low`；未获得可确认的 turn ID。两者均为 `ephemeral=true`，断开后变为 not loaded；cwd 未产生文件，临时探针脚本已清理。
- 两个独立 client 对第二个 thread 的 metadata `thread/read(includeTurns=false)` 均成功；`includeTurns=true` 和后接入 `thread/resume` 分别被 ephemeral 限制/报 no rollout。一次最小 `turn/start` 未取得可归档生命周期事件，未再重试，避免重复模型调用。
- 未进行审批实验：没有稳定 pending request，也没有扩大权限或触碰已有用户 thread；`acceptForSession` 跨 client 仍未验证。

**C0：有条件通过。** direct Unix WebSocket、initialize/thread/list、双 client metadata read、状态通知存在性已确认；ephemeral 限制和未验证项已明确。Phase 1 可冻结为：生产使用 durable thread；审批等待不做无缝跨 client 接管，采用 `interrupt → turn/completed(interrupted) → handoff`。跨 client approval、durable resume/turn 事件、重连恢复仍列为 C1 集成测试，不得在接口中宣称已验证。

复现所用只读命令：

```bash
codex --version
codex app-server daemon version
codex app-server --help
codex app-server generate-json-schema --experimental --out /tmp/c0-schema
stat -c '%A %a %U:%G %n' ~/.codex/app-server-control ~/.codex/app-server-control/app-server-control.sock
```
