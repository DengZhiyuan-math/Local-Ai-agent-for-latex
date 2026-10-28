# 一行安装：方案

日期：2026-10-17。来源：[新用户上手](../2026-10-17-onboarding/report.md)之后的讨论：不打包成 app，而是把安装压到"粘贴一行命令"，其余都在 Home 的 Getting started 里一键完成。产品原则同前：不懂 git 的人也能用，Prism 能做的自己做，提示里没有 git 术语。执行结果写入同目录的 `report.md`。

## 已定的选择

- **Prism 装在哪里**：用户看不到的程序目录。Windows `%LOCALAPPDATA%\Prism\prism-local`，macOS `~/Library/Application Support/Prism/prism-local`，Linux `${XDG_DATA_HOME:-~/.local/share}/prism/prism-local`。用 git 克隆，所以照旧自动更新（`selfupdate.py` 不变）。
- **LaTeX 默认**："快速开始"下载 Tectonic（一个文件，不要管理员权限，宏包第一次编译时自动下载；`build.py` 找不到 TeX 时已经用它）。"完整安装"（MiKTeX / MacTeX）是第二个按钮。已有 TeX 的直接打勾。
- **Prism 自己下载的程序**（Tectonic；macOS 和 Linux 上的 GitHub CLI）放在 `registry.state_dir() / "bin"`，所有平台都加到 PATH（`tools.refresh_path`），Home 和每个编辑器启动时都调用。

## 6.1 Windows 一行安装（`install.ps1`，仓库顶层）

```
irm https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.ps1 | iex
```

1. Python ≥ 3.9（带 tkinter：Windows 的选择文件夹对话框要用）：找真正的 Python（跳过 Microsoft Store 的别名）；没有就 `winget install Python.Python.3.12 --scope user`；没有 winget 就下载 python.org 的安装程序，只为当前用户静默安装。
2. git：没有就 `winget install Git.Git`。仍没有 git（没有 winget）时，下载 ZIP 解压，并说明以后在 Home 装好 git 后会自动改为可更新的副本（本阶段只说明，不做转换）。
3. 克隆到上面的目录；已在时只更新（fast-forward）。
4. 运行 `launcher\make-shortcut.ps1`（开始菜单和桌面的 Prism），然后打开 Home。
5. 每一步一句话说明；失败时说明怎么办。参数 `-Dir`、`-NoShortcut`、`-NoLaunch` 供测试和高级用户使用。
- **测试**：`tests/test_install_ps1.py`，用 pwsh 运行，PATH 上放假的 winget / git / python，目标目录在临时目录；不真装任何东西，不写真实的开始菜单。

## 6.2 macOS 和 Linux 一行安装（`install.sh`，仓库顶层）

```
curl -fsSL https://raw.githubusercontent.com/DengZhiyuan-math/Local-Ai-agent-for-latex/main/install.sh | sh
```

1. macOS：python3 和 git 来自"命令行开发者工具"：缺少时运行 `xcode-select --install`，等它装完（最多 30 分钟）再继续。Linux：缺少时说出那一条 apt / dnf / pacman 命令并退出。
2. 克隆或更新到上面的目录。
3. macOS：`launcher/make_macos_app.py`（`/Applications` 不可写时用 `~/Applications`）；Linux：`launcher/make_desktop_entry.py`。然后打开 Home。
4. POSIX sh，不依赖 bash。环境变量 `PRISM_DIR`、`PRISM_NO_LAUNCH=1`、`PRISM_NO_SHORTCUT=1`；测试用 `PRISM_INSTALL_OS`（Darwin / Linux）模拟平台。
- **测试**：`tests/test_install_sh.py`，用 sh 和假的 git / python3 / xcode-select / uname 运行，目标目录在临时目录。

## 6.3 后端：LaTeX、GitHub CLI 下载、AI（`tools.py`）

`GET /api/tools` 的回答扩充为（旧字段含义不变，`can_install` 改为按程序给出）：

