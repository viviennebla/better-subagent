# better-subagent App Server 下一轮开发计划

状态：**用户已批准；Phase 0 等待 remote-control 显式授权**

日期：2026-09-10

批准记录：用户于 2026-09-10 确认按本方案开始；执行采用逐阶段校准，并优先控制 Agent token 消耗。

基线：better-subagent `0.1`、Codex CLI / App Server `0.154.0`

设计输入：[`APP-SERVER-PERMISSIONS-INVESTIGATION.md`](./APP-SERVER-PERMISSIONS-INVESTIGATION.md)。本文采用其中“宽基础权限 + 可控提权”的结论，并将其收敛为下一轮可实现的 Gateway 合同。

## 1. 本轮目标

把 better-subagent 从“每个 Run 启动一个 SDK worker”迁移为“连接已有 Codex App Server 的 Session Gateway”，跑通以下最小闭环：

1. Board 可以读取 Session 的真实运行状态和完整历史；
2. Board 或 Orchestrator 可以启动、steer、打断 turn；
3. 用户可以在 Board 处理运行时审批；
4. 用户可以随时通过 CLI/GUI 查看 Session，并可显式接管或交还控制权；
5. Board 不会因为用户人工接入而继续错误调度同一个 Session。

本轮优先级是：**可用性 > 可靠性 > 完整治理**。先保证开发流程顺畅，不建设企业级权限、分布式锁或复杂恢复系统。

## 2. 已冻结的产品原则

### 2.1 人工接入是正常能力

- 同一 Session 可以被 Board、CLI 和 GUI 查看。
- 查看历史不等于接管；Board 使用 `thread/read(includeTurns=true)` 做只读展示。
- 用户始终有权从 CLI/GUI 继续工作。Gateway 的控制状态用于协调和避免误调度，不作为阻止用户操作的安全锁。
- Gateway 发现外部客户端已经启动 turn 时，自动让出写入并把 Session 标记为 `external`，不把这种情况当作系统故障。
- 任意时刻只应有一个活跃 turn；这里防止的是意外双写，不限制用户的最终控制权。

### 2.2 权限默认满足开发需要

默认开发策略为：

```text
permission profile: vimo-development
approval policy: on-request
approvals reviewer: auto_review
workspace: 绑定仓库与 worktree 可写
network: 开发网络可用
session approval: 允许 acceptForSession
```

`vimo-development` 应覆盖编辑代码、运行测试/构建、安装依赖、访问开发与测试服务、写临时目录以及本地 Git add/commit。用户可以为可信本机会话显式选择 full access。

本轮不做以下收紧：

- 不为普通开发命令建立细粒度白名单；
- 不默认使用 read-only；
- 不要求每条命令都由用户审批；
- 不实现多租户 RBAC、账号系统、token 轮换或隐私分级；
- 不阻止用户从本机 CLI/GUI 直接恢复 Session。

Browser 仍只调用明确的业务动作，不获得任意 App Server RPC 代理。这是为了保持接口简单，避免网页参数与 runtime 配置互相污染，不是建立复杂安全系统。

#### 2.2.1 权限能力合同

Gateway 对外使用统一的 `operationPolicy`，吸收 App Server 在不同 RPC 中使用 `permissions`、`permissionProfile`、`sandboxPolicy` 等字段名的差异：

```json
{
  "preset": "development",
  "permissionProfileId": "vimo-development",
  "approvalPolicy": "on-request",
  "approvalsReviewer": "auto_review",
  "allowSessionApproval": true,
  "runtimeWorkspaceRoots": ["/canonical/bound/worktree"]
}
```

首版只提供三个用户可理解的预设，不做权限规则编辑器：

| 预设 | 用途 | 行为 |
| --- | --- | --- |
| `development` | coder/reviewer 日常工作，默认 | 仓库与构建目录可写、开发网络可用、常规提权由 auto-review 处理 |
| `workspace` | 用户希望临时收敛能力 | 工作区可写，其余按 App Server profile 和审批处理 |
| `full-access` | 用户明确选择的可信本机操作 | 使用宿主允许的 full access；不会由 Prompt、报告或自动路由选择 |

Session registry 同时记录：

- `requestedPolicy`：用户或 Orchestrator 选择的预设；
- `effectivePolicy`：App Server 实际返回的 profile、sandbox 和审批设置；
- `policySource`：`default | user | orchestrator`；
- `policyUpdatedAt`：最近一次确认生效的时间。

规则保持宽松且明确：

1. 新 Session 默认继承 `development`，不按 investigator/coder/reviewer 角色自动降权；
2. 用户可在 Session 侧栏随时切换预设，运行中变更通过 `thread/settings/update` 提交；收到 `thread/settings/updated` 后才更新 `effectivePolicy`；
3. 如果宿主 `requirements.toml` 不允许请求的 profile，展示实际 effective policy 和原因，不反复弹出逐命令审批；
4. `acceptForSession` 是正常按钮，不隐藏在高级设置中；只影响当前 Codex Session，不写成全局永久授权；
5. `external` 模式下由 CLI/GUI 控制本轮权限，Gateway 只观察 effective state，不在后台覆盖；reclaim 后才重新应用 registry 的 requested policy；
6. Gateway 每次冷 resume 都显式提交 Session 的 requested policy、cwd 和 runtime workspace roots，避免因进程重启退回更窄默认值；
7. App Server 宿主要求始终是能力上限。Gateway 不尝试绕过该上限，但允许用户直接选择宿主已经允许的 profile。

