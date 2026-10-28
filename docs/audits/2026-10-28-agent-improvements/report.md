# Agent 权限、额度、模型配置与 label 导航改进

日期：2026-10-28。范围：BUG-002 → BUG-001 → FEAT-002 → FEAT-001。

四项功能均已接通前后端。复用现有 Backend、AgentManager、流式事件、用户 settings、Project.symbols 和编辑器 tab。未新增运行时依赖。

## 根因和实现

### BUG-002：Web 工具权限

原先的 CLI 调用发送文本后关闭 stdin，无法把 UI 的决定传回 `can_use_tool` 请求。

现在使用双向 stream-json，初始化后发送原提示。WebSearch/WebFetch 等待 UI 的单次、会话或持久授权。批准后向同一进程、同一请求返回原始工具输入，继续原任务。拒绝、Stop、控制请求取消、进程退出和迟到回复均会释放或拒绝待处理请求。

授权按项目、CLI profile、provider、工具隔离。WebFetch 限定 HTTP origin，拒绝私有地址、凭据 URL 和非 HTTP 协议。持久授权写入 Prism 用户状态目录的 `web-permissions.json`，不写入项目。持久化失败时继续等待用户选择，不自动放行。保留 CLI deny、文件 scope 和 shell 限制；其他工具的权限回调拒绝处理。

### BUG-001：额度刷新

后端的 `rate` 事件被前端状态机丢弃；点击刷新图标内部时也不会触发查询。旧查询忽略错误和没有新数据的结果，UI 静默恢复全局浏览器缓存。

现在转发 rate 事件，使用 `closest()` 识别刷新按钮，并在每个成功、失败或停止的请求结束后查询。刷新使用 Claude 的只读 `get_usage` 控制请求，不发送 user prompt，也不回退到 Haiku 请求。手动刷新强制新查询；普通可见页面查询使用 60 秒缓存。

数据包含最后成功时间、过期标记、缓存来源及错误。CLI 的百分比转换为 0–1；不完整通知不会覆盖完整快照。失败保留同身份的成功快照和时间，标记 stale。profile、凭据文件或认证覆盖项变化清除缓存。旧 HTTP 响应不能覆盖更新的流式结果。UI 不再恢复全局 `chat.rate`。API/cloud 身份不显示另一个订阅账号的额度。

### FEAT-002：模型和 thinking effort

对话头部新增模型、thinking effort 和刷新控件。Claude 从 initialize 元数据读取模型能力，Codex 从 app-server `model/list` 读取，OpenAI 兼容后端查询 `/models`。各后端支持刷新、缓存和失败状态。切换 profile/账户后不复用旧能力数据。

后端按所选模型校验 effort。每次请求使用当前模型和 effort，继续现有 session。选项通过现有 settings 服务保存，并合并保留其他 provider 的默认值。运行期间禁用配置控件。原有 `/model` 和 `/effort` 命令复用相同保存路径。

标准 `/models` 通常不返回 effort 能力。这种情况下需要明确的 `model_efforts` 配置；界面不会猜测档位。DeepCode 继续使用已有配置模型目录，未虚构其动态发现协议。

### FEAT-001：可点击 LaTeX label

复用 Project.symbols 建立项目级 label → 文件、行号列表及版本。支持多文件、显式 LaTeX 引用、引用列表和已知 label 名。数学输出在 KaTeX 节点外附加导航链接。

点击时重新扫描源码，刷新已打开的干净 tab 后跳转，保留用户未保存的修改。重名 label 提供位置选择，缺失 label 显示提示。索引支持源文件增加、删除、行号变化及保留 mtime 的内容变化。隐藏、生成、排除和项目外文件不会进入映射。

## 修改文件

