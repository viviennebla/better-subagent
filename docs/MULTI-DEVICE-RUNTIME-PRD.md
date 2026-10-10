# Multi-device Better Subagent PRD

状态：draft for implementation handoff  
日期：2026-10-06  
基线：better-subagent `main@a5f55e6`

## 1. 背景

better-subagent 当前已经能够管理 Codex Session、单活 Run、steer、interrupt、审批、handoff/reclaim，并通过 App Server transport 控制本机 Codex runtime。

当前限制是：Gateway 与 App Server 默认位于同一台机器，Session、cwd、thread、runtime ownership 都隐含绑定到单机。

实际开发已经进入多设备模式：开发机负责实现，测试环境负责验收，部署环境负责运行正式服务。开发工作完成并触发自动部署后，需要把验证任务可靠地调度给测试环境 Agent，并将结果回报到 Board。

## 2. 产品目标

把 better-subagent 从“单机 Codex Session Gateway”演进为“多设备 Agent Runtime”。

目标闭环：

```text
dev Agent
  ↓ 完成开发
GitHub CI/CD
  ↓ test deployment healthy
Board 创建 Verification Task
  ↓ dispatch verifier@test
better-subagent
  ↓
test Device Agent
  ↓ local Codex App Server
Verifier 执行验收
  ↓
Ledger / Verification Report
  ↓
PASS → review
FAIL → needs_rework
```

better-subagent 只负责 runtime 调度与执行，不负责决定 Task 应进入 review 还是 needs_rework。

## 3. 核心原则

1. Board owns business truth：Task、Workflow、Ledger、业务状态。
2. better-subagent owns runtime truth：Device、Agent、Session、Run、approval、writer ownership。
3. Environment 与 Device 分离：
   - `test` 是调度语义；
   - `test-server-01` 是实际执行设备。
4. Run 创建后必须固定 target Device，后续 steer / approval / interrupt 都回到同一设备。
5. 跨设备执行采用 durable command + idempotent execution，不依赖一次 HTTP 请求是否刚好成功返回。
6. Device Agent 主动连接 Gateway，避免要求每台设备暴露入站端口。
7. MCP 可作为外部控制入口，但不替代内部 durable runtime protocol。
8. V1 先跑通真实 dev → test verification vertical slice，不建设通用集群调度平台。

## 4. 概念模型

**两种 Agent 不是一类对象。** 本节的 Device Agent 是机器侧执行进程（Registry 中的 `agentId`）；Codex Multi-Agent V2 的 Root/Subagent 是 App Server 管理的 Thread 树（`threadId`、`sessionTreeId`、`parentThreadId`）。不把 Codex Subagent 注册到设备 Agent 表，也不让设备 Agent 接管上游 Parent 的协作所有权。概念与能力映射见 [Codex Multi-Agent V2](./CODEX-MULTI-AGENT-V2.md)。


### Device

```text
deviceId
name
environment
platform
status
lastSeen
capabilities[]
agentVersion
```

status 至少：

```text
online
degraded
offline
```

### Agent

```text
agentId
deviceId
role
capabilities[]
enabled
```

V1 可以一台 Device 只有一个默认 Agent，但数据模型不要把 Agent 与 Device 合并。

### Session

沿用现有 Session contract，并新增：

```text
deviceId
agentId
environment
controlGeneration
```

Session 的 threadId / cwd / runtime policy 仍由 better-subagent 管理。

### Run

沿用现有 Gateway Run，并新增：

```text
targetDeviceId
targetAgentId
controlGeneration
commandId
```

Run 的 targetDeviceId 一旦创建不得被 Task / Session 后续修改覆盖。

## 5. V1 功能范围

### P0-1 Device Registry

Gateway 必须能够：

- 注册 Device；
- 更新 heartbeat；
- 标记 offline；
- 查询 Device / environment / capabilities；
- 防止两个活跃连接同时声明同一个 deviceId。

### P0-2 Device Agent Runtime

每台执行机器运行一个轻量 Device Agent：

- 维护稳定 deviceId；
- 主动连接 Gateway；
- 连接本机 Codex App Server；
- 上报本机 Agent / Session inventory；
- 接收 start / steer / interrupt / approval command；
- 上报 Run 事件；
- 断线后自动重连。

### P0-3 Cross-device Session

Session 必须明确归属 Device。

Board 仍然可以按 Session 调度；better-subagent 负责把 Session 路由到正确 Device Agent。

若 Session 所属 Device offline：

- Session 不可调度；
- 返回明确 unavailable reason；
- 不自动改绑其他 Device；
- 不重放已有 Prompt。

### P0-4 Durable Work Delivery

start run 不再依赖一次同步远程调用完成。

Gateway 需要 durable command queue，至少覆盖：

- start;
- steer;
- interrupt;
- approval decision.

delivery 语义：

```text
at-least-once delivery
+
commandId idempotency
```

同一 commandId 重试不得创建第二个 Turn。

