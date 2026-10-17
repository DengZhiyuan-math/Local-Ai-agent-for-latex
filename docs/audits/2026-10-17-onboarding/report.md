# 新用户上手：结果

日期：2026-10-17。方案见 [plan.md](plan.md)。5.1–5.5 已全部完成，每项一个提交。真实安装、真实登录和第二台电脑上的操作需要在真实环境里验证，见文末。

## 完成的项

| 项 | 提交 | 现在的行为 | 测试 |
|---|---|---|---|
| 5.1 | `01f15bf` | `tools.py` 检测 git 和 GitHub CLI。Windows 上用 winget 在后台安装（`GET /api/tools`，`POST /api/tools/install`）。不能自动安装时（没有 winget，或在 macOS/Linux 上），用一句话说明怎么手动装。运行中刚装好的程序不用重启 Prism 也能找到：会从注册表重读 Windows 的 PATH，并补上安装程序的常用目录。没装 git 时，原来显示 `[WinError 2] The system cannot find the file specified`，现在显示 "Git is not installed on this computer. Install it from the Home page."（`errors.NotInstalled`，仍是 OSError，调用方照旧捕获）。gh 的状态提示也改为指向 Home，不再出现命令 | `test_tools`（新增 5 项，winget 用替身，不真装），`test_github` 改写 |
| 5.2 | `1b8c561` | `GITHUB.login()` 在后台运行 `gh auth login --web`，读出一次性代码，交给页面显示。授权完成后运行 `gh auth setup-git`，第一次推送时不会再要求登录一次。被拒、取消、15 分钟未确认都结束这次登录，并各给一句说明；gh 的原文放在 Details 里 | `test_github.LoggingIn`（新增 2 项，用假的 gh 走完整个设备码流程） |
| 5.3 | `b90aa71` | Home 的"Getting started"面板。三项（git、GitHub CLI、GitHub 账户）没有全部就绪时，显示在列表上方，每项一个按钮：Install，或 Log in to GitHub（显示代码，并给出 GitHub 页面的链接）。失败时给一句话加 Try again，技术细节折叠。全部就绪后面板消失。新建项目和发布对话框不再让人去设置页登录 | 浏览器新增 1 项（安装中、安装失败、安装完成、显示代码、登录后面板消失） |
| 5.4 | `3082117` | Home 的"From GitHub…"列出你在 GitHub 上的仓库，可以筛选；选中一个，克隆到默认目录（同名时加 "(2)"）。克隆下来的仓库标记为 managed，装上禁止强推的钩子，同步目标在第一次同步时自动绑定。Prism 的共享仓库会还原成 Home 上的共享文件夹：子文件夹照原样建好，每个含 `\documentclass` 的文件夹加为一个项目。已在列表里的同一仓库只打开，不重复克隆 | `test_hub.OpenFromGitHub`（新增 2 项），浏览器新增 1 项 |
| 5.5 | `7ab1e9a` | README：安装只需要 Python 和 TeX，git 和 GitHub CLI 由 Home 来装；没有 git 时可以 Download ZIP，但这样得到的副本不会自动更新；GitHub 一节改为 Getting started、Log in to GitHub、From GitHub | — |

## 执行中的一次事故

5.4 的第一个测试第一次运行时，把仓库克隆到了 `C:\Users\dengz\Documents\paper`。原因是测试里没有设默认目录，项目列表清空后，`default_parent` 退回到 `~/Documents`。产品逻辑没有问题，是测试没有隔离好。已修复：测试的 `setUp` 把默认目录设到临时目录，两个测试也都断言克隆位置在临时目录之内；此后多次运行，Documents 下没有再出现新的文件夹。

**这个文件夹需要你手动删除**（自动删除被权限规则拒绝了）：`C:\Users\dengz\Documents\paper`。它是测试生成的：origin 是假的 `https://github.com/me/paper.git`，里面只有一个 "Initial commit"，创建于 2026-10-17 15:32。

## 回归

回归在 `7ab1e9a` 的导出源码上运行，使用隔离的状态目录，并设 `PRISM_SKIP_TEX=1`：

- 单元测试：[611 项，跳过 31 项，无失败](full-tests.txt)
- Node 检查：全部通过
- 浏览器：[109 项通过](browser.txt)

跑完后再查，Documents 下没有新的文件夹。

## 需要在真实环境里验证

最好在一个没装过 git 和 gh 的 Windows 账户或虚拟机里验证：

1. 打开 Home，Getting started 面板出现；点 git 的 Install，Windows 会请求一次权限，装完后该行打勾；GitHub CLI 同样。
2. 点 Log in to GitHub，页面显示代码；点 Open GitHub，输入代码并授权后，面板消失。然后新建一个项目并建立 GitHub 仓库，第一次推送不应再弹出登录框。
3. 在另一台电脑上（或另一个文件夹），用 From GitHub… 打开刚才的仓库，编辑后应能自动同步回去。

已核实：本机真实的 gh 2.90 在没有终端时运行 `auth login --web`，会打印 "! First copy your one-time code: XXXX-XXXX" 和 "Open this URL to continue in your web browser: https://github.com/login/device"，格式与解析代码一致。核实时用的是临时 gh 配置目录，在读到代码后就结束了进程，没有完成登录，你现有的登录不受影响，临时配置目录中也没有写入任何内容。授权完成之后的步骤（`setup-git`、首次推送）仍需在真实环境中验证。