```
{
  "git": path|null, "gh": path|null,
  "tex": {"engine": path|null, "tectonic": path|null},     # engine: pdflatex/xelatex/lualatex，含 build.tex_dirs()
  "claude": path|null,
  "can_install": {"git": bool, "gh": bool, "tectonic": bool, "tex": bool, "claude": bool},
  "installing": {name: {"state": running|done|failed, "error", "details"}},
  "how": {"git": str, "gh": str, "tectonic": str, "tex": str, "claude": str},
  "github": {...}, "login": {...}
}
```

`POST /api/tools/install {"name"}`，name 为 `git`、`gh`、`tectonic`、`tex`、`claude`：

- `tectonic`：所有平台。从 GitHub 上 tectonic-typesetting/tectonic 的最新 `tectonic@…` 发布下载本平台的文件（Windows x86_64 zip；macOS x86_64 / aarch64；Linux x86_64 / aarch64 musl），解压到 `state_dir()/bin`。
- `gh`：Windows 照旧 winget；macOS 和 Linux 从 GitHub 上 cli/cli 的最新发布下载，用发布里的 `checksums.txt` 校验 SHA-256，放进 `state_dir()/bin`。
- `tex`：Windows `winget install MiKTeX.MiKTeX`；macOS 有 Homebrew 时 `brew install --cask mactex-no-gui`，否则不可自动安装，`how` 给出 tug.org/mactex；Linux 不可自动安装，`how` 给出包名。
- `claude`：官方安装命令（Windows `irm https://claude.ai/install.ps1 | iex`，其他 `curl -fsSL https://claude.ai/install.sh | bash`）。
- 下载只用标准库（urllib），有时限，写到临时文件再改名；失败时一句话加 Details。
- `POST /api/tools/claude-login`：打开一个终端窗口运行 `claude`，让用户登录一次（Windows 新控制台；macOS Terminal；Linux 常见终端，找不到时 Refused 并说明）。
- `build.py` 找不到 TeX 时的提示改为指向 Home 的 Getting started。编辑器服务器启动时调用 `tools.refresh_path()`，找得到刚下载的 Tectonic。
- **测试**：本地 HTTP 服务器冒充 GitHub 的发布接口和下载，测 Tectonic 和 gh 的下载、解压、校验失败、`can_install`；不访问网络。

## 6.4 Home 的 Getting started 和设置页

- 新增 **LaTeX** 一行：已就绪时打勾（显示用的是 TeX 还是 Tectonic）；否则主按钮"Quick start (Tectonic)"，次按钮"Full LaTeX"（不可自动安装时给出 `how` 的一句话）。
- **AI 助手**一行，标"可选"：没有 claude 时"Install"；装好后"Log in"（`/api/tools/claude-login`）。这一行不影响面板何时消失：git、GitHub CLI、GitHub 账户和 LaTeX 都就绪后面板消失。
- `can_install` 按程序读取；macOS 和 Linux 上 GitHub CLI 也是一个 Install 按钮。
- 设置页的状态区同样列出 LaTeX 和 AI 助手。
- **测试**：`tests/browser/home_menu.mjs` mock `/api/tools`，覆盖新行。

## 6.5 文档

README 的安装一节以一行命令开头，原来的分步安装保留给熟悉的用户。

---

## 顺序

6.1、6.2、6.3、6.4 互不改同一文件，并行进行；6.4 依赖上面定下的 `/api/tools` 格式。最后 6.5，跑完整回归，写 report.md。

## 需要在真实环境验证

- 干净的 Windows 账户或虚拟机：粘贴一行，到 Home 出现；Quick start 后编译 `examples/minimal`。
- 干净的 Mac：开发者工具的安装窗口、`.app`、Tectonic 和 GitHub CLI 的下载。
- 真实的 GitHub 发布文件名是否与解析一致（tectonic、cli/cli）。
