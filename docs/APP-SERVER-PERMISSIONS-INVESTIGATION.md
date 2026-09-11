# Codex App Server 权限模型调查与 VIMO 开发态建议

状态：调查结论，供 Gateway 从 SDK worker 迁移到 App Server 时冻结权限合同。

调查基线：Codex CLI / App Server `0.154.0`，2026-09-10。

原则：开发效率优先。Agent 应拥有完成编码、测试、依赖解析和本地提交所需的连续操作空间；权限系统保留少量明确边界，不把每个正常开发动作都变成审批。

## 1. 结论

App Server 的权限由三个彼此独立的维度组成：

1. permission profile 决定文件系统与网络的基础能力；
2. `approvalPolicy` 决定越出基础能力时是否允许发起审批；
3. `approvalsReviewer` 决定审批交给用户还是 `auto_review`。

App Server 不认识 VIMO 的 Task、owner、role 或 CAS，也不负责判断哪个网页用户可以恢复哪个 Session。这些业务授权仍由 Gateway/Board 执行。反过来，Gateway 不应把业务上的“允许调度”误解成宿主机 full access。

建议将权限策略定义为“宽基础权限 + 可控提权”，而不是“极窄基础权限 + 高频人工批准”：

- 日常 coder 使用本机已校准的 `:workspace` profile；未知 profile fail closed，profile discovery 延期；
- 工作区、绑定 worktree、构建缓存和临时目录可写；本地 Git commit 所需的 `.git` 写入不能被长期卡住；
- 开发所需网络默认可用，包括依赖、Git、测试 API 和文档查询；
- `approvalPolicy=on-request`，允许 Agent 为边界外动作说明原因并申请权限；
- `approvalsReviewer=auto_review` 处理常规低风险升级，减少开发过程中反复等待；
- 用户可以对可信的本地开发 Session 显式选择 full access，但它不是网页调度的默认值，也不能由 Prompt、报告或浏览器字段触发。

## 2. 权限解析链

```text
VIMO owner/role/CAS 与 Session 单活
        ↓  Gateway 负责
App Server requirements.toml 上限
        ↓
请求指定的 permission profile
        ↓
runtimeWorkspaceRoots 物化实际工作区
        ↓
approvalPolicy 判断能否申请边界外权限
        ↓
approvalsReviewer=user | auto_review
        ↓
沙箱执行命令、补丁、网络请求
```

内置 profile：

- `:read-only`：文件只读、网络受限；适合纯调查，不适合作为 coder 默认值。
- `:workspace`：工作区可写、网络受限；可作为保守回退，但仍不足以覆盖需要联网和本地 commit 的完整开发流程。
- `:danger-full-access`：不应用 App Server 外层沙箱；只作为用户显式选择的可信本机模式。

Gateway 默认传 `:workspace`，并只传宿主已允许的 profile ID，不接受调用方提交任意 profile JSON。首版语义应至少包含：

- 对 canonical `runtimeWorkspaceRoots` 写入；
- 对绑定仓库 Git metadata 的本地提交能力；
- 对 `/tmp` 和必要构建缓存的写入；
- 开发网络可用；
- 保留对工作区外写入和危险宿主机操作的提权路径。

这不是隐私隔离 profile。若未来需要严格防止读取凭据，应另立安全阶段设计 deny rules；不要在当前开发合同中用大面积不可读路径破坏工具链、Shell 初始化和依赖发现。

## 3. App Server 协议字段

`thread/start` 与 `thread/resume` 接受：

- `permissions`：命名 profile ID；
- `runtimeWorkspaceRoots`：用于物化 `:workspace_roots` 的绝对路径；
- `approvalPolicy`；
- `approvalsReviewer`；
- 兼容字段 `sandbox`，不得与 `permissions` 同时提交。

`turn/start` 和 `thread/settings/update` 也可更新上述设置，但兼容沙箱字段名为 `sandboxPolicy`。`command/exec` 又把 profile 字段命名为 `permissionProfile`。Gateway adapter 必须吸收这种协议命名差异，Board、Browser 和 Agent 只使用一个稳定的内部 `operationPolicy` 合同。

命名 profile 相关字段在 0.154.0 仍标记为 experimental。Gateway 初始化连接时需要声明：

```json
{
  "capabilities": {
    "experimentalApi": true
  }
}
```

Gateway 应先调用 `permissionProfile/list {cwd}`，只选择返回 `allowed=true` 的 profile；同时用 `configRequirements/read` 读取宿主的强制上限。

## 4. 建议的 Gateway operationPolicy

Board 只传 `sessionId`、Prompt 和稳定 request ID。Gateway 根据自己的 Session registry 生成：

```json
{
  "permissionProfileId": ":workspace",
  "approvalPolicy": "on-request",
  "approvalsReviewer": "auto_review",
  "runtimeWorkspaceRoots": [
    "/canonical/path/to/bound/worktree"
  ],
  "allowSessionApproval": true
}
```

约束重点不应放在削弱 Agent 的正常工具能力，而应放在控制权限来源：

- `cwd` 与 workspace roots 只能来自服务端 Session registry，并在使用前做 absolute、realpath、symlink 和绑定 worktree 校验；
- Browser、Prompt、StageReport、`nextAction` 不能提交或覆盖权限参数；
- `allowSessionApproval=true` 允许对当前已加载 Session 中重复的安全动作使用 `acceptForSession`，避免同一操作连续弹窗；它不是永久全局授权；
- 用户显式切换 profile 时，Gateway 记录 requested/effective profile 和操作者，不能只覆盖旧值；
- `thread/settings/update` 返回空对象只代表请求进入队列，必须等 `thread/settings/updated` 后才将新权限视为生效。

