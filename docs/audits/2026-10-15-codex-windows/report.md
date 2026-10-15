# Codex Windows 沙盒修复与接手审计

日期：2026-10-15（Europe/Berlin）。D 已单独提交为 `c5d3dd5bc05b20d6ff3c7831b0184153438e996c`，未推送。该提交共 24 个文件，不包含 README、Codex 后端或 Codex 测试。本文及 Codex 修复仍在工作区，未提交。

## 原修复

编辑器每轮以 `codex exec --json --ignore-user-config --ignore-rules` 启动 CLI，并显式传入本轮命名权限配置和 `approval_policy="never"`。忽略用户配置也丢掉了 `windows.sandbox`：在本机 CLI 上，未启用原生沙盒时，普通读取命令也被策略拒绝。适配器在 Windows 上显式传入 `windows.sandbox="elevated"`，同时保留原有文件权限、禁用命令网络、禁用 Web 搜索、Apps、全局 hooks，以及仅由编辑器注册 compile MCP 的行为。

原修复还增加了 GUI 环境中的 CLI 发现：找不到常规 CLI 时，读取 `%LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe`，按修改时间选择候选，每次重新查找。provider 的 `bin`、`CODEX_BIN`、PATH 优先级保持原样。未改写用户 config.toml，也未添加绕过审批或沙盒的参数。

原生沙盒需预先在 CLI 或桌面应用中完成配置。本轮不更改用户或组织的全局沙盒选择。官方 Windows 文档将 `elevated` 描述为专用低权限用户、ACL 和防火墙等组成的实现；较新版本还支持 MXC，不能把本次在特定 CLI 上验证的结果推断为所有实现均已验证。[Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)

## 本次发现及修复

1. **文件范围可重新开放受保护目录。** 对 `.codex/protected` 直接授予写权限时，较具体的文件规则覆盖了父目录只读规则。原生 apply_patch 实测能写入。现在检查用户路径与解析后目标，拒绝向 `.git`、`.codex`、`.agents`、`.aws` 内的文件发放写权限；Windows 按不区分大小写处理。内部链接解析后指向这些目录时也不发放写权限。
2. **嵌套元数据遗漏。** 完整项目 Edit 原先只保护根目录的三个元数据位置。原生探针确认 `nested/.git/protected` 和 `nested/.agents/protected` 能被修改。现在每轮枚举已存在的嵌套元数据，生成明确的只读子树规则，并补齐 `.aws`。不进入元数据目录，不跟随目录链接或 Windows junction；枚举失败则停止该轮，避免默默漏掉保护。
3. **CLI 更新期间发现竞态。** 一份安装在检查过程中被删除或暂不可读，会使原先整个候选列表失效，隐藏仍可用的其他安装。现在逐个跳过失败候选；故障注入测试能重现旧行为并验证修复。
4. **兼容性检查不足。** 旧检查只验证 `exec --help` 中的 `--ignore-user-config`，但实际命令同时依赖 `--ignore-rules` 和 `exec resume`。现在对新会话、续聊分别检查两个隔离参数，不兼容时在发送消息前提示更新。CLI 版本检查与基于可执行文件修改时间的缓存仍保留。
5. **原生测试依赖活动用户缓存。** 原测试使用真实 CODEX_HOME，独立沙盒命令会读用户配置和缓存。本轮发现全局缓存目录锁初始化错误后，将所有原生探针及独立沙盒契约测试改用临时 CODEX_HOME；真实续聊也只在该临时目录中持久化会话。独立契约测试还同步采用生产命令的 Windows 参数。

