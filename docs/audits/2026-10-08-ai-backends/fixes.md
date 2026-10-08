# AI 后端修复与验收

日期：2026-10-08。此文件是原始审计的后续记录，不替换原始报告或结果。
产品修改保留现有 Backend / AgentManager 结构，未增加产品依赖。修改尚未提交或推送。
最终工作区文件的 SHA-256 见 [fixes-source-hashes.json](fixes-source-hashes.json)。

| 审计问题 | 修复行为 | 验收证据 |
| --- | --- | --- |
| API 搜索读取项目外符号链接 | API 读取、搜索、附件、项目指令和服务端目录共用 `project_path`；允许项目内链接，拒绝项目外链接和 `.git` | 外部、内部、断链回归夹具 |
| 图片、脚本、隐藏和排除目录漏报 | 变更检测遍历项目文件，不使用编辑器导航目录；记录文件字节、模式、链接元数据 | 二进制、隐藏、build、node_modules、新文件、scope/Ask 回滚和 Undo 回归 |
| Undo 被当作权限边界 | Codex 原生 profile 限制写入；Deep Code scope 明确标为回合后恢复 | 原生 sandbox 与真实 CLI；界面显示实现限制 |
| Deep Code 使用 Claude 工具指令 | 每回合刷新 `read/write/edit/bash` 指令，编辑使用原生 snippet | 真实 Deep Code 0.4.3 新会话、恢复会话和 snippet 编辑，模型响应为本地合成夹具 |
| Codex / Deep Code 没有编译工具 | 复用 `mcp_compile.py`；Edit 注册、Ask 不提供；服务端拒绝 Ask 的代理编译请求 | 两个真实 CLI 调用本地合成 build 端点 |
| 默认模型未应用 | 管理器在校验与 Job 创建前解析有效模型；三个 CLI 也支持直接调用时的默认值 | 默认与显式覆盖回归；无效默认模型被拒绝 |
| API 视觉附件只有路径 | 已声明支持图像的 API provider 发送原生 `image_url` 内容及文件名/MIME；Codex 使用 `--image` | 本地 API 捕获真实 HTTP 请求体；不支持的输入在模型请求前被拒绝 |
| 斜杠消息丢选区 | 未知斜杠消息保留引用正文；Skills 文案描述编辑器目录接入情况 | Node 执行实际前端函数 |
| CLI 路径与准备状态不准确 | 验证可执行文件；Codex 缓存兼容性检查并检查登录；Deep Code 读取项目 API key | 无 bin 回归；Codex 原生启动；Deep Code 项目配置验收 |
| Git 环境被描述为不可绕过 | 修正文案；Codex 禁止命令网络与 `.git` 写入，只提供编辑器 MCP；Claude 使用 strict MCP 配置 | 配置检查与真实 CLI；不声称环境清理是硬权限 |
| 工具失败误报成功 | API 使用 build 的失败标志；Codex 保留 MCP 业务错误；Deep Code 读取真实 session 格式并去重工具事件 | API 回归与两个 CLI 的真实 MCP 失败事件 |

## 权限与覆盖范围

Ask 不允许代理创建、修改或删除文件，也不允许代理编译。每回合更新模式说明。
作者仍可用编辑器 Compile 按钮编译。Edit 的编译工具会写入编辑器输出目录。
编译进程由编辑器运行，属于显式提供的能力，不受 CLI 源文件 scope 限制。

Codex 使用逐回合命名权限 profile，不混用旧 `sandbox_mode`。
不加载用户 Codex 配置或 execpolicy rules；登录凭据仍由 Codex 自己读取。
只有编辑器 `compile` MCP 被预批准，其他用户 MCP 和 Apps 不接入此回合。
此实现已在 macOS Codex CLI 0.156.1 验收。

