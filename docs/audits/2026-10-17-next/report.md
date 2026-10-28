# 下一阶段工作结果

日期：2026-10-17。方案见 [plan.md](plan.md)。第 1、1b、2、4 阶段已完成，每项一个提交；第 3 阶段（真实环境验收）需要你来操作，尚未进行。

## 第 1 阶段：正确性

| 项 | 提交 | 修复后的行为 | 测试 |
|---|---|---|---|
| 1.1 | `0746817` | 新建 GitHub 仓库时，在仓库建立之前就把绑定写成 Prism 所建的仓库（`github.com/<owner>/<name>` 加当前分支），并设好分支的上游。此后 Home 或编辑器检查目标时，看到的都是已绑定状态，不会再把配置里的其他仓库绑定上去。配置指向别处时：不推送，用白话说明；同步暂停，配置恢复一致后自动继续，不需要确认。GitHub 没建成时撤回绑定 | `test_publish`（3 项改写，含 Home 在创建过程中查看），`test_git_target`；旧代码上 Home 的那次查看会绑定 `me/other` |
| 1.3 | `8c7480b` | Deep Code 低于 0.4.3 时拒绝启动，并给出升级命令。版本号从 `cli.js` 旁的 `package.json` 读，不起进程。读不出版本时照常运行 | `test_agent`（新增 1 项，5 种版本） |
| 1.4 | `ab52459` | **做法与方案不同。** fetch 和 push 拿到网络锁之后、git 启动之前，重新读取并校验目标（`_checked_target`）。换成另一个仓库就暂停；同一仓库只是地址写法变了，按新的写法传输。方案原定"按校验过的 URL 推送"，没有采用，原因有两个：保存的 URL 是去掉了凭据的，按它推送会认证失败；git 对命令行上直接给出的 URL 也会套用 `insteadOf` 改写，推送的地址仍然不是校验过的那个。实测还发现，原先校验与推送之间的空隙不是毫秒级，而是"等网络锁"的时间，另一个窗口传输期间可以长达几分钟 | `test_git_target`（新增 1 项）；旧代码上这一项推到了另一个仓库 |
| 1.5 | `0015132` | 项目位于别的仓库里面（kind 为 nested）时，agent 的 git 设了 `GIT_CEILING_DIRECTORIES`（值为项目的父目录），找不到外层仓库。项目有自己的仓库时照常 | `test_git_target`（新增 1 项） |
| 1.2 | 并入 1b.3 | README 的同步一节已整体重写 | — |

## 第 1b 阶段：把 git 藏起来

| 项 | 提交 | 结果 | 测试 |
|---|---|---|---|
| 1b.1 | `54135f7` | `.git/prism-local.json` 记录 `"managed": true`。写入的时机有两个：`git_init` 新建仓库时（新项目、合并后的共享仓库、拆分后的各个仓库），以及 `create_github_repo` 为仓库设置 GitHub 远端时。已有的仓库不补记 | `test_publish`（新增 1 项：Prism 建的、终端建后由 Prism 发布的、终端 clone 的） |
| 1b.2 | `c3d7b06` | Prism 管理的仓库，以下情况自动修复后继续同步，并给页面一条白话通知：上游指向别处、`pushRemote`/`pushDefault`/mirror 指向别处、GitHub 远端被删（按地址加回）、HEAD 脱离在分支的提交上或其后。最后一种情况把分支移到 HEAD，不改文件，也不丢提交。以下情况不碰，仍然暂停：远端地址指向另一个仓库、切到了别的分支、HEAD 在历史中的其他位置、用户自己的全局设置。页面的定时查看（`status()`）不做任何修复。建仓库时遇到偏差，也先尝试修复再报错 | `test_git_target`（新增 2 项），`test_publish`（改写 1 项：分支被改后，自动修复并推送成功） |
| 1b.3 | `6509de4` | 暂停原因统一由 `synctarget.plain()` 给出白话；失败信息第一行是白话，git 的原文放在后面。`status()` 和按钮的应答都分成 `error` 与 `error_details`。编辑器把技术信息折叠在 Details 里；菜单改为 Save to GitHub now / Get changes from GitHub / Save to GitHub by itself / Sync with the new one。Home 卡片显示"全部在 GitHub 上"或"还没全部上传"，分支和计数放进提示；"Upload paused"与"GitHub not reached"分开显示（此前同步暂停时也显示后者）。README 的同步一节按不懂 git 的读者重写，另附一段给懂 git 的人 | 相关单元测试和浏览器测试按新文字更新 |