| 文件 | 用途 |
| --- | --- |
| `prism_local/permissions.py` | Web 请求、授权、取消与持久化 |
| `prism_local/usage.py` | 只读 CLI 查询、归一化和身份隔离缓存 |
| `prism_local/backend_claude.py` | 双向控制协议、授权续跑、模型元数据和额度查询 |
| `prism_local/backends.py` | 保持 stdin 开放、模型目录和每模型 effort 校验 |
| `prism_local/backend_codex.py` | Codex 模型列表与能力发现 |
| `prism_local/backend_openai.py` | API 模型发现和缓存 |
| `prism_local/agent.py` | 权限路由、配置保存、模型列表和请求完成后的额度同步 |
| `prism_local/settings.py` | 保存并合并用户 Agent 默认配置 |
| `prism_local/server.py` | 模型、配置、权限和 label HTTP 接口 |
| `prism_local/project.py`、`symbols.py` | 新鲜 label 索引、解析与安全过滤 |
| `prism_local/static/app.js`、`index.html`、`app.css` | 配置控件、权限卡、额度状态和源码导航 |
| `prism_local/static/client.js`、`turn.js`、`chatlogic.js` | 请求封装、事件转发、引用识别和额度顺序规则 |
| `tests/test_agent_improvements.py`、`test_agent_http.py` | 配置保存、请求结束同步和真实 HTTP 集成测试 |
| `tests/test_model_catalog.py`、`test_usage.py`、`test_web_permissions.py`、`test_labels.py` | 四项功能的正常和失败场景 |
| `tests/labels_check.js`、`usage_state_check.js`、`frontend_check.js`、`client_routes.js`、`test_frontend.py` | 前端解析、顺序、事件和路由一致性 |
| `tests/browser/agent_improvements_fixture.py` | 隔离 UI/HTTP 测试服务及 CLI 协议替身 |
| `README.md`、`CONTEXT.md` | 用户行为、配置说明和领域术语 |

`build.py`、`httpbase.py`、`build_results.py`、相关既有测试和旧审计目录不是本次新增工作。`project.py`、`server.py` 的已有修改保留，新增功能只在相关位置增补。

## 验证

- 专项测试：44 项通过（日志：[专项日志](validation/targeted-tests.txt)）。manager 6、HTTP 集成 1、模型 5、额度 9、权限 12、label 8、frontend 3。
- 既有 Agent 测试：60 项通过（[日志](validation/agent-tests.txt)）。
- 全量测试：运行 573 项，31 项跳过，出现 3 处既有失败，涉及 2 个测试方法。详见 [全量日志](validation/full-tests.txt)。
- 未修改的 HEAD 副本复现相同失败：Safari 和 Chrome 原生 AppleScript 无法找到应用标识，各失败一次；POSIX 下将 `C:/x` 当作相对路径，失败一次。对应日志为 [macOS 基线日志](validation/baseline-macos.txt)和[路径配置基线日志](validation/baseline-projectconfig.txt)。本次未扩展修改这些模块。
- `git diff --check`、Python 编译检查和前端语法检查通过。

真实 HTTP 集成测试使用本地 CLI 协议替身，覆盖动态目录、无效 effort、配置保存、跨项目/失效权限拒绝、UI 决策到同一进程续跑、完成后额度查询、强制刷新、失败保留时间及新鲜 label 行号。

隔离 UI 验证使用真实编辑器和 Claude adapter，确认：选择 deep/high；UI 批准 WebSearch 后继续；切换 fast 后保留历史并发送 `--resume fixture-chat --model fast`；已授权搜索无需再次确认；刷新后保留默认配置；label 打开多文件源码并跟随外部插入行；重复/缺失 label 分别显示选择和提示；额度查询失败显示 stale 和原成功时间。页面无 console error。

## 验证边界和已知限制

- 没有调用真实模型、真实 WebSearch/WebFetch 或真实订阅额度接口。安装的 CLI 仅做无推理元数据协议探测；行为测试使用隔离的本地协议替身。
- `get_usage` 是 CLI 控制协议，旧版本或不支持的上下文会明确显示 unavailable/stale，不会伪造数据或发送付费模型请求。
- API 模型未提供 effort 元数据时需配置 `model_efforts`；DeepCode 的目录来源为配置。
- 本机全量测试的 3 处既有失败未在本任务中修复。
- 未提交、推送、部署或安装 App。
