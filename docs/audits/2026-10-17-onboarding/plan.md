# 新用户上手：方案

日期：2026-10-17。来源：[上一阶段报告](../2026-10-17-next/report.md#1b4-调查不懂-git-的新用户从零开始)的 1b.4 调查。产品原则同上一阶段：不懂 git 的人也能用，Prism 能做的自己做，必须让用户做时只给一句话和一个按钮。执行结果写入同目录的 `report.md`。

## 目标

一个从没用过 git 的新用户，在 Windows 上装好 Prism 和 TeX 之后，只在 Prism 里点按钮，就能完成：装好 git 和 GitHub CLI，登录 GitHub，给论文建私有仓库，在第二台电脑上打开同一篇论文。

## 5.1 工具检测与一键安装（`tools.py`）

- `tools.status()` 返回 `{git, gh, can_install}`：
  - `git`、`gh`：找到的程序路径，或 None。
  - `can_install`：Windows 上 `winget` 是否可用。
- 刚装好的程序不在已运行进程的 PATH 里。所以每次检测前，从注册表重读 PATH（Machine 加 User），并补上两个常见安装目录：`C:\Program Files\Git\cmd`、`C:\Program Files\GitHub CLI`。装完之后不用重启 Prism。
- `tools.install(name)`：在后台运行 `winget install --id Git.Git` 或 `GitHub.cli`，参数 `-e --silent --accept-package-agreements --accept-source-agreements`。用 `proc.run`，时限 15 分钟。进度和结果放在 `tools.INSTALL[name]` 里，页面轮询读取。
- 不能自动安装时（没有 winget，或在 macOS/Linux 上），给一句话加一个链接或命令：macOS 用 `xcode-select --install` 装 git、`brew install gh` 装 GitHub CLI，Linux 用系统的包管理器。
- git 不在时，`errors.say` 不再显示 `[WinError 2] The system cannot find the file specified`，改为"git is not installed; install it from the Home page"。
- **测试**：用假的 `winget` 和临时 PATH 测检测、安装命令、PATH 刷新，以及 git 缺失时的提示文字。不真的安装任何东西。

## 5.2 在 Prism 里登录 GitHub（`github.py`）

- `GITHUB.login()`：在后台运行 `gh auth login --web --hostname github.com --git-protocol https --skip-ssh-key`，从输出里读出一次性代码和网址，放进 `GITHUB.LOGIN`；页面显示代码，并打开 `https://github.com/login/device`。gh 等待用户在网页上授权；成功后运行 `gh auth setup-git`，让 git 推送时用这次登录，不再弹第二次登录框。然后刷新登录状态。
- 超时（15 分钟）或用户取消时结束进程，并说明原因。
- **测试**：用假的 gh，打印代码、等一个信号文件、然后退出，测代码的解析、成功、超时和 `setup-git` 的调用。真实流程放进第 3 阶段的验收。

## 5.3 Home 的"开始使用"面板

- 三项全部就绪之前，Home 列表上方显示一个面板，逐项列出：
  - git：已装 ✓，或按钮"安装"；
  - GitHub CLI：已装 ✓，或按钮"安装"；
  - GitHub：已登录 ✓（显示账户名），或按钮"登录"。
- 安装中、等待授权时显示状态；某一步失败时，显示一句话和"重试"按钮，技术细节折叠起来。
- 全部就绪后面板消失；设置页保留同样的状态，供以后查看。
- 现有提示里的 "Run: gh auth login"、"Install it from https://cli.github.com" 都改成指向这个面板。
- **测试**：浏览器测试 mock `/api/tools` 和 `/api/github/login`，按顺序走完三项。

## 5.4 从 GitHub 打开（第二台电脑）

- Home 新增"从 GitHub 打开…"：
  - 用 `gh repo list --json nameWithOwner,name,updatedAt,isPrivate --limit 200` 列出你的仓库。
  - 用户选一个，Prism 用 `git clone https://github.com/<owner>/<name>.git` 克隆到默认目录。目标目录已存在时，在名字后加 (2)。
  - 克隆后：标记为 managed，装上禁止强推的保护（`install_guard`），同步目标由第一次同步自动绑定。
  - 加入列表。若是 Prism 的共享仓库（顶层有 `prism-repo.json`），把其中每个含 `\documentclass` 的文件夹（深度 ≤ 3）作为项目加入，并在 Home 建一个同名文件夹，设为共享。
- 已在列表里的仓库（同一个 GitHub 地址）不重复克隆，直接打开。
- **测试**：用 `FakeGitHub` 列出仓库，克隆本地裸仓库，检查 managed 标记、保护、项目列表，以及共享仓库展开成多个项目。

## 5.5 文档

README 的安装一节改成：装好 Python 和 TeX 后打开 Home，按面板操作。命令行安装方式保留给熟悉的用户。

---

## 顺序

```
5.1 → 5.2 → 5.3（依赖 5.1、5.2 的接口）→ 5.4 → 5.5
```

每项一个提交；最后跑完整回归，写 report.md。

## 需要在真实环境验证（列入第 3 阶段）

- 用 winget 真实安装 git 和 GitHub CLI（最好在一个没装过的 Windows 账户或虚拟机里）。
- `gh auth login --web` 的真实设备码流程，以及登录后 git 推送不再弹框。
- 在第二台电脑上从 GitHub 打开一个真实的私有仓库。
