# Multi-device Better Subagent 技术方案

状态：implementation design draft  
日期：2026-10-06  
对应 PRD：[`MULTI-DEVICE-RUNTIME-PRD.md`](./MULTI-DEVICE-RUNTIME-PRD.md)  
基线：better-subagent `main@a5f55e6`

## 1. 当前基线

当前进程：

```text
Board
  ↓ HTTP
better-subagent Gateway :1999
  ↓ local App Server transport
Codex App Server
  ↓
Thread / Turn
```

现有 Gateway 已具备：

- Session registry；
- single-active Run guard；
- requestId idempotency；
- start / steer / interrupt；
- approval；
- handoff / reclaim；
- managed / external control mode；
- App Server exact thread / turn identity；
- restart 后 orphan Run reconciliation；
- unknown outcome fail closed。

当前主要限制：

- runtime store 是单个 JSON 文件；
- Gateway 与 App Server 隐含同机；
- 无 Device 一等模型；
- 无跨设备 durable command delivery；
- terminal 回调依赖当前进程连接；
- 无 Agent heartbeat / offline routing。

## 2. 目标拓扑

```text
                         Board
                           |
                           | existing HTTP contract
                           v
                better-subagent Coordinator
                ---------------------------
                Device Registry
                Session Registry
                Run Store
                Command Queue
                Runtime Event Journal
                           |
                 outbound persistent links
                  /                    \
                 v                      v
       Device Agent @ dev      Device Agent @ test
       ------------------      -------------------
       local identity           local identity
       command dedupe           command dedupe
       event outbox             event outbox
       App Server transport     App Server transport
              |                        |
              v                        v
        Codex App Server          Codex App Server
```

Coordinator 是 Board 唯一访问的 runtime endpoint。

Board 不直接知道 Device Agent URL。

## 3. 进程拆分

### Coordinator

沿用 `better-subagent` 服务入口，逐步改造成 Coordinator。

职责：

- 对 Board 保留现有 `/v1/sessions` / `/v1/runs` API；
- 管 Device / Agent / Session / Run；
- 持久化 command；
- 接收 Device event；
- 维护 runtime projection；
- 对 Device Agent 路由 control command。

### Device Agent

新增进程建议：

```text
better-subagent-agent
```

职责：

- 加载稳定 device identity；
- 主动连接 Coordinator；
- heartbeat；
- 维护本机 App Server transport；
- 解析本机 Session runtime；
- 执行 Coordinator command；
- 本地幂等；
- 本地 event outbox；
- reconnect / replay。

Device Agent 不保存 Board Task 或 Ledger。

## 4. 持久化设计

V1 不引入外部数据库。

Coordinator 使用 Python 标准库 SQLite：

```text
data/runtime.sqlite3
```

建议 WAL mode。

### 4.1 devices

```sql
device_id TEXT PRIMARY KEY
name TEXT NOT NULL
environment TEXT NOT NULL
platform TEXT
status TEXT NOT NULL
capabilities_json TEXT NOT NULL
agent_version TEXT
connection_generation INTEGER NOT NULL
last_seen_at TEXT
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

### 4.2 agents

```sql
agent_id TEXT PRIMARY KEY
device_id TEXT NOT NULL
role TEXT NOT NULL
capabilities_json TEXT NOT NULL
enabled INTEGER NOT NULL
updated_at TEXT NOT NULL
```

### 4.3 sessions

保留现有 Session 字段并增加：

```sql
session_id TEXT PRIMARY KEY
device_id TEXT NOT NULL
agent_id TEXT
environment TEXT NOT NULL
thread_id TEXT NOT NULL
cwd TEXT NOT NULL
control_mode TEXT NOT NULL
runtime_status TEXT NOT NULL
control_generation INTEGER NOT NULL
runtime_json TEXT NOT NULL
updated_at TEXT NOT NULL
```

requested/effective policy 可先保留在 `runtime_json`，无需第一轮拆满所有列。

### 4.4 runs

```sql
gateway_run_id TEXT PRIMARY KEY
request_id TEXT NOT NULL UNIQUE
session_id TEXT NOT NULL
target_device_id TEXT NOT NULL
target_agent_id TEXT
control_generation INTEGER NOT NULL
status TEXT NOT NULL
prompt_hash TEXT NOT NULL
transport_turn_id TEXT
error TEXT
created_at TEXT NOT NULL
started_at TEXT
terminal_at TEXT
updated_at TEXT NOT NULL
```

Prompt 正文不要求长期保存在 runs；command payload 可在 terminal 后按保留策略清理。

### 4.5 commands

```sql
command_id TEXT PRIMARY KEY
gateway_run_id TEXT
session_id TEXT NOT NULL
target_device_id TEXT NOT NULL
control_generation INTEGER NOT NULL
type TEXT NOT NULL
payload_json TEXT NOT NULL
state TEXT NOT NULL
attempt INTEGER NOT NULL
lease_generation INTEGER
leased_until TEXT
last_error TEXT
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