## 第 2 阶段：测试欠账

| 项 | 提交 | 结果 |
|---|---|---|
| O9、O10 只做过语法检查 | `e729614` | 浏览器测试各加一项：置顶保存失败后恢复原状并提示；"server not reachable"在服务恢复后消失 |
| 两个子进程不关句柄 | `8139998` | 已关闭。在 `PYTHONWARNINGS=always::ResourceWarning` 下，修复前 2 条警告，修复后 0 条 |
| `test_backend_contracts` 只能从 `tests/` 运行 | `8139998` | 文件内自行加入路径，从仓库根目录直接运行通过 |
| `test_proc` 依赖 tasklist | `8139998` | 改用 `OpenProcess` 加 `WaitForSingleObject` 判断；存活、已结束、本进程、不存在的 PID 四种情况均正确 |

## 第 4 阶段：维护

- `ac3d6ff`：`.claude/worktrees/` 加入 `.gitignore`。旧 worktree 仍需你删除（自动删除被拒绝）。
- 推送到 origin：还没有推，等你决定。

## 1b.4 调查：不懂 git 的新用户从零开始

在本机核对代码，并模拟 PATH 中没有 git 的情况：

| 情况 | 现在用户看到什么 | 建议 |
|---|---|---|
| 没装 git | Prism 不检测。项目显示"Not on GitHub"；新建仓库时报 `[WinError 2] The system cannot find the file specified` | Home 启动时检测。缺 git 时顶部给一句话和一个按钮"安装 git"：Windows 上由 Prism 运行 `winget install Git.Git`，或者随 Prism 附带便携版 MinGit |
| 没装 GitHub CLI | "GitHub CLI not found. Install it from https://cli.github.com, then run: gh auth login" | 同上，提供一键安装（`winget install GitHub.cli`）。长期可以考虑不依赖 gh：用 GitHub 的 device flow 登录，再用 REST API 建仓库 |
| 没登录 GitHub | "GitHub CLI is not logged in. Run: gh auth login"，要求在终端操作 | 设置里放一个"登录 GitHub"按钮：Prism 在后台运行 `gh auth login --web`，把一次性代码显示在页面上，并打开 github.com 的授权页。需要在真实环境确认 gh 能这样非交互运行 |
| 第一次新建仓库 | Home 的"+ GitHub"已经可用；1.1、1b.3 之后提示都是白话 | — |
| 第二台电脑打开同一个项目 | **没有这个功能**，只能在终端 `git clone`，或用别的工具 | Home 加"从 GitHub 打开"：列出账户下的仓库（`gh repo list`），由 Prism clone 到默认目录，标记为 managed 并加入列表 |

这一项按方案只做调查。实现另起方案，并需要真实环境验证（安装、登录流程）。

## 回归

回归在 `ac3d6ff` 的导出源码上运行，使用隔离的状态目录，并设 `PRISM_SKIP_TEX=1`：

- 单元测试：[602 项，跳过 31 项，无失败](full-tests.txt)
- Node 检查：全部通过
- 浏览器：[107 项通过](browser.txt)

真实 TeX 编译和真实模型调用不在其中，留给第 3 阶段。

## 剩余

- 第 3 阶段真实环境验收：需要你来操作。
- 推送到 origin：等你决定。
- 1b.4 的实现：另起方案。