Deep Code 只有操作类别权限。临时项目设置 overlay 在 Ask 中拒绝写入、删除与 MCP。
overlay 在回合后恢复原设置的精确字节；如有并发修改，则保留修改并报告。
Deep Code 原生权限不是 OS sandbox，不能表示单文件 scope。
单文件 scope 继续使用回合后恢复；界面明确说明此限制。
用户级 MCP 会被 Deep Code 合并且无法逐回合移除，因此存在此配置时停止启动，避免悄悄接入外部工具。

快照不扫描 `.git`，不跟随目录链接，不读取外部链接目标。
每文件最多保留 25 MiB，每次快照最多保留 100 MiB。
超限文件以 SHA-256 比较，报告修改并显示 Undo 不可用。
读取失败在结果卡片显示覆盖不完整。Undo 不覆盖回合后已再次修改的文件。
快照是差异/恢复机制，不是访问权限保证。

API 默认仅声明文本附件。视觉模型需配置 `"input_types": ["text", "image"]`。
未实现 API PDF 传输或 OCR。Deep Code 默认文本输入，未默认宣称其模型视觉能力。

## 原生运行证据

- [Codex 四回合记录](codex-acceptance.json)：真实已登录 Codex，新会话 → Ask 恢复 → 无 scope Edit → 新 scope Edit；文件修改与模式切换通过。本地编译端点返回合成错误，工具正确标记失败。
- [Deep Code 三回合记录](deepcode-acceptance.json)：真实 CLI 0.4.3，本地合成模型与 build；原生读取/snippet 编辑、恢复会话、Ask 写入拒绝、scope 外修改恢复通过。设置原文在各回合后保持一致。
- 原生 sandbox：直接尝试写三个文件，只允许 scoped `main.tex`；拒绝 `other.tex` 和 `figure.png`。

真实 Codex 测试先使用本机配置模型，服务拒绝该模型；随后使用 CLI 自带默认模型完成验收。
产品不会偷偷替换用户配置的模型，模型错误仍按运行失败报告。

这些结果不证明真实 DeepSeek 模型遵循提示、不证明视觉理解正确，也不证明真实 TeX 编译成功。
Windows/Linux 未验收。Deep Code 临时测试运行时安装在 `/private/tmp`，未改变产品或全局 npm 依赖。

可重复运行的可选验收：

```sh
PRISM_NATIVE_CODEX=1 python3 -m unittest discover -s tests -p test_backend_contracts.py
python3 docs/audits/2026-10-08-ai-backends/codex_acceptance.py
python3 docs/audits/2026-10-08-ai-backends/deepcode_acceptance.py --bin /path/to/deepcode
PRISM_SKIP_TEX=1 python3 -m unittest discover -s tests
```

Codex 验收会调用已登录模型，产生正常模型用量；编译使用本地合成夹具。
Deep Code 验收只调用本地合成模型。macOS 原生 sandbox 测试需允许宿主启动 sandbox-exec。

## 原有测试失败

Claude shell 测试隔离配置切换，并使用安全的环境清理。
临时测试目录先规范化，修正 macOS `/var` 与 `/private/var` 的路径别名断言。
Windows 格式路径断言只在 Windows 执行。
Deep Code 假 CLI 的调用记录移到其临时用户目录，避免混入完整项目变更检测。
GitSync 使用已有 `--no-optional-locks` 能力，避免状态读取刷新索引与保存产生可选锁竞争。

最终全量：`Ran 227 tests in 145.185s; OK (skipped=14)`，0 failures、0 errors。
12 项新增契约测试单独启用原生 Codex sandbox 后也全部通过。
JavaScript 语法、Python 编译和 `git diff --check` 通过。
未关闭测试夹具资源产生的 ResourceWarning 仍存在，不计作测试失败。

原始输出见 [fixes-tests.txt](fixes-tests.txt) 与 [原生契约测试](fixes-native-contract-tests.txt)。
机器可读范围与结果见 [fixes-results.json](fixes-results.json)。