权限管理的目标是减少无意义阻塞并让实际权限可见，不是替代 Codex/App Server 自身的 sandbox 或审批实现。

### 2.3 事实源边界

| 概念 | 权威来源 | 其他模块职责 |
| --- | --- | --- |
| thread/turn、运行状态、待审批请求 | Codex App Server | Gateway 订阅并归一化 |
| Session 配置、当前控制方式、Gateway Run | better-subagent | Ledger 只保存引用和投影 |
| Task、stage、报告、人工调度决定 | Web Ledger | Gateway 不推断业务路线 |
| 角色流程和 Orchestrator 操作规则 | workflow skill | 不保存或伪造 runtime 状态 |

因此控制权不能只写进 workflow skill，也不应让 Ledger 成为 App Server 状态的第二事实源。

## 3. 最小运行模型

Session 只增加三个核心运行字段：

```json
{
  "controlMode": "managed | external",
  "runtimeStatus": "notLoaded | idle | active | waitingOnApproval | systemError",
  "activeTurnId": "turn-id-or-null"
}
```

- `managed`：Gateway 可以 dispatch、steer、interrupt 和响应审批。
- `external`：用户正在 CLI/GUI 控制；Gateway 只观察，Board 禁止对该 Session 再 dispatch。
- 不引入租约、锁续期、抢占 token 或复杂的 owner 树。
- 若检测到 Session 已有非 Gateway 发起的 active turn，直接投影为 `external`。

Run 继续保存 `gatewayRunId`、`requestId`、`sessionId` 和终态。App Server 的真实 `threadId/turnId` 由 Gateway 关联，不由 Browser 提交。

## 4. 人工查看与控制权移交

### 4.1 只读查看

Gateway 提供历史读取接口，内部调用 `thread/read(includeTurns=true)`。只读查看不改变 `controlMode`，也不阻止 Board 继续管理 Session。

用户也可以在 GUI/CLI 中 resume 后只查看记录。只要没有启动新 turn，Gateway 不应把它误判为接管。

### 4.2 显式移交

Board 提供“移交到 CLI/GUI”：

1. Session idle 时立即切换为 `external`；
2. Session active 时先调用 `turn/interrupt`，收到 `turn/completed: interrupted` 后切换；
3. Session 正在等待审批时，默认建议直接在 Board 审批；用户仍可选择“打断并移交”；
4. 页面显示 Session UUID，用户自行在 CLI/GUI resume，同一 thread 的历史保留。

首版不假设另一个 App Server client 能接管已经发送给原 client 的未决 JSON-RPC 审批请求。如果 Phase 0 校准证明可以安全转移，再增加“不中断当前 turn 的无缝移交”。

### 4.3 外部直接接入

显式移交是推荐路径，但不是硬门禁。如果用户直接在 GUI/CLI 启动 turn：

- Gateway 从 App Server 事件发现该 active turn；
- 将 Session 调整为 `external`；
- Ledger 停止继续调度，并显示“外部客户端控制”；
- 不自动打断用户的 turn，也不把 Task 标记为完成或失败。

### 4.4 交还 Board

用户完成外部操作后点击“交还 Board”。Session 已 idle 时恢复为 `managed`；若仍 active，只提示当前状态，不强制抢占。交还不会自动重放 Prompt。

## 5. Gateway API 增量

保持现有 `/v1/sessions` 和 `/v1/runs` 合同，增加最少接口：

| 接口 | 用途 |
| --- | --- |
| `GET /v1/sessions/{sessionId}` | Session runtime 与控制状态 |
| `GET /v1/sessions/{sessionId}/history` | 完整 thread 历史 |
| `POST /v1/runs/{runId}/steer` | 向当前 turn 追加用户说明 |
| `POST /v1/runs/{runId}/interrupt` | 通过 App Server 打断 |
| `POST /v1/approvals/{requestId}/decision` | `accept/acceptForSession/decline/cancel` |
| `POST /v1/sessions/{sessionId}/handoff` | 打断后移交或 idle 直接移交 |
| `POST /v1/sessions/{sessionId}/reclaim` | idle 后交还 Gateway |
| `PUT /v1/sessions/{sessionId}/operation-policy` | 用户切换权限预设并读取 requested/effective 结果 |

网页继续使用业务化接口，不提交 cwd、model、permission profile、任意 RPC method 或 shell。Session 的默认运行参数由 Gateway registry 提供；用户修改权限档位走独立的 Session 设置操作，不混入 dispatch 表单。

## 6. 模块拆分与并行开发

### Phase 0：协议校准，先验收再编码

负责人：investigator / architect

产物：可重复的本机探测脚本与结果表。

