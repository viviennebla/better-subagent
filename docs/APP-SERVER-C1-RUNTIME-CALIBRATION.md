# App Server C1 Runtime Calibration

日期：2026-09-11；commit：`f7c4f1f`（`feat: add app-server session transport`）。本次只验证真实 0.154.0 runtime，不修改产品代码。

## 范围与安全边界

使用现有 GUI-managed Unix WebSocket `/home/feiyan/.codex/app-server-control/app-server-control.sock`；未连接 Board 1099/Gateway 1999，未重启/停止 App Server，未读取或触碰任何已有用户 thread。只创建一个明确的 C1 临时目录 `/tmp/better-subagent-c1` 和一个 durable 临时 thread，失败后已调用 `thread/delete` 清理。

## 已完成的只读/最小探测

- `AppServerTransport.connect()` → `initialize` 成功，版本仍为 0.154.0。
- `permissionProfile/list {cwd: /tmp/better-subagent-c1}` 返回 `:read-only`、`:workspace`、`:danger-full-access`，三者 `allowed=true`；宿主不存在 `vimo-development`，因此实验请求使用 `:workspace`。
- 使用提交代码直接 `transport.request("thread/start", {cwd, model:"gpt-5.6-luna", ephemeral:false, permissions:":workspace", approvalPolicy:"on-request", approvalsReviewer:"user", runtimeWorkspaceRoots:[cwd]})` 成功。

创建的临时 thread：`01a08e61-79a7-76d3-9742-e120e1ec38bf`。

start response 的 effective facts：`activePermissionProfile.id=:workspace`、`approvalPolicy=on-request`、`approvalsReviewer=user`。Gateway registry 写入了临时 `sessionId=c1-runtime`、`controlMode=managed`、`runtimeStatus=notLoaded`、requested profile `:workspace`。

## Blocking defect：Gateway 无法启动首个 durable thread turn

调用 `GatewayService.start_run({sessionId:"c1-runtime", prompt:"Reply exactly C1_OK.", requestId:"c1-start-1"})` 时，`AppServerTransport.start_turn()` 无条件先调用 `thread/resume`；App Server 返回：

```text
no rollout found for thread id 01a08e61-79a7-76d3-9742-e120e1ec38bf
```

随后 Gateway 将其归类为 `run_start_failed`（502）。因此尚未执行 start terminal、steer、interrupt、approval 或第二 client external projection，避免在首项失败后扩大实验。

最小复现：

```text
thread/start(ephemeral=false, cwd=/tmp/better-subagent-c1, permissions=:workspace) -> success, threadId=T
GatewayService.start_run(sessionId -> threadId=T)
  AppServerTransport.start_turn
    thread/resume(threadId=T) -> -32600 no rollout found
```

严重度：**High / C1 阻断**。当前实现不能从一个新建但尚无 rollout 的 durable thread 开始首个 turn；Phase 1 不能宣称可用。可能的修复方向是首个 turn 走 `thread/start` 返回的 loaded state/直接 `turn/start`，或在协议允许时区分“首次加载”和“resume”；本报告不实施修复。

## 清理与未验证项

- 已对唯一 C1 thread 调用 `thread/delete`，返回 `{}`；临时 cwd 未产生业务文件，探针脚本已删除。
- 未获得 turn ID，因此没有 terminal/steer/interrupt/approval 事件，也没有 pending approval 或第二 client。
- C1 verdict：**不通过，阻断于首个 Gateway start_run**。修复并复审前，后续 runtime calibration 项目保持未验证。

## Attempt 2（commit `38a3e36`）

目标是验证 read-first 修复。使用唯一临时 cwd `/tmp/better-subagent-c1-attempt2` 与 durable thread `01a08e67-a34b-79c3-8b7b-cd16ff55477c`；`:workspace` profile allowed，effective `:workspace/on-request/user`。

- `AppServerTransport.connect/initialize`、`permissionProfile/list`、`thread/start` 成功。
- `GatewayService.history` 在首 turn 前未产生可用 history（新 thread 尚无 rollout）；未将此视为阻断，继续执行。
- 首个最短 turn 成功：turn `01a08e67-a410-73a2-a36c-55a68c9e7dd3`，Gateway Run `run-e8a85edd14974653bd2048b0dab64556`，终态 `completed`。
- 长 turn 成功启动：turn `01a08e68-5402-7963-862b-a6732103ead9`，Gateway Run `run-8cf56955732045e9a5363c7678eafb0e`；`steer` 请求 `c1-a2-steer` 成功进入协议调用，随后 `interrupt` 请求 `c1-a2-interrupt` 返回终态 `interrupted`。store 中 terminalAt 已写入，未见 unknown。
- 本次一次性探针在 interrupt 后即结束并清理，因此 approval scope、第二 client external projection、idle reconnect 未执行，保持**未验证**；无 pending approval、无临时业务文件。
- 已调用 `thread/delete`；复核返回 `thread not loaded`，再次 delete 返回 `no rollout found`，确认已不再 loaded。随后删除 `/tmp/better-subagent-c1-attempt2` 与一次性探针。

### Attempt 2 verdict

