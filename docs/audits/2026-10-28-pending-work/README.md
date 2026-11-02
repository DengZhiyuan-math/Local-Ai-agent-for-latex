# 未提交工作清单

盘点日期：2026-10-28。分支：`main`。基准：`e33657e0abd1073ed7f933a371c42c656e59dd7e`。

盘点开始时有 22 个已跟踪修改文件、24 个未跟踪文件，暂存区为空。下列范围按实际差异划分；共享文件应按差异块拆分。本文是新增整理记录，不计入上述初始数量。

## 建议的本地提交单元

| 顺序 | 提交主题 | 内容 | 当前验证 |
| --- | --- | --- | --- |
| 1 | `fix(http): handle burst asset connections` | 静态资源冷启动并发连接的 backlog 修复 | 并发回归及 HTTP 测试通过 |
| 2 | `feat(build): record dependency-backed build results` | 实际编译依赖、源码/PDF 哈希及只读构建凭证接口 | 构建凭证与失败场景测试通过 |
| 3 | `feat(project): save sources only when hash matches` | 独立 SHA-256 条件保存接口，防止覆盖新人工修改 | HTTP 冲突及成功保存测试通过 |
| 4 | `feat(agent): add approvals, usage sync, model controls and label navigation` | 本次两个 Bug 修复、两个功能、测试与说明 | 专项 44 项通过；既有 Agent 60 项通过 |

第 3 组测试目前放在第 2 组的新测试文件中，建议在第 2 组之后提交。第 4 组中的权限、模型目录和额度查询复用双向 CLI/只读控制服务，并与前端、HTTP 集成测试交叉关联，建议作为同一集成提交保留。

### 1. HTTP 静态资源并发

完整文件：

- `prism_local/httpbase.py`
- `tests/test_httpbase.py`

行为：将监听队列设为 `socket.SOMAXCONN`；测试先暂停 accept，再同时发起 8 个静态资源请求，确认响应完整。

### 2. 编译结果凭证

完整文件：

- `prism_local/build.py`
- `prism_local/build_results.py`（新增）

共享差异：

- `prism_local/project.py`：`import build_results`，以及 `build_results.begin/finish` 和构建异常处理。
- `prism_local/server.py`：`import build_results` 和 `GET /api/build/result`。
- `tests/test_build_results.py`（新增）：`Results`、`Rules` 和 `HTTPResult.test_get_is_read_only_and_exposes_no_current_build`；条件保存测试归下一组。

行为：为 TeX/latexmk 启用 recorder，为 Tectonic 记录 Makefile 依赖；只给依赖未变化且 PDF 相符的成功构建出具 complete 凭证。读取接口不发起编译。构建失败、进行中、源码变化及无法证明的依赖有不同状态。

当前证据来自隔离编译替身和 HTTP 测试。真实 TeX 编译与原始上层调用需求未在本次整理中重新验收。当前编辑器前端没有调用这两个新增服务端接口。

### 3. 按哈希条件保存

共享差异：

- `prism_local/project.py`：`import hashlib`、`Project.save_conditional()`。
- `prism_local/server.py`：`POST /api/file/conditional`。
- `tests/test_build_results.py`：`HTTPResult.test_conditional_write_has_a_separate_endpoint_and_preserves_conflicting_manual_text`。

行为：先获取既有文件锁并检查 Agent 编辑状态，再比较当前源码 SHA-256；不匹配返回 409，匹配才原子写入。独立路由避免旧服务忽略 hash 条件。

`project.py` 中的 `hashlib` 也被第 4 组 label 版本计算使用；只在首次需要它的提交加入 import。

### 4. Agent 四项改进

完整范围：

- `prism_local/agent.py`
- `prism_local/backends.py`
- `prism_local/backend_claude.py`
- `prism_local/backend_codex.py`
- `prism_local/backend_openai.py`
- `prism_local/permissions.py`（新增）
- `prism_local/usage.py`（新增）
- `prism_local/settings.py`
- `prism_local/symbols.py`
- `prism_local/static/app.js`、`app.css`、`chatlogic.js`、`client.js`、`index.html`、`turn.js`
- `tests/test_agent_improvements.py`、`test_agent_http.py`、`test_model_catalog.py`、`test_usage.py`、`test_web_permissions.py`、`test_labels.py`（新增）
- `tests/labels_check.js`、`usage_state_check.js`、`browser/agent_improvements_fixture.py`（新增）
- `tests/frontend_check.js`、`client_routes.js`、`test_frontend.py`
- `README.md`、`CONTEXT.md`（后者新增）
- [实现报告](../2026-10-28-agent-improvements/report.md)及其 `validation/` 日志
- `.scratch/agent-improvements/map.md`、`issues/01-integration.md`：已解决的本地 wayfinder 记录

共享差异：

- `prism_local/project.py`：label 索引、解析及 `json` import；`hashlib` 与前一组共享。
- `prism_local/server.py`：模型目录、配置、Web 权限、额度刷新及 label 路由。

行为和边界：权限批准回传同一 CLI 进程；额度只读查询并显示成功时间/过期/失败；模型和 effort 按能力选择并保存；label 点击时刷新索引和目标 tab。真实模型/Web 工具/订阅额度未调用。缺少 effort 元数据的 API 需显式配置，DeepCode 目录仍来自配置。

## 历史验收资料

`docs/audits/2026-10-08-sync-repair/report.md` 和 `verified.jpg` 尚未跟踪，可作为独立历史资料提交：

`docs(audit): archive sync-repair verification`

报告记录的是 2026-10-08 当时状态。其“代码未提交”描述不能代表当前工作树：

- `gitsync.py` 和 `test_gitsync.py` 当前没有差异。
- “先筛选 changes，再限定本回合路径”的修复已在提交 `72dc949` 中。
- 已确认 `72dc949` 是当前 HEAD 的祖先。

保留原报告和截图作为历史证据；不把它们列为新的待修复代码。

## 验证记录与未解决项

本次整理新运行 `test_build_results` 和 `test_httpbase`：35 项通过。异常处理测试会主动打印 RuntimeError/TypeError；最终测试结果为 OK。

沿用本次会话已有结果：

- [Agent 专项日志](../2026-10-28-agent-improvements/validation/targeted-tests.txt)：44 项通过。
- [既有 Agent 日志](../2026-10-28-agent-improvements/validation/agent-tests.txt)：60 项通过。
- [全量日志](../2026-10-28-agent-improvements/validation/full-tests.txt)：运行 573 项，31 项跳过，3 处失败，涉及 2 个测试方法。
- [macOS 基线](../2026-10-28-agent-improvements/validation/baseline-macos.txt)和[路径配置基线](../2026-10-28-agent-improvements/validation/baseline-projectconfig.txt)在未修改 HEAD 副本上复现了相同失败。

未解决的既有失败：Safari/Chrome 原生 AppleScript 检查各一处；POSIX 对 `C:/x` 的输出目录检查一处。它们不属于上述四组变更。

## 整理操作

- 将 5 份 Agent 验证日志从 `.scratch/agent-improvements/` 归档到 `docs/audits/2026-10-28-agent-improvements/validation/`，内容保持不变。
- 更新实现报告中的日志链接，使审计目录包含完整证据。
- 保留本地 wayfinder 地图与已解决票据。
- 保留全部功能代码与原有修改；暂存区和提交历史未改变。
