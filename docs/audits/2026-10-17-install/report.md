# 一行安装：结果

日期：2026-10-17。方案见 [plan.md](plan.md)。6.1–6.5 已全部完成，每项一个提交。真实的干净机器上的验证见文末。

## 完成的项

| 项 | 提交 | 现在的行为 | 测试 |
|---|---|---|---|
| 6.1 | `9720549` | `install.ps1`：`irm … \| iex` 一行。找带 tkinter 的 Python ≥ 3.9（跳过 Microsoft Store 的别名），没有就只为当前用户安装 3.12（winget，没有 winget 用 python.org 的安装程序）；找 git，没有就用 winget 装，仍没有就下载 ZIP 并说明这份副本不会自动更新；克隆到 `%LOCALAPPDATA%\Prism\prism-local`，已有时只 fast-forward；别的东西占着目标文件夹时不动它并说明；做开始菜单和桌面的快捷方式（`make-shortcut.ps1` 新增 `-Python`）；打开 Home。整个脚本在一个 script block 里：通过 iex 运行时，失败不会关掉用户的 PowerShell 窗口，也不留下变量和函数。文件只用 ASCII（Windows PowerShell 5.1 会误读无 BOM 的 UTF-8） | `test_install_ps1`（18 项，pwsh 7 和 Windows PowerShell 5.1 各跑一遍，假的 winget / git / py） |
| 6.2 | `27aba6f` | `install.sh`：`curl … \| sh` 一行，只用 POSIX sh，全部在 `main` 里、最后一行才调用（下载中断时什么都不做）。macOS 先问 `xcode-select -p`，缺开发者工具时运行 `xcode-select --install` 并等它装完（最多 30 分钟），工具不在时不碰 `/usr/bin` 的占位程序；Linux 缺 Python 或 git 时说出那一条 apt / dnf / pacman / zypper 命令；克隆或更新；macOS 做 `.app`（`/Applications` 不可写时放 `~/Applications`），Linux 做菜单项；打开 Home。`.gitattributes` 让它保持 LF | `test_install_sh`（24 项，sh 和 dash） |
| 6.3 | `a8a6053` | `tools.py`：`/api/tools` 按方案的格式回答（`tex`、`claude`、按程序的 `can_install`）。Tectonic 在所有平台从 GitHub 的发布下载到 `state_dir()/bin`；macOS 和 Linux 上 GitHub CLI 同样下载，并用发布里的 checksums 校验 SHA-256；Windows 上完整的 LaTeX 用 winget 装 MiKTeX；Claude Code 用官方安装命令，`/api/tools/claude-login` 打开一个运行 `claude` 的终端窗口。解压只取需要的那个普通文件，拒绝 `..`、绝对路径和链接；先写临时文件再改名。编辑器启动时 `tools.refresh_path()`，`build_env()` 也带上 Prism 自己的 bin，所以刚下载的 Tectonic 能直接编译。找不到 TeX 时的提示指向 Home 的 Getting started | `test_tools_download`（17 项，本地 HTTP 服务器冒充 GitHub，不联网），`test_tools` |
| 6.4 | `32348da` | Getting started 新增 LaTeX 一行（Quick start：Tectonic；Full LaTeX：完整安装，不能自动安装时给一句说明）和"可选"的 AI 助手一行（Install；装好后 Log in）。git、GitHub CLI、GitHub 账户和 LaTeX 都就绪后面板消失，AI 助手不影响。设置页新增 Programs 一栏，列出全部程序和同样的按钮 | 浏览器 home menu 15 项（新增 1 项，原有 3 项扩充） |
| 6.5 | `6699fc2` | README 的安装一节以一行命令开头，原来的分步安装放在"By hand"下；Layout 列出 `install.ps1`、`install.sh` 和 `tools.py` | — |

## 与方案不同之处

- **macOS 不在后台安装完整的 LaTeX。** 方案里写的是有 Homebrew 时 `brew install --cask mactex-no-gui`。但 MacTeX 的安装包要管理员密码，而后台运行的程序没有地方输入，几乎一定失败。所以 macOS 上 Full LaTeX 改为一句说明（去 tug.org/mactex 下载，期间 Quick start 照常可用），测试也确认不会运行任何程序。
- **Linux 打开 Home 用 `launcher/prism_launcher.pyw`，不用 `bin/prism-home`。** `prism-home` 是 bash 脚本，而有的系统没有 bash；菜单项运行的本来就是这个启动器。

## 已核实的外部事实

用已登录的 gh 查询了真实的发布文件（只读）：

- Tectonic 最新正式版 `tectonic@0.17.0`，有 `-x86_64-pc-windows-msvc.zip`、`-{x86_64,aarch64}-apple-darwin.tar.gz`、`-{x86_64,aarch64}-unknown-linux-musl.tar.gz`，与代码一致。
- GitHub CLI 最新版 `v2.102.0`：macOS 的 universal 版是 `.pkg`，没有 `.zip`；代码随后会找 `gh_<v>_macOS_{arm64,amd64}.zip`，这两个都存在。`linux_{amd64,arm64}.tar.gz` 和 `checksums.txt` 也都存在。