状态：

```text
queued
leased
accepted
succeeded
failed
cancelled
stale
```

command 与 Run 状态不要合并成一列。

### 4.6 runtime_events

```sql
event_id TEXT PRIMARY KEY
device_id TEXT NOT NULL
device_sequence INTEGER NOT NULL
gateway_run_id TEXT
session_id TEXT
type TEXT NOT NULL
payload_json TEXT NOT NULL
created_at TEXT NOT NULL

UNIQUE(device_id, device_sequence)
```

用于 dedupe 和 replay。

## 5. Device Agent 本地状态

每台 Device Agent 使用轻量 SQLite：

```text
~/.better-subagent/agent.sqlite3
```

最小内容：

```text
identity
processed_commands
event_outbox
```

### identity

稳定：

```text
deviceId
deviceSecret/reference
environment
```

deviceId 不应随进程重启变化。

### processed_commands

保存最近 commandId 与 execution identity，用于 Coordinator 重发后的幂等返回。

### event_outbox

runtime event 在本地先持久化，再发送 Coordinator；收到 Coordinator ACK 后删除/压缩。

这样：

```text
Turn completed
→ Device Agent 写 event_outbox
→ 网络断开
→ reconnect
→ replay event
```

terminal 不丢。

## 6. Coordinator ↔ Device Agent 协议

内部协议不使用 MCP。

推荐一个长期 outbound WebSocket：

```text
Device Agent → Coordinator
```

Device 不需要开放入站端口。

### 6.1 hello

```json
{
  "type": "hello",
  "protocolVersion": 1,
  "deviceId": "test-server-01",
  "environment": "test",
  "platform": "linux",
  "capabilities": ["codex", "verification", "logs"],
  "agentVersion": "0.2.0"
}
```

Coordinator 返回 connectionGeneration。

同 deviceId 新连接建立后，旧 connectionGeneration 失效。

### 6.2 heartbeat

建议 10 秒一次。

Coordinator 30 秒未观察到 heartbeat 后标记 offline。

具体时间做配置，不作为协议字段。

### 6.3 command

```json
{
  "type": "command",
  "commandId": "cmd-...",
  "commandType": "start",
  "sessionId": "...",
  "controlGeneration": 17,
  "payload": {}
}
```

### 6.4 command ack

```json
{
  "type": "commandAck",
  "commandId": "cmd-...",
  "status": "accepted | rejected | duplicate",
  "runtimeRef": {}
}
```

ACK 只说明 Device Agent 已接管 command，不表示 Run terminal。

### 6.5 runtime event

```json
{
  "type": "runtimeEvent",
  "eventId": "evt-...",
  "deviceSequence": 1203,
  "eventType": "run.started",
  "gatewayRunId": "run-...",
  "sessionId": "...",
  "payload": {}
}
```

Coordinator ACK：

```json
{
  "type": "eventAck",
  "deviceSequence": 1203
}
```

## 7. Command 生命周期

### start

Board 调：

```text
POST /v1/runs
```

Coordinator transaction：

1. 校验 Session / Device；
2. requestId 幂等检查；
3. 创建 Gateway Run `starting`；
4. 创建 start command `queued`；
5. commit；
6. 尝试实时投递；
7. 返回稳定 gatewayRunId。

