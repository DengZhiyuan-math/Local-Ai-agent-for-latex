# 第二阶段 F：发布、GitHub 与设置模块收尾

日期：2026-10-14（Europe/Berlin）。基线提交：`59699609b67602c45fc7ef9394bfffba2501840a`。

F 已完成本地实现和验证。本报告记录仅提交 F 之前的工作区验证快照；该提交包含 F 的代码、测试及本目录记录，不包含另行修复的 Codex Windows 沙盒相关文件。尚未推送。本次没有开展 B4、D、C、H、G。

## 范围与改动

Claude 留下的主体拆分保留：`settings.py` 负责全局设置，`github.GhCli` 负责 GitHub CLI，`publish.py` 负责发布流程。Home 使用这些模块，编辑器启动时直接加载 `publish`，不再加载 Home 服务器。

本次收尾：

- 修正 `test_hub.py` 调用已移走的 `hub._unstage_big`；发布逻辑测试改为直接调用 `publish`。
- 为 `FakeGitHub` 补上临时裸仓库和本地 Git 传输。只对 fetch/push 命令提供临时 URL 重写；目标发现、身份校验和绑定仍检查真实配置中的 GitHub 地址。成功测试实际执行 Git push，并核对远端提交和文件内容。
- 增加 `test_github.py`，覆盖登录状态缓存、刷新与过期、CLI 缺失、创建命令及 Git 环境清理、失败和超时、分支保护及套餐不可用。
- 增加 `test_publish.py`，覆盖默认 owner、重复发布、非 main 分支、创建失败、首次推送被拒绝、目标或分支不符、锁繁忙、大文件及构建输出、嵌套项目拒绝。
- 验证设置损坏时不覆盖文件和不解除 Claude 账号限制、配置目录切换、并发保存保留两个修改；加强编辑器启动依赖检查，要求发布模块已加载且 `hub` 未加载。

发布失败处理补齐两类既有缺口：

1. GitHub 仓库已创建，但远端身份或分支不符：错误结果保留已创建仓库的 URL，`publish_project` 正确返回 `created: true`，拒绝绑定和推送，保留本地提交及文件。
2. 首次推送遇到 `RepoBusy`：返回发布错误和已创建仓库的 URL，避免异常直接传出。

这两类缺口在拆分前 `hub.py` 中也存在，并非 F 拆分新增。三个回归测试在修复前全部报错，修复后通过。

## 验证

运行环境为 Windows，使用本机 Python 和 Git。Git Bash 的临时缓存与测试服务器需要宿主权限环境；没有修改 Codex 后端的沙盒设置。

| 检查 | 结果 | 原始记录 |
|---|---|---|
| 发布失败回归，修复前 | 3 项，3 项错误，确认能捕获缺口 | [red-tests.txt](red-tests.txt) |
| F、Home、目标绑定、Codex 专项 | 90 项，89 通过、1 平台跳过，65.731 秒 | [targeted-tests.txt](targeted-tests.txt) |
| 编辑器启动依赖、并发设置保存 | 2 项通过，0.237 秒 | [integration-tests.txt](integration-tests.txt) |
| 完整回归 | 394 项，370 通过、24 跳过，0 失败、0 错误，614.089 秒 | [full-tests.txt](full-tests.txt) |
| 输入、同步按钮与合作者推送压力测试 | 100/100 通过，4 个独立进程并发，497.9 秒 | [stress-results.json](stress-results.json) |
| 重启警告的拆分前基线复现 | 1 项通过；同一警告复现，11.896 秒 | [baseline-lifecycle.txt](baseline-lifecycle.txt) |

完整回归命令：`PRISM_SKIP_TEX=1 python -m unittest discover -s tests -v`，未启用 `PRISM_NATIVE_CODEX`。

压力测试重复运行 `test_sync_buttons.SyncButtons.test_buttons_while_typing_and_a_coauthor_pushes`。每次验证输入行、合作者的修改均到达远端，按钮无错误，仓库无未完成状态。

24 项跳过包括：12 项真实 TeX 验证、3 项需显式启用的 Windows 原生 Codex 沙盒探针、8 项其他平台专用检查、1 项需要符号链接的检查。没有执行真实 GitHub 建仓库、分支保护或真实模型验收；新发布端到端测试使用本地裸仓库。

## 保留的 Codex 修复

以下文件与接手 F 时的 SHA-256 完全一致：

- `prism_local/backend_codex.py`
- `README.md`
- `tests/test_codex_discovery.py`
- `tests/test_codex_runtime.py`

因此显式 `windows.sandbox="elevated"`、CLI 查找和 hooks 隔离改动均保留。普通 Codex 专项检查已随上述 90 项测试通过；原生沙盒探针本次未重跑。

## 已有警告

完整回归出现一条 `ResourceWarning: subprocess ... is still running`，来自重启生命周期测试。使用 `git archive HEAD` 导出的独立临时副本重跑相同测试，同一警告复现。对象分配跟踪指向 `registry.py:493` 的 `subprocess.Popen`：启动后台服务器后，仍运行的 Popen 对象离开作用域。

测试本身通过，并执行了其进程清理。`registry.py`、`httpbase.py`、`test_lifecycle.py` 均未修改。这个进程生命周期收尾问题留在 F 范围之外；不能把本次结果描述为“无资源警告”。

详细的测试统计、源文件哈希与 Codex 修复保留校验见 [results.json](results.json)。
