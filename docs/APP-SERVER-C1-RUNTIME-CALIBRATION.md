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