若 Device offline：

- Run 保持 `starting` 或更明确的 `queued` projection；
- command 保持 queued；
- 不创建第二 Run；
- Device online 后继续 delivery。

是否对 Board 暴露 `queued` 作为新状态需要兼容评估。若保持旧合同，可先映射为 `starting`，detail 中增加 deliveryState。

### Device accepted

收到 commandAck：

```text
command → accepted
Run 仍 starting
```

只有 App Server 报告 authoritative turn.started 后：

```text
Run → active
```

### terminal

Device Agent 先持久化 event，再发送：

```text
run.completed
run.failed
run.interrupted
```

Coordinator transaction：

- event dedupe；
- Run terminal；
- command terminal；
- Session runtime projection 更新；
- event cursor 更新。

## 8. Idempotency

### Coordinator

`requestId` 继续是 StartRun 的业务幂等键。

```text
same requestId + same sessionId + same promptHash
→ same Gateway Run
```

不同 payload：

```text
409 request_id_conflict
```

### Device Agent

`commandId` 是内部执行幂等键。

若重复 command：

- 不再次调用 App Server；
- 返回原 commandAck；
- 若已有 transportTurnId，一并返回。

## 9. controlGeneration

每个 Session 保存递增 `controlGeneration`。

在以下事件发生时增加：

- Session 重新绑定 Device；
- managed → external；
- external → managed；
- thread identity 被替换；
-管理员显式 reset runtime ownership。

command 创建时冻结 generation。

Device Agent 执行前必须验证：

```text
command.controlGeneration == current Session generation
```

否则：

```text
rejected: stale_generation
```

这用于阻止排队中的旧 command 在 handoff / rebind 后落到新的 writer 上。

## 10. App Server ownership

每个 Device Agent 只连接本机 App Server。

Coordinator 永远不直接访问远程 App Server socket。

现有：

```text
AppServerTransport
```

应尽量下沉到 Device Agent 复用，不重写 RPC 校准逻辑。

Device Agent 将 App Server 原始事件归一化为 runtime event 后再发 Coordinator。

## 11. Session discovery

Device Agent 周期性/事件驱动读取本机 App Server thread inventory，并上报 Coordinator。

Coordinator 的 `GET /v1/sessions` 仍输出可调度 Session，但新增：

```json
{
  "deviceId": "test-server-01",
  "environment": "test",
  "agentId": "verifier@test-server-01"
}
```

Browser 不提交 device endpoint。

## 12. Offline / reconnect

### Device offline before command delivery

```text
command queued
Run starting
Session unavailable
```

不失败，不重放 Prompt。

### Device offline after ACK before turn.started

结果未知。

Run 应 fail closed：

```text
unknown
```

Device reconnect 后可按 exact command / App Server turn identity reconcile。

禁止自动创建第二个 Turn。

### Device offline during active Run

Coordinator：

```text
Run 保持 active/unknown projection
Device offline
```

重连后 Device Agent：

1. read exact thread / turn；
2. 找 terminal fact；
3. replay terminal event；
4. 若无法证明，维持 unknown。

## 13. Board 合同兼容

Board 在 Phase 1～3 不应需要理解 Device protocol。

现有 Gateway HTTP API 保持。

Board 可逐步消费 additive metadata：

```text
Session.deviceId
Session.environment
Run.targetDeviceId
Device availability
```

业务层不保存 heartbeat。

Verification Task 的自动创建与 PASS/FAIL workflow 不属于本仓库。

## 14. MCP facade

MCP 建议在 core runtime 稳定后实现。

MCP server 直接调用 Coordinator application service，不复制状态。

初始工具：

```text
device_list
agent_list
session_list
run_get
run_start
run_interrupt
```

MCP tool call 不承担 command delivery durability。

## 15. HTTP / WebSocket 服务边界

建议：

```text
:1999
├─ existing Board HTTP API
├─ /v1/devices
├─ /v1/agents
└─ /v2/agent/connect   WebSocket
```

V1 不额外开多个公开端口。

若 WebSocket 实现复杂，可先使用 long-poll command + POST event 做 Phase 2 calibration，但最终协议仍需 outbound persistent link。