验证 Codex `0.154.0`：

1. better-subagent 能否通过 `codex app-server proxy` 连接当前 GUI 已使用的 daemon；
2. 两个 client 对同一 thread 的订阅、`thread/read` 和 `thread/resume` 行为；
3. 外部 client 启动 turn 后另一个 client 收到的状态事件；
4. 未决审批请求能否由后接入 client 响应；
5. daemon 或 Gateway 重启后 thread、turn 和 pending request 的恢复表现；
6. `vimo-development` profile 与 `acceptForSession` 的真实生效行为。

**Calibrate Gate C0：** 用户审阅事实表，确定“等待审批时无缝移交”还是“打断后移交”。在此之前不冻结相关接口语义。

### Phase 1：App Server transport 与 Gateway runtime

负责人：better-subagent coder

依赖：C0。

- 新建长期 App Server client，完成 initialize、订阅和请求/响应关联；
- 以 transport adapter 替换每 Run 一个 SDK worker；
- 接入 `thread/read/resume`、`turn/start/steer/interrupt` 和 terminal 事件；
- 接入审批请求与 `serverRequest/resolved`；
- 加入 `managed/external` 判断和外部 active turn 自动让出；
- 保留 `sdk-worker` feature flag 作为一轮回退，不新增长期双实现维护承诺。

**Calibrate Gate C1：** 不接 Board，直接演示同一真实 Session 的 history、start、steer、interrupt、approval 和外部接入识别。

### Phase 2：三个模块并行接入

#### A. Ledger domain / adapter

- Task 只记录 `gatewayRunId` 和 Session 控制状态投影；
- `external` Session 不出现在可调度列表；
- 加入 handoff、reclaim、steer、approval 的 Board action；
- Gateway 状态与 Ledger 不一致时以 Gateway/App Server runtime 为准刷新，不自动改变业务 stage。

#### B. Web UI

- Sessions 列表放入现有侧栏；
- Session 详情、完整历史和时间线使用弹窗/抽屉，不放到页面底部；
- `waitingOnApproval` 显示宽松的决策按钮，包括“允许本次”和“本 Session 允许”；
- 进行中任务提供 steer、打断和移交；
- `external` 明确显示“CLI/GUI 控制中”和“交还 Board”。

#### C. workflow skill 与文档

- 明确“查看不等于接管”；
- Orchestrator 只把 idle/managed Session 用于新调度；
- external 状态由 Gateway 事实触发，不由 Agent 文本报告生成；
- workflow skill 可以建议移交或交还，但不能直接篡改 control state。

**Calibrate Gate C2：** 用户在网页完成一次真实流程：调度 → steer → Session 级批准 → CLI/GUI 接管 → 交还 → 再调度。

### Phase 3：一次性切换与体验验收

负责人：integrator / reviewer。

- 无进行中 Run 时停止旧 SDK worker transport；
- 启用 App Server transport，并保留一条配置级回退路径；
- 更新 user systemd unit 和 Board Gateway 地址；
- 运行自动测试和一轮真实 Session 冒烟；
- 用户完成最终 calibration 后再删除旧 worker 依赖。

**Calibrate Gate C3：** 连续使用一轮后确认状态、审批、人工接管和 UI 均可用，再宣布迁移完成。

## 7. 验收条件

1. Board 展示的 active/idle/waiting 状态来自 App Server，不依赖进程 PID 或 Agent 自报；
2. CLI/GUI 只读查看不会让 Board 异常；
3. 用户直接从外部启动 turn 时，Board 在下一次同步后停止新调度且不打断用户；
4. Board 可对当前 turn steer、interrupt，并能处理 `acceptForSession`；
5. 显式 handoff 后相同 Session 的完整历史可在 CLI/GUI 继续；
6. reclaim 只在 idle 时生效，不重放旧 Prompt；
7. 默认 coder 权限足以连续完成编码、测试、依赖安装和本地 commit；
8. 页面能看到 requested/effective policy，用户可切换 `development/workspace/full-access`；
9. `acceptForSession` 可用，重复的同类开发操作不再逐次等待；
10. 外部 GUI/CLI 控制期间 Gateway 不覆盖该客户端选择的权限，交还后恢复 registry policy；
11. Gateway 重启后至少能从 App Server 重新识别 runtime 状态，不把未知状态伪造为 done；
12. 现有 Ledger Task/StageReport 数据无需迁移或重写。

## 8. 明确不做

- 企业级登录、RBAC、多租户隔离；
- 分布式锁、消息队列、HA 和跨主机 Session 调度；
- 对每条 shell 命令做规则引擎；
- 自动 merge、deploy 或生产环境写入；
- 在 C0 未验证前实现未决审批的跨 client 无缝转移；
- 重写 Ledger 的 Task 模型或把 workflow 固化成状态机。

## 9. Review 时需要确认的两项

1. 默认采用 `vimo-development + on-request + auto_review + acceptForSession`；
2. 若 C0 不能证明未决审批可跨 client 接管，首版采用“打断当前 turn 后移交”。

除这两项外，其余阶段可按上述模块边界直接拆任务执行。
