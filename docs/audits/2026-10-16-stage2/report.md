# 第二阶段 B4、C、H、G 及其后的修复

日期：2026-10-15 至 2026-10-16（Europe/Berlin）。范围：[审计遗留问题](../2026-10-15-followups/report.md)（`5376989`）之后的 24 个提交，至 `aa54609`。均未推送。

第一阶段（`2cbe5f4`）和第二阶段 A、E、B1–B3、F、D 见此前的报告。本报告的验证在 `aa54609` 上重新运行；每个提交的说明另有当时的验证记录。

## 提交

### B4：历史回合只保留撤销需要的数据

| 提交 | 内容 |
|---|---|
| `f5fc744` | 最近 50 个回合的撤销只保留根目录与改动文件的前后内容（`Undoable`）；完整的 `Job`（事件记录、进程、提示）只保留进行中的回合和最近 8 个结束的回合 |

### C：服务器里的 `Project`

`server.py` 从 1439 行减到 611 行。

| 提交 | 内容 |
|---|---|
| `d56fb76` | `synctex.py`：SyncTeX 文件按路径、时间戳、大小只读一次，返回不可变的记录，查询无需加锁 |
| `f9b22cb` | `errors.py`（`Refused` 400、`NotFound` 404、`Conflict` 409）；错误到状态码只在 `httpbase` 一处转换；缺参数为 400 并说明参数名，`KeyError` 不再被当成 400 |
| `488b571` | `project.py`：项目目录、设置和文件列表合成 `Project`；`prism.json` 改动后无论谁来读都重新读取；显示的文档只经 `Project.show` 在锁内修改 |
| `d8e9d22` | 保存、上传和文件菜单归入 `Project`，依赖由外部传入，测试不需要服务器全局变量 |
| `b45edba` | `symbols.py`：源文件扫描；各文件扫描结果按项目缓存 |
| `5b33792` | 编译与“项目是否忙”归入 `Project`；重启、改名、合并冲突、空闲退出共用 `Project.busy` |
| `1ae0a3c` | 路由测试在进程内运行；越界路径为 `Refused`。并修正一个遗留的跳过装饰器（见下文“过程中发现的问题”） |

### H：把 `GitSync` 拆成同步目标、合并、调度

`gitsync.py` 从 1173 行减到 863 行。

| 提交 | 内容 |
|---|---|
| `e29273c` | `synctarget.py`：同步目标的判定和确认；读取或确认目标不再需要构造整个同步对象 |
| `d14d45f` | `repository.locate`：项目在 git 里的四种情况（自有、共享、嵌套、无仓库）只判断一次，原来 4 处重复 |
| `2794a3b` | `integrate.py`：全部合并操作；中止合并只写一次 |
| `5105a77` | `syncschedule.py`：提交、推送、拉取的时机由一个调度器决定，时钟由外部注入 |
| `70e6998` | 推送和拉取在网络传输期间只持网络锁，推送时点“发送”不再卡住 |
| `cde580e` | 修复 `70e6998` 引入的回归（见下文） |
| `cb27fab` | 测试只走公开接口；Home 与发布读取项目自己的输出目录 |

### G：前端拆分

页面上的判断移入不依赖页面的模块，由 `tests/frontend_check.js` 在 Node 下检查；`tests/browser/` 在无界面 Chrome 中驱动真实页面。

| 提交 | 内容 |
|---|---|
| `6a9fd9b` | `util.js`：两个页面共用的辅助函数（原有多份互不一致的副本）；Node 检查与浏览器检查的设施 |
| `f182352` | `syncstate.js`、`chatlogic.js`、`homeview.js` |
| `b1d5b4f` | `tabs.js`：打开的文件及其保存；保存失败时说明真实原因 |
| `f0efc0a` | `client.js`：每个路由一个函数；测试核对每个请求都是 `server.py` 处理的路由和方法 |
| `3e9b938` | `turn.js`：Agent 回合的事件显示什么（原计划只记录、不拆分的 reduceTurn） |

### 计划之外

| 提交 | 内容 |
|---|---|
| `3b5e80b` | 同一编辑器开约 6 个页面时卡住：浏览器对同一服务器最多 6 个连接，每页各占一条在线状态连接。现在同一服务器的页面共用一条（Web Lock 选出持有者，关闭后由下一页接手）；隐藏页面跟随回合时不占连接 |
| `ea9b97c` | 浏览器检查不再把测试文件放进系统回收站（`PRISM_TRASH_DIR`） |
| `d74eea1` | 浏览器检查不再覆盖系统剪贴板（`PRISM_CLIPBOARD_FILE`） |
| `aa54609` | Windows 上，Agent CLI 和编译启动的程序放进 Job Object：CLI 退出后留下的命令、编译留下的程序都会被结束，服务器崩溃时也随之结束 |

## 过程中发现的问题