read-first 修复已通过首 turn、terminal、steer、interrupt 的最小真实路径；Attempt 1 的首 turn blocker 不再复现。整体 C1 仍为**未完成/条件通过核心项**：approval（turn/session scope）、第二 client external 投影和 reconnect 尚未校准，不能宣称完整 C1 通过。后续若继续，应在新的唯一 C1 临时 thread 上单独完成剩余三项，不重做已通过项。

## Attempt 3（仅剩余项）

使用唯一新 cwd `/tmp/better-subagent-c1-attempt3` 与 durable thread `01a08e6a-7bf0-7240-81d1-01289b734c17`；profile `:read-only`、`on-request`、reviewer `user`，模型 `gpt-5.6-luna`、effort `low`。

### A. Approval：部分通过，session scope 未证实

- Gateway 首个 run：`run-8dff531ab0c64eed8b3de0935bfa69ec`；turn `01a08e6a-7cc6-71d3-aac4-859ceedd581c`。
- 真实 pending request：JSON-RPC requestId `26`，method `item/commandExecution/requestApproval`，threadId/turnId 如上，cwd 为临时目录；命令为 `printf 'ok' > .../approval.txt`。Gateway store 投影为 `waitingOnApproval`。
- `availableDecisions` 仅含 `accept`、`acceptWithExecpolicyAmendment`、`cancel`，**不含 `acceptForSession`**。探针仍按目标验证发送 `acceptForSession`，Gateway 返回成功并收到 `serverRequest/resolved`，但文件未产生、turn 未 terminal，后接入 client 仍观察到 `active/waitingOnApproval`。
- 结论：Gateway 的 `acceptForSession` 请求在 JSON-RPC 层未被立即拒绝，但对 command approval 的实际语义未证实，不能把它视为合法 Session 授权；应以后续明确协议能力为准，不扩大权限实验。

### B. External：未验证（被 approval 状态阻断）

第二 client 对同一 thread `thread/resume` 返回 `active` + `waitingOnApproval`，`turn/start` 返回同一 active turn `01a08e6a-7cc6-71d3-aac4-859ceedd581c`，并非新 external turn；Gateway snapshot 仍为 `managed/waitingOnApproval`，未抢占也未错误标记 external。第二 client 随后发送 interrupt `{}`，避免继续扩大；因此“idle 时第二 client 启动新 turn → Gateway external/active”保持未验证。

### C. Idle reconnect：通过

在上述 active 状态被安全打断后，Gateway transport close → connect；`thread/read(includeTurns=false)` 返回 `status.type=idle`，`thread/resume` 也返回 idle。没有自动重放 Prompt，也没有伪造 done。该项为**通过（idle-only）**；active/pending reconnect 仍未验证。

### Attempt 3 清理与 verdict

- `serverRequest/resolved` 曾出现在通知流；事件方法包含 `turn/started`、`turn/completed`、`thread/status/changed`、`serverRequest/resolved` 等。
- 已调用 `thread/delete` 返回 `{}`；临时文件未产生，cwd 与一次性探针已删除。
- Attempt 3：approval **部分通过但 acceptForSession/session scope 未通过**；external **未验证**；idle reconnect **通过**。完整 C1 仍**不通过/条件通过**，不可宣称所有剩余项已完成。

## Attempt 4（目标 `8af77ce`）

按要求仅补 approval 与 external，各使用独立的新 durable 临时 thread；未接 Board/Gateway 部署、未重启服务、未触碰既有 thread。

### Approval：真实 `accept` 也未完成（阻断）

- cwd `/tmp/better-subagent-c1-attempt4-approval`；thread `01a08e73-3d01-7fe2-b1a1-32d0dd678f20`；Gateway Run `run-72023593f4fc4a4baa3c399b3812925d`。
- pending request：numeric JSON-RPC requestId `29`，method `item/commandExecution/requestApproval`，turn `01a08e73-3dbc-7b91-abbd-8ae34a11918a`；命令只写临时 `approval.txt`。`availableDecisions` 明确支持 `accept`（也支持 amendment/cancel），本次只发送 `accept`。
- Gateway 返回 `APP_RESPONSE {requestId:"29",decision:"accept"}`，但未收到 `serverRequest/resolved`，文件未生成，terminal 未到达；pending 未消失。最小事实指向 request ID 类型问题：服务端请求 ID 是数值 `29`，Gateway 路径/存储将其字符串化后，`AppServerTransport.respond_approval` 回发 JSON-RPC id `"29"`，可能导致响应无法匹配。未修改代码、未重试或扩大权限。
- **Verdict：approval blocker，C1 不通过。** `acceptForSession` 不应继续测试；Attempt 3 已证明其不在 command approval 的 `availableDecisions` 中。

### External：通过（active projection / no抢占）