## 执行中的一次事故

**这台电脑上被真的装了 Python 3.12.10。** 写 6.1 的子代理手动试运行 `install.ps1` 时，没有放假的程序，也没有打开测试用的保护开关。脚本于是走了"没有 winget、也没有 Python"那条路，下载并静默安装了 python.org 的 Python 3.12.10。这是只为当前用户的安装：

- `C:\Users\dengz\AppData\Local\Programs\Python\Python312`，创建于 2026-10-17 16:58；
- 注册表 `HKCU\Software\Python\PythonCore\3.12`；
- **用户 PATH 的最前面多了 `Python312\Scripts\` 和 `Python312\`**，新开的终端里 `python` 会是 3.12，不再是 3.14。

它想用安装程序自己的卸载功能撤销，被权限规则拒绝了，之后没有再尝试。已修复的部分：`PRISM_INSTALL_NO_REGISTRY=1`（测试都会设）时，脚本拒绝运行 python.org 的安装程序（`install.ps1:195`），有测试覆盖，测试不会再这样做。其他东西没有被碰过：`%LOCALAPPDATA%\Prism` 不存在；开始菜单里的 `Prism.lnk` 是你自己 2026-09-25 做的。

**需要你自己卸载**（其中任一种都可以）：

- 设置 → 应用 → 卸载 "Python 3.12.10 (64-bit)"；
- 或在 PowerShell 里运行：`& "C:\Users\dengz\AppData\Local\Package Cache\{b6ce88eb-2ce3-4d91-8efc-425ae1f48caf}\python-3.12.10-amd64.exe" /uninstall /quiet`

## 回归

回归在 `6699fc2` 的导出源码上运行，使用隔离的状态目录，设 `PRISM_SKIP_TEX=1`。9333 端口被另一个会话的 Chrome 占着，没有去动它；导出副本里的浏览器测试改用 9448 端口，测试内容不变：

- 单元测试：[676 项，跳过 31 项，无失败](full-tests.txt)（上一阶段是 616 项）
- Node 检查：8 项全部通过
- 浏览器：[112 项通过](browser.txt)

跑完后再查：Documents 下没有新的文件夹，`%LOCALAPPDATA%\Prism` 不存在。

## 同日的上一轮修复（新用户上手的审计）

[新用户上手](../2026-10-17-onboarding/report.md)完成后的审计发现的问题，在本阶段之前已经修复，回归包含它们：

| 提交 | 修复 |
|---|---|
| `01195d6` | 没装 git 或 GitHub CLI 时，页面显示那句说明（424），不再是 "file not found"（404） |
| `035901a` | 登录 GitHub 后，让 git 用这次登录的设置会重试一次；仍失败时登录照样完成，并给一句不带术语的提醒。登录启动中点取消也能结束 |
| `edfa685` | Getting started 的按钮点完恢复可用；Check again 重新问 GitHub 是否已登录；上面的提醒只弹一次；设置页列出 git 和 GitHub CLI |
| `3033491` | 同时点两次 Install 只装一次 |
| `fbd4337` | 同一个仓库同时从 GitHub 打开两次只复制一份（原来第二次会出错） |

## 需要在真实环境里验证

1. **干净的 Windows 账户或虚拟机**：在 Windows PowerShell 5.1 和 PowerShell 7 里各粘贴一次那一行，到 Home 出现为止；用户名带空格或中文时也要试；winget 的 `Python.Python.3.12 --scope user` 和 `Git.Git`（包括权限窗口）；没有 winget 时的 python.org 方案；Quick start 后编译 `examples/minimal`；Full LaTeX（MiKTeX）在一小时内装完，并且 `build.tex_dirs()` 找得到它；Claude 登录窗口能打开。
2. **干净的 Mac**：开发者工具的安装窗口只出现一次；`make_macos_app.py` 在开发者工具自带的 Python 3.9 下能用；Apple Silicon 上下载的 Tectonic 和 gh 能通过 Gatekeeper 运行；python.org 的 Python 没运行 "Install Certificates" 时，HTTPS 下载会不会失败；用真实的 `curl … | sh` 跑一遍（macOS 的 `/bin/sh` 是 sh 模式下的 bash 3.2）。
3. **Linux**：菜单项出现、启动器打开 Home；各发行版的包名（特别是 zypper 的 `python3-tk`）。
4. **GitHub 的速率限制**：不登录时每小时 60 次 API 请求；装一次 Tectonic 或 gh 各用 1 次。今天查询时匿名请求就被拒过一次（403），所以这一点值得在真实环境里留意。