## 16. 迁移阶段

### Phase 0 — repo / deployment ownership

目标：better-subagent 正式成为 Codex Family 独立 submodule 和独立 deploy component。

不改变运行行为。

### Phase 1 — SQLite single-device parity

把当前 JSON registry / Run store 迁到 Coordinator SQLite。

仍只有本机 App Server。

验收：

- 所有现有 tests 通过；
- restart / idempotency 语义不变；
- JSON 可一次性迁移；
- 无跨设备逻辑。

### Phase 2 — local Device Agent

实现说明：Phase 2 先建立 Coordinator → LocalDeviceAgent → AppServerTransport 的明确代码 ownership boundary 和 Device / Agent durable model。LocalDeviceAgent 与 Coordinator 暂时同进程，不为了本机拆进程提前增加一套临时 IPC；Phase 3 引入 durable remote command/event protocol 时再把该 boundary 进程化。

逻辑拓扑仍为：

```text
Coordinator
Device Agent
App Server
```

Coordinator 不再直接创建 AppServerTransport。

验收现有 start/steer/interrupt/approval/handoff/reclaim 全通过。

### Phase 3 — remote second Device

接入真实 test Device。

实现：

- heartbeat；
- durable command；
- event outbox；
- reconnect；
- exact Device routing。

### Phase 4 — Board verification vertical slice

Board 增加 environment/device projection。

跑通：

```text
Verification Task
→ verifier@test
→ remote Run
→ Ledger report
```

### Phase 5 — MCP

在 runtime core 稳定后再提供 MCP facade。

## 17. 测试矩阵

必须新增以下 integration tests：

| Case | Expected |
| --- | --- |
| duplicate start command | one App Server Turn |
| Coordinator restart with queued command | command survives |
| Coordinator restart after Device ACK | no duplicate Turn |
| Device restart before ACK | redelivery + idempotent |
| Device disconnect after turn.started | no automatic replay |
| terminal while disconnected | event outbox replays |
| same deviceId second connection | old generation invalidated |
| Session handoff while command queued | stale command rejected |
| Session rebind Device | old generation rejected |
| offline target Device | Session unavailable, queue durable |
| remote approval | routed to original target Device |
| interrupt | routed to original target Device |
| Coordinator event duplicate | one state transition |
| App Server exact Turn missing after reconnect | fail closed unknown |

## 18. 代码组织建议

保持当前包名，逐步增加：

```text
better_subagent/
├─ coordinator.py
├─ device_agent.py
├─ storage.py
├─ protocol.py
├─ contracts.py
├─ gateway.py
├─ server.py
└─ transport.py
```

不要第一轮创建大量抽象接口。

建议边界：

- `storage.py`：SQLite transaction；
- `protocol.py`：Coordinator ↔ Device wire schema；
- `device_agent.py`：local App Server + command/event；
- `coordinator.py`：routing / leasing / projection；
- `gateway.py`：现有业务 API adapter，逐步变薄。

## 19. 实施纪律

另一个开发 chat 开工时必须遵守：

1. 先完成 Phase 0/1，不直接编码 remote WebSocket；
2. 每阶段保持现有 Board contract 可用；
3. unknown outcome 永远 fail closed；
4. 不自动重放 Prompt；
5. 不把 Device endpoint 交给 Browser；
6. 不在 Board 复制 runtime truth；
7. 不引入 Redis / Kafka / RabbitMQ；
8. 先用真实第二台 test Device 做 calibration，再扩展调度策略；
9. 每阶段单独 PR；
10. 每个 PR 都补 restart / duplicate / disconnect 测试。

## 20. 第一张开发任务

建议另一个 chat 从这一项开始：

**Phase 1：SQLite single-device parity**

交付：

- schema / migration；
- JSON → SQLite 一次性迁移；
- Session / Run / approval / idempotency 全部改为 SQLite transaction；
- 当前 HTTP contract 不变；
- 全量现有 tests 通过；
- 新增 Coordinator restart / duplicate request / orphan Run 测试；
- 不引入 Device Agent。

只有这个 PR 合并后，再开始 Phase 2。
