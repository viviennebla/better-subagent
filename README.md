# better-subagent

`better-subagent` 是面向 Codex Session 的轻量 Gateway。它把 Session 注册、单活 Run、启动、状态查询和打断从 Web Ledger 中抽离出来，让 Board 只处理 Task、人工调度和业务状态。

当前 `0.1` 基线仍通过 `@openai/codex-sdk` worker 恢复既有 thread。下一轮将迁移到长期运行的 Codex App Server，并加入人工查看、审批、steer 和 CLI/GUI 控制权移交。待评审方案见 [`docs/APP-SERVER-NEXT-ROUND-PLAN.md`](docs/APP-SERVER-NEXT-ROUND-PLAN.md)。

## 当前接口

- `GET /health`
- `GET /v1/sessions`
- `PUT /v1/sessions/{sessionId}`
- `POST /v1/runs`
- `GET /v1/runs/{gatewayRunId}`
- `POST /v1/runs/{gatewayRunId}/interrupt`

完整合同见 [`docs/BETTER-SUBAGENT-CONTRACT.md`](docs/BETTER-SUBAGENT-CONTRACT.md)。

## 本地运行

```bash
npm ci
python3 -m better_subagent --host 127.0.0.1 --port 8790
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