- 独立 cwd `/tmp/better-subagent-c1-attempt4-external`；thread `01a08e74-05a2-7cb0-87ea-967d651acc2c`；第二 client turn `01a08e74-0684-7791-9fd9-8a6de3518cbf`。
- Gateway 无 managed run 时，client B 启动最短可控 turn；client A 收到 `thread/status/changed`、`turn/started` 等事件，Gateway snapshot 为 `controlMode=external`、`runtimeStatus=active`、`activeTurnId` 正确，未抢占/未拒绝。
- B 发送 `turn/interrupt` 返回 `{}`；Gateway snapshot 随后仍为 `external/active`，说明 external terminal 后的 idle 投影尚未实现或未在本次事件路径更新；该残余行为需后续修复/校准。
- **Verdict：external active projection 通过；external→idle 投影未通过/未验证。**

### Attempt 4 清理

两个 thread 均调用 `thread/delete` 返回 `{}`；两个临时 cwd、文件和一次性探针已删除。完整 C1 仍不通过，核心阻断是 approval response 未被 App Server 解析；Attempt 2 的首 turn/terminal/steer/interrupt 与 Attempt 4 的 external active projection 可保留为已证实事实。

## Attempt 5（目标 `2f8223a`）

### A. Approval：numeric id / accept 闭环通过

- cwd `/tmp/better-subagent-c1-attempt5-approval`；thread `01a08e77-318d-7311-b2b3-eeaabae45584`；Gateway Run `run-3d95ef7e956a4c68b94704889aa8ea4b`。
- pending 为 numeric JSON-RPC requestId `32`，method `item/commandExecution/requestApproval`，turn `01a08e77-326d-7cb1-a055-eaee3808223b`；`availableDecisions` 含 `accept`，本次只发送 `accept`。
- Gateway 保留 numeric request ID 并正确回发；临时文件已生成，收到 `serverRequest/resolved`，pending store 清空。Attempt 5 原始记录未取得 terminal callback/Run completed 证据，不能在本 Attempt 追认 terminal。
- **Verdict：approval accept/resolved/file 通过；terminal 未确认。** `acceptForSession` 仍按 runtime capability unavailable 处理，不阻塞基础 turn approval；terminal 闭环由 Attempt 6 单独确认。

### B. External：通过

- 独立 cwd `/tmp/better-subagent-c1-attempt5-external`；thread `01a08e77-fbdf-78d1-a966-1a242e95d218`；第二 client turn `01a08e77-fcc7-7780-9d7c-54d6655149c9`。
- Gateway 无 managed run 时第二 client 启动可控 turn，第一 client/Gateway snapshot 为 `external/active`，activeTurnId 正确，无抢占。
- 第二 client `turn/interrupt` 返回 `{}`；第一 client 收到真实 `turn/completed`，最终 snapshot 为 `external/idle`、`activeTurnId=null`。
- **Verdict：external active→interrupt→completed→idle 全链路通过。**

### Attempt 5 清理与总评

两个 thread 均 `thread/delete` 返回 `{}`；临时文件、cwd、探针均已清理。Attempt 5 证明 numeric approval identity、accept/resolved/file 和 external 全链路有效，但该 Attempt 的 terminal 未确认；terminal 闭环由 Attempt 6 后续确认。session-scope approval 仍不应宣称全局可用。

## Attempt 6（approval terminal only）

- cwd `/tmp/better-subagent-c1-attempt6`；thread `01a08e79-5ae4-7621-8386-0f8ef150dc87`；Gateway Run `run-99de00999d354b9bb8da4fee2d496eff`；turn `01a08e79-5bbd-72a2-b472-e075101aefee`。
- 严格短 prompt 触发 command approval，numeric requestId `33`，`availableDecisions=[accept, acceptWithExecpolicyAmendment, cancel]`；只提交 `accept`。文件生成，pending 清空；Gateway 收到 resolved（同一 approval handler 路径）。
- 5 秒轮询 `thread/read(includeTurns=false)`：第 1 次起即 `status.type=idle`；Gateway store 的 Run 同时为 `completed`，无 `active`/`unknown` 关联错误。随后最多 50 秒轮询中一直保持 idle/completed。
- 探针的 `terms=[]` 是旁路监听遗漏；Gateway Run 已为 `completed`，结合 `thread/read` 的 idle 状态，足以证明 terminal callback 已执行，不构成产品失败。执行封装在 60 秒附近终止，未完成原计划 90 秒轮询；之后只读复核仍返回 idle，并直接 `thread/delete` 成功。
- 已清理 thread、`approval.txt`、cwd 与探针；未重测其他项。**Attempt 6 verdict：approval terminal callback 通过（由 Gateway Run=completed 证实）；没有发现 Gateway run active 的关联 bug。**

## C1 MVP 最终裁决

结合 Attempts 2、5、6，C1 MVP **通过**。已验证：durable thread/read-first、首 turn/completed、可控长 turn steer、interrupt/interrupted、command turn approval 的 numeric request ID 保真 + `accept`/`serverRequest/resolved`/文件结果/Run completed、第二 client external active→interrupt→真实 completed→external idle，以及 idle reconnect/read/resume。未验证或有能力条件：`acceptForSession` 仅当当前 request 的 `availableDecisions` 或 permissions scope 明确支持时展示；当前 command approval 不支持，不宣称全局可用。自动 reconnect 后恢复/自动调度、active/pending reconnect、分页 history 与完整 settings effective projection 延期。