规则优先级与命名权限格式已对照官方说明；实际修复使用精确子树规则。本机 CLI 的递归 read glob 探针报配置错误，因此未用未经验证的通配规则替代保护。[Permissions](https://learn.chatgpt.com/docs/permissions)

生产搜索确认 Codex 会话命令统一由 `backend_codex.Codex.command` 生成；`CliBackend.run` 负责进程生命周期。未发现另一个绕过该命令构造器的生产 Codex 启动入口。Claude/Deep Code 使用各自的权限机制，不使用 `--ignore-user-config`，本轮未扩大其改动范围。

## 验证

最终核对记录见 [results.json](results.json)：D 的 13 个源文件及测试与验收快照一致；本轮 5 个源文件的哈希与专项通过后的快照一致，暂存区为空，未产生额外提交。

- 修复前反例：[red-tests.txt](red-tests.txt)；10 个测试中出现 8 个失败子场景，覆盖文件范围、嵌套元数据、候选竞态和兼容性检查。
- 修复前原生证据：[metadata-before.json](metadata-before.json)、[policy-before.json](policy-before.json)。这些文件区分真实写入结果和不支持的 glob 配置。
- 原有 3 项原生检查：[original-native-tests.txt](original-native-tests.txt)，全部通过，确认原 Windows 修复有效。
- 最终专项及原生检查：[native-tests.txt](native-tests.txt)、[native-result.json](native-result.json)，19 项全部通过，约 40 秒。
- 完整回归：[full-tests.txt](full-tests.txt)、[full-result.json](full-result.json)，444 项中 415 通过、29 跳过，0 失败、0 错误，约 522 秒。29 项跳过包括真实 TeX 12 项、默认不启用的原生 Codex 探针 8 项、其他平台 8 项和符号链接 1 项；原生探针已在上述专项中单独启用并通过。

完整回归的父级 stderr 另输出两条既有夹具资源警告，分别为持锁子进程退出时未显式关闭 `prism-local-files.lock`，以及读取子进程退出时未显式关闭 `main.tex`。这些警告不在 Python unittest 流重定向日志内，另记在 [fixture-warnings.txt](fixture-warnings.txt)。D 验收已在未包含 D 的基线独立复现相同两类警告；本轮未改动对应夹具。不能把本次结果描述为完全无资源警告。

原生检查使用本机 `codex-cli 0.162.0-alpha.17.2` 和 Python 3.12.14；Responses 由临时本地 HTTP 服务器脚本提供，未调用真实模型或使用用户登录。覆盖缺失 Windows 设置的拒绝反例、Ask/单文件/完整项目的原生 shell 权限、apply_patch 允许与拒绝、嵌套元数据、项目外文件、带空格路径、完整 Edit 后续聊切换 Ask 和另一文件范围、用户 MCP 配置隔离，以及 Edit compile MCP 到本地假编辑器的完整调用。编译探针只验证协议，不运行 TeX。

最初 compile 探针直接调用 MCP 名称，当前 CLI 使用统一工具包装，返回 `unsupported call`；改用实际 `functions.exec` 路由后通过。这是测试协议校正，未修改生产 MCP 注册。早期失败记录见 [native-additional-red-tests.txt](native-additional-red-tests.txt)，最终通过结果以 native-tests.txt 为准。

## 边界与运行环境

- 精确子树规则保护轮次开始时已存在的嵌套元数据；每轮重新枚举。尚未存在、在同一完整项目 Edit 中新建的嵌套元数据不受该枚举规则预先保护。不要将结果描述为动态拦截任意新建元数据路径。
- 未在其他 Windows CLI 版本、MXC、Linux/macOS 上运行原生探针。非 Windows 命令不注入 Windows 沙盒选项，有独立单元覆盖。尚未执行真实模型、GitHub 账户或真实 TeX 编译操作。
- 临时 CODEX_HOME 隔离测试的配置、会话及缓存，不隔离 Windows 机器级沙盒服务和账户。原生检查需要显式设置 `PRISM_NATIVE_CODEX=1`，不应与正在工作的 CLI 批量并行运行。
- 活动 CODEX_HOME 的 `.sandbox-bin` 目录出现 `helper_sandbox_lock_failed`。目录存在；在同一机器用独立临时 CODEX_HOME 执行独立沙盒检查可通过。普通 Codex 工具通道也出现相同初始化错误，因此本轮后续 shell 检查通过已授权的主机执行完成。没有修改全局 ACL、删除缓存、结束桌面进程或放宽权限；当前工具环境故障与适配器命令修复分开记录，不能把临时目录测试通过写成全局缓存已修好。若活动 CLI 仍报该错误，需要重启 Codex 后复核其原生沙盒配置。[Windows sandbox 故障排查](https://learn.chatgpt.com/docs/windows/windows-sandbox)
- D 验收仍以 [D 报告](../2026-10-14-refactor-d/report.md) 为基础；本轮没有改变 D 源码。启用 D 需要完整退出旧 Home/编辑器后重新启动一次，旧进程内存中的实现不会随提交更新。本轮未重启用户服务。

## 复核补记（Claude，2026-10-15）

- 工作区 5 个文件与 source-snapshot.json 哈希一致；Codex 专项及原生测试复跑通过。
- 原生测试结果依赖解释器。上文 Python 3.12.14 为 Codex 自带运行时，`CodexSandboxUsers` 对其有执行权限；本机用户安装的 Python 3.14/3.11（`%LOCALAPPDATA%/Programs/Python`）只授权给当前账户、Administrators 和 SYSTEM。以用户 Python 启用 `PRISM_NATIVE_CODEX=1` 时，独立沙盒契约测试报 `CreateProcessAsUserW failed: 5 (Access is denied)`。该测试在 Windows 上已改用系统 PowerShell，两种解释器下均通过。生产含义：编辑器 Codex 轮次中，沙盒 shell 不能启动仅为用户账户安装的程序；compile 经 MCP 在编辑器中运行，不受影响。README 已注明。
- 补充原生探针（完整项目 Edit，apply_patch 新建文件）：根目录尚不存在的 `.codex/config.toml`、`.agents/skills/x.md` 被拒绝；`nested/.git/config` 可新建，与上文边界一致。
- `features.hooks=false` 已补原生验证：临时 CODEX_HOME 的 `hooks.json` 配置 SessionStart、UserPromptSubmit、PreToolUse 命令 hook，两次运行均加 `--dangerously-bypass-hook-trust` 排除信任因素。去掉该设置的对照运行中 hook 会执行，说明仅靠 `--ignore-user-config` 不能屏蔽 `hooks.json`；生产命令下 hook 未执行。
- 复核后修复三处：@ 提及元数据文件时，Edit 轮次在开始前给出明确错误（`check_scope`），不再静默不授权；元数据扫描遇到无法读取的目录时给出说明路径的错误，扫描中被删除的目录跳过；元数据名称在所有平台不区分大小写，`fsutil.project_path` 的 `.git` 检查同样处理（macOS 上未存在的 `.GIT` 路径不会被路径解析还原大小写）。
- 轮次中新建的元数据：补充探针确认完整项目 Edit 可在子目录新建 `.git`，且快照从不进入 `.git`，改动列表看不到。现在每轮（所有后端）在开始和结束时各扫描一次任意层级的 `.git`、`.codex`、`.agents`、`.aws`（与 Codex 只读规则共用 `fsutil.metadata_paths`），轮次结果中列出新出现的路径。只提示，不删除，由作者决定。这是事后提示，不是预先拦截；编辑器在轮次结束前退出时不会提示。
- 所有原生检查均使用临时 CODEX_HOME。活动 `~/.codex` 的 `.sandbox-bin` 锁故障未处理，复核时 Codex 桌面进程仍在运行；完全退出并重启 Codex 后，仍需在编辑器中以真实环境运行一次 Edit 轮次确认。
