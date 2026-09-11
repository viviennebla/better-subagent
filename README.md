# better-subagent

`better-subagent` 是面向 Codex Session 的轻量 Gateway。它把 Session 注册、单活 Run、启动、状态查询和打断从 Web Ledger 中抽离出来，让 Board 只处理 Task、人工调度和业务状态。

当前实现已完成 App Server transport/runtime 的静态 review，默认连接 GUI-managed Codex App Server；仍需 C1 runtime calibration。SDK worker 保留为配置级回退。方案与校准边界见 [`docs/APP-SERVER-NEXT-ROUND-PLAN.md`](docs/APP-SERVER-NEXT-ROUND-PLAN.md)。

## 当前接口

- `GET /health`
- `GET /v1/sessions`
- `GET /v1/sessions/{sessionId}`（runtime/control、policy、pending approvals）
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