## 5. Resume 合同

冷恢复时，App Server 的选择顺序是：本次 `thread/resume` 显式覆盖、历史中最后持久化的 profile ID、当前配置默认值。历史记录保存的是 profile 身份，不是永久冻结的底层权限；同一 ID 会按当前 config 与 requirements 重新解析。

为了让 VIMO 可解释且不因重启意外收紧权限，Gateway 每次冷恢复都应显式提交 registry 中的：

- `permissions`；
- `approvalPolicy`；
- `approvalsReviewer`；
- `cwd` 与 `runtimeWorkspaceRoots`。

恢复后以 `ThreadResumeResponse.activePermissionProfile`、`sandbox` 和其他 effective 字段为准。若 effective profile 与请求不一致，Session 应进入配置不一致状态并显示原因，而不是静默降级后让 Agent 在执行中不断碰壁。

## 6. 必须保留的少量硬边界

开发态宽权限不等于把 App Server 的全部 RPC 暴露给网页。

### 6.1 宿主机逃逸接口

- `thread/shellCommand` 明确以 full access、无 thread sandbox 的方式运行用户 Shell；
- `process/spawn` 明确在宿主机无 Codex sandbox 启动进程；
- `command/exec` 虽可指定沙箱，但属于独立的任意命令执行入口，不依赖受控 Turn。

这些接口可以保留给可信本机客户端或 Gateway 内部运维，但不能进入 Browser action、自由文本或通用 RPC proxy。限制它们不会妨碍 Agent 在正常 Turn 内使用 Shell、补丁、测试和 Git；正常 Agent 工具由宿主允许的 `:workspace` profile 承载。

### 6.2 任意配置覆盖

`thread/start`、`thread/resume` 的 `config` 是自由结构覆盖，能够进入完整运行配置。Gateway 不得透传该字段；已知且确实需要的设置必须逐项进入 server-owned allowlist。未知字段、token、secret、provider、MCP、hook 和命令配置一律拒绝。

### 6.3 Session 所有权与单写者

App Server transport 认证不等于 VIMO 身份认证。任何获准连接的本机客户端都不会自动受到 Ledger owner/role 约束。Gateway 必须继续校验 Task CAS、目标 Session、owner/role 和 Run 归属。

`have an active writer` 也是单写者冲突，不是权限不足。建议一个长期运行的 App Server 成为受管 Session 的唯一 writer，Gateway 在它之上复用连接；不要让 SDK worker、第二个 App Server 和 GUI 同时冷恢复并写同一 rollout。外部 GUI 已接管的 Session 标记为 unmanaged/busy，等待释放后再由 Gateway 接管。

## 7. 传输边界

当前本机 App Server 使用 Unix control socket。实测目录权限为 `0700`、socket 为 `0600`，只提供 OS 用户级隔离；同一 Linux 用户下的其他进程仍可能连接。

Gateway 优先连接 Unix socket。若未来改用 WebSocket：

- 非 loopback listener 必须配置 capability token 或 signed bearer token；
- loopback 可以无 App Server 认证，仍只能由 Gateway 暴露受控业务 API；
- 不把原始 App Server WebSocket 转发到 LAN 或 Browser。

## 8. 开发态与高风险操作的分界

以下能力属于正常开发自由，默认允许：编辑绑定代码、运行测试和构建、安装项目依赖、查询技术资料、访问测试 API、本地 Git add/commit、写临时文件、启动测试所需的短期子进程。

以下动作不应靠收紧日常文件权限解决，而应由 VIMO workflow 单独要求用户确认：部署、修改 live data、操作生产环境、merge、force push、删除远端资源、发送外部消息、使用或导出凭据。这样 Agent 在编码阶段不会被频繁阻塞，真正不可逆或有外部影响的动作仍有清楚边界。

## 9. 当前建议

1. App Server 迁移第一阶段采用 `:workspace + on-request + auto_review`。
2. 保留 `:workspace` 作为故障回退，保留用户显式 full-access 入口，不把 full access 做成自动路由目标。
3. 支持当前 Session 范围的重复授权，避免逐命令审批。
4. Gateway 只做权限来源、Session 所有权、单写者和宿主机逃逸 RPC 的硬控制；不要为每种开发命令建立细粒度白名单。
5. 对 named profile、恢复后的 effective profile 和审批生命周期做集成测试；不要仅测试请求 JSON。

## 10. 证据来源

- [Codex App Server 协议与说明](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- [0.154.0 thread/start、resume、settings 与 shellCommand 定义](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/app-server-protocol/src/protocol/v2/thread.rs)
- [0.154.0 approvalPolicy 与 approvalsReviewer 定义](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/app-server-protocol/src/protocol/v2/shared.rs)
- [0.154.0 permission profile 编译与内置 profile](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/src/config/permissions.rs)
- [0.154.0 command/exec 与 process/spawn 定义](https://github.com/openai/codex/tree/rust-v0.154.0/codex-rs/app-server-protocol/src/protocol/v2)
- [0.154.0 Unix socket 与 WebSocket 认证](https://github.com/openai/codex/tree/rust-v0.154.0/codex-rs/app-server-transport/src/transport)

## 11. 未决项

- profile discovery、`configRequirements/read` 与准确 filesystem/network 能力仍需后续真实 schema 校准；未知 profile 不自动回退。
- GUI Remote 与 Gateway 是否共用同一个 App Server writer，需要以最终部署拓扑确认；权限 profile 本身不能解决双 writer。
- 自动审批拒绝后是否回退人工审批，应在审批 UI 合同中单独确定，不应通过默认 full access 绕过。