### P0-5 Runtime Event Return

Device Agent 必须可靠回传：

- command accepted / rejected；
- run started；
- approval requested / resolved；
- run completed / failed / interrupted；
- transport unknown；
- control mode changed。

Gateway 重启或 Device 短暂断线后不能永久丢失 terminal event。

### P0-6 Existing Board Compatibility

V1 必须保留现有主要接口语义：

```text
GET  /v1/sessions
GET  /v1/sessions/{id}
POST /v1/runs
GET  /v1/runs/{id}
POST /v1/runs/{id}/steer
POST /v1/runs/{id}/interrupt
POST /v1/approvals/{id}/decision
POST /v1/sessions/{id}/handoff
POST /v1/sessions/{id}/reclaim
```

新增字段优先 additive。

Board 不应因为多设备改造被迫同时大重写。

### P0-7 Environment-aware Discovery

Gateway 能按 environment / role / capability 查询 Agent / Session。

V1 不要求自动负载均衡。Board / Workflow Controller 可以明确选择 verifier@test 对应的 Session。

### P0-8 Verification Workflow Support

better-subagent 必须支持以下业务链路所需的 runtime 能力：

```text
Verification Task
→ verifier Session @ test
→ Run
→ terminal
→ Agent 回报 Ledger
```

better-subagent 不负责：

- 自动创建 Verification Task；
- 根据 PASS/FAIL 修改 Task status；
- 判断 deployment 是否成功。

这些属于 Codex Family / Board workflow。

## 6. MCP 范围

MCP 是对外能力入口，不是 Gateway ↔ Device Agent 内部协议。

V1 可预留：

```text
device_list
agent_list
session_list
run_get
run_start
run_interrupt
```

但 MCP facade 不应阻塞 P0 vertical slice。

内部跨设备通信应使用可恢复的 command/event protocol。

## 7. 非目标

V1 明确不做：

- Kubernetes 式 scheduler；
- CPU/GPU 自动资源分配；
- Session live migration；
- 自动复制 worktree；
- 自动搬迁 Codex thread；
- 多 Gateway HA；
- Kafka / RabbitMQ / NATS；
- Browser 直接访问 Device Agent；
- 任意远程 shell MCP；
- Board 业务规则下沉到 better-subagent；
- production approval workflow。

## 8. 可靠性要求

以下场景必须有确定行为：

1. Gateway 写入 command 后立即重启；
2. Device 收到 command 后 ACK 丢失；
3. Turn 已启动但 Gateway 没收到响应；
4. Device 执行过程中离线；
5. Device 重连；
6. terminal event 在断线期间产生；
7. 同一 commandId 重复发送；
8. Session 在 command 排队期间 handoff / reclaim；
9. Session rebind 后旧 command 到达；
10. Gateway 与 Device 对 Run 状态不一致。

禁止通过“自动再次发送 Prompt”解决 unknown outcome。

## 9. 安全边界

V1 假设部署在可信私有网络 / overlay network 中。

最低要求：

- Device Agent 主动向 Gateway 建立连接；
- Device identity 不能由业务 Prompt 决定；
- Browser 不能提交任意 Gateway / Device endpoint；
- Device 注册凭据与 Codex thread/session identity 分离；
- 不把 App Server socket 暴露到网络；
- 远程协议只暴露明确 runtime command，不提供通用 shell proxy。

不建设多租户 RBAC。

## 10. 首个产品验收场景

必须用真实两设备链路验收：

```text
Device A: environment=dev, role=coder
Device B: environment=test, role=verifier
```

流程：

1. 两台 Device Agent 同时 online；
2. Gateway 能列出两台 Device 和各自 Session；
3. Board 对 dev Session 的已有调度行为不回归；
4. 创建 Verification Task；
5. dispatch 到 verifier@test Session；
6. Run 固定 targetDeviceId=Device B；
7. Device B 的 Codex App Server 启动对应 Turn；
8. Turn 完成；
9. Gateway Run 收敛 terminal；
10. Agent 能按现有 Ledger 方式回报；
11. Device B 临时断线后重复上述流程仍可恢复。

## 11. 成功标准

V1 完成条件：

- 单设备现有 better-subagent contract 全部回归通过；
- 两设备同时 online；
- 跨设备 start / steer / interrupt / approval 可路由；
- Gateway / Device Agent 任一侧重启不造成重复 Turn；
- offline Device 不被调度；
- terminal event 不因短时断线永久丢失；
- existing Board adapter 无需大改即可工作；
- dev → test verification vertical slice 真实通过。

## 12. 实施优先级

```text
Phase 0  独立仓库 / CI / deploy ownership 校准
Phase 1  SQLite + 单设备行为等价
Phase 2  Device Registry + 本机 Device Agent
Phase 3  第二台真实 Device + durable command/event
Phase 4  Board environment projection + verification vertical slice
Phase 5  MCP facade
```

不要跳过 Phase 1 直接做跨设备网络层。