| 问题 | 发现方式 | 处理 |
|---|---|---|
| `70e6998` 让后台定时同步可以插进按钮的重试之间；合作者同时推送时，按钮 5 次重试用尽，报 “GitHub has changes this copy does not have yet” | 最终验收的压力测试：同样负载下 60 轮失败 17 轮，`5105a77` 为 60/60。刚提交时跑过的 100 轮恰好负载低，没有暴露 | `cde580e`：推送、拉取和 `resolve_mine` 重新一次只做一个，提交不等它们，“发送”不卡住的效果保留 |
| 页面恢复上次的标签时，你点开的文件可能又被切回上次记住的文件，打的字进了那个文件 | G3 的浏览器保存检查 5 次失败 3 次。一度怀疑是被停止的回合收尾时覆盖保存（可能丢数据），排查后排除 | `b1d5b4f`：以最后点开的文件为准，其余只作为后台标签恢复 |
| `publish.project_sync` 把输出目录写死为 `build/`，输出目录另设的项目，其 PDF 和日志被当成源文件 | H 收尾时检查测试改走公开接口 | `cb27fab` |
| SyncTeX 测试移到 `test_synctex.py` 后，一个需要真实 TeX 的跳过装饰器落到了路由测试上：`d56fb76` 至 `5b33792` 的完整回归在 `PRISM_SKIP_TEX` 下跳过了 SyncTeX 路由测试（专项运行中它运行且通过） | C 的路由测试改写 | `1ae0a3c` 移除该装饰器 |
| 同一编辑器开多个页面会卡住 | G3 浏览器检查中用探针复现：多开 6 个页面时新页面 `openFile is not defined` | `3b5e80b`；`many_pages.mjs` 在改回每页一条连接时失败 |
| 浏览器检查自身：录制回合的会话 id 写进真实 provider，后续检查发消息被服务器拒绝 | 新检查 `turn_view.mjs` 首次运行时连带 `many_pages` 失败两次 | 录制回合使用单独的假 provider；仅测试代码 |

## 验证

运行环境：Windows 11，Python 3.14.2，Node 24.19.0，Chrome 155.0.8059.80，Git 2.45.1，`codex-cli 0.163.0-alpha.10.2`（上一份审计为 0.162）。

| 检查 | 结果 | 记录 |
|---|---|---|
| 完整回归（`PRISM_SKIP_TEX=1 python -m unittest discover -s tests -v`） | 502 项，472 通过、30 跳过，0 失败、0 错误，444.5 秒；无资源警告 | [full-tests.txt](full-tests.txt) |
| 原生 Codex 沙盒与 Codex 专项（`PRISM_NATIVE_CODEX=1`，从 `tests/` 运行） | `test_codex_runtime` 9 项、`test_backend_contracts` 13 项、`test_codex_discovery` 14 项，全部通过，无跳过 | [native-tests.txt](native-tests.txt) |
| 真实 TeX（不设 `PRISM_SKIP_TEX`） | `test_build` 34 项（2 项 Linux/macOS 专用跳过）、`test_synctex` 8 项，全部通过 | [tex-tests.txt](tex-tests.txt) |
| 浏览器端到端（`python tests/browser/run.py <空目录>`） | 9 组检查，连续 3 次全部通过：文件菜单 16、重载后接回回合 6、回合显示 8、保存 7、Home 菜单 8、页面加载 10、多页面 6、回收站目录 1、剪贴板文件 1 | [browser.txt](browser.txt) |
| 同步压力测试：`test_sync_buttons…test_buttons_while_typing_and_a_coauthor_pushes` 重复运行，每轮核对输入的行和合作者的修改都到达远端、按钮无错误、仓库无未完成状态 | 100/100 通过，2 个独立进程并发，696.5 秒 | [stress-results.json](stress-results.json) |
| 用户实测 | 在编辑器中用 Codex 真实完成一次 Edit 回合；“Update: restart” 成功。时间在 `3e9b938` 之后、`d74eea1` 之前 | — |

30 项跳过：需要 TeX 12 项（完整回归设了 `PRISM_SKIP_TEX`，已在上表单独运行并通过），需显式开启的原生 Codex 沙盒 9 项（已单独运行并通过），Linux/macOS 专用 8 项，需要符号链接 1 项。

浏览器检查使用隔离的服务器：`PRISM_STATE_DIR`、`PRISM_AGENTS`、`PRISM_TRASH_DIR`、`PRISM_CLIPBOARD_FILE` 都指向临时工作目录，Agent 回合使用本地慢速假 API，不使用真实模型，不碰你的项目列表、回收站和剪贴板。

新检查在旧代码上失败、修复后通过的有：
- `test_proc.py` 中“进程退出后留下的程序被结束”（Windows）；
- `test_agent.py` 中两条“CLI 留下的命令已结束”；
- `test_build.py` 中“编译留下的程序已结束”；
- `turn.js` 的 Node 检查（故意让回复画两次、思考框不关时失败）；
- `client.js` 的路由检查（故意写错一个路由时失败）；
- `many_pages.mjs`（改回每页一条连接时失败）。

## 未验证与边界

- `aa54609`（Job Object）之后没有再做真实的 Codex Edit 回合，只有原生沙盒测试。Codex 若启动要求脱离 Job 的进程，允许离开，这类进程不受管。
- `aa54609` 起，Linux/macOS 上 CLI 留下的命令在回合结束时总会被结束（此前只在它们占着输出时）。本轮没有在 Linux/macOS 上运行。
- 回合中由 Agent 打开的程序（例如用 `start` 打开的 PDF 阅读器）会在回合结束时关闭。
- `3b5e80b`：未持有在线连接的页面若非正常关闭（浏览器崩溃），服务器最多约 2 分钟后才不再计入。只影响 Home 上“编辑器已打开”的标记，以及“编辑器开着时不能移到回收站”。
- 发布期间仍持有 `GITSYNC.lock`：同期的自动提交等待，GitHub 菜单最多等 2 秒后先按已知状态回答，保存不受影响。发布每个项目一次，维持为可接受的限制。
- Home 删除项目时，若其编辑器仍开着则拒绝，不自动关闭。
- macOS 的剪贴板、废纸篓未在 macOS 上运行；Linux 的复制粘贴只在同一项目内可用。
- `test_backend_contracts` 要从 `tests/` 目录运行（`python -m unittest discover -s tests -p ...`）；用 `python -m unittest tests.test_backend_contracts` 会因找不到 `test_agent` 而加载失败。此前即如此，未改。
