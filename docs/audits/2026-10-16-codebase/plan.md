# 代码库整理与审计修复方案

日期：2026-10-16。三份只读审计：编辑器前端、服务端（编辑器服务、Home、编译、git 同步）、AI agent 后端。每份报告里最严重的几条已在代码中核对属实；agent 报告里 `snapshot.py` 的行号有偏差，但问题本身成立（预算在 `snapshot.py:70-89` 按遍历顺序扣减）。以下保留制定时的方案；执行结果记录在 [report.md](report.md)。

## 目标

1. **稳定**：不写错项目、不丢编辑、不永久卡死、不把出错说成成功或把停止说成出错。
2. **性能**：空闲时不做重复工作。每 2 秒的轮询、每 5 秒的同步、每次按键都只做"有变化时才必须做的事"。
3. **易用**：每个失败都有一句用户能照着做的话，不再静默失败。
4. **结构**：把散在调用方的决策收进少数几个深模块，并在它们的接口上测试：前端用 Node 检查，后端用 unittest 和已有的 fake。

## 前提与协作

- 另一个会话正在改 `texhighlight.js`、`app.css`、`tests/browser/pages.mjs`，也碰过 `app.js`。前端阶段开始前，先把当前工作区的改动按来源分开提交，我的改动与那个会话的改动分开，作为基线。
- 每一项单独一个提交，提交信息说明行为变化。每个阶段结束时跑完整测试：unittest、`frontend_check.js`、`snippets_check.js`、浏览器测试 `tests/browser/run.py`。结果写进同目录的 `report.md`，格式同以前的审计。
- 沿用仓库已有的做法：决策逻辑放在纯模块里（`turn.js`、`tabs.js` 那样以 `module.exports` 结尾），`app.js` 只负责绘制。后端不引入第三方依赖。

## 统一规则

1. **一个页面只属于一个项目。** 页面第一次拿到项目标识后，每个请求都带上它；服务端不匹配就拒绝。页面收到拒绝后停止保存和轮询，并明确说明原因。
2. **会结束的子进程必须有真正的时限。** 时限到了，整棵进程树一起结束（Windows 用 Job Object）。不再直接调用 `subprocess.run(timeout=…)`。
3. **一轮 agent 的结局只有三种：ok、stopped、failed，由 agent 管理器判定一次。** 适配器只负责翻译各家 CLI 的输出；同一个错误只显示一次。
4. **网络或服务的暂时失败要重试，不能当成结束。** 只有明确的终止信号才算结束：`done`，或者 404 "unknown job"（这时要说明结果未知）。
5. **错误信息面向用户。** 超时、锁占用、文件被占用都翻译成一句"发生了什么、该怎么做"，统一在一处完成，不再把 Python 异常名和原始命令行直接显示给用户。

---

## 第 1 阶段：稳定性（A 批 + D1–D3）

### 1.1 D1 `proc.run`：真正有时限的短命令

- **问题**：Windows 上，`subprocess.run(timeout=…)` 杀掉直接子进程后，还会无限期等待仍占着管道的孙进程。这些孙进程包括 git-remote-https、ssh、凭据管理器，以及 npm `.cmd` 外壳后面的 node。后果是同步线程卡死且锁不释放，Home 的 `_fetch_lock` 永远占着，agent 的 preflight 和用量查询也会卡住。证据：`gitenv.py:54`，以及 `backend_claude.py:156,678`、`backend_codex.py:37,43,162`、`github.py:84`、`hub.py:1015`。
- **接口**：`proc.run(cmd, *, timeout, input=None, cwd=None, env=None, text=…, check=False) -> CompletedProcess`，超时抛出 `subprocess.TimeoutExpired`，与现有调用方的异常处理兼容。
- **实现**：用 `proc.start`（带 Job Object）启动；仿照 `build.Runner._wait` 循环调用 `communicate(timeout=0.5)`；到时限后 `kill_tree`，再给一个很短的宽限期，然后放弃剩余输出。
- **调用方**：`gitenv.git` 和上面列出的各处都改成调用它；`backend_claude._read_catalog` 里手写的 Timer 加 kill 也换成它。
- **测试**（`test_proc.py`）：一个孙进程一直占着 stdout 的 `.cmd` 外壳，`proc.run` 在 timeout 加宽限期之内返回；正常命令的返回码、输出、`input` 都和 `subprocess.run` 一致。

### 1.2 锁超时返回 409，而不是断开连接（A7）

- **问题**：`registry.FileLock` 超时时抛出 `TimeoutError`，`httpbase.py:167` 把它当成"页面已离开"处理，直接断开连接。于是重命名、开关同步时，页面只显示"server not reachable"。
- **修法**：新增 `LockBusy(Refused, TimeoutError)`，状态码 409，消息为"另一个 Prism 窗口正在写 X，请稍后重试"。`httpbase` 改为先判断是否已经开始应答以及是否为 socket 错误，再按 `Refused` 应答。原来按 `TimeoutError` 或 `OSError` 捕获的地方（比如 `synctarget.validate`）行为不变。
- **测试**：在一个持锁的线程里调用重命名路由，返回 409 和这句话。

### 1.3 D3 由管理器判定一轮的结局

- **问题**：每个适配器各自决定什么算错误。Stop 之后显示红色的"exit 1"加 stderr（`backends.py:328-330`）。Deep Code 和 Codex 的错误显示两遍。Claude 的错误结果文本被丢掉（`backend_claude.py:538-560`）。
- **接口**：`run(job)` 返回 `TurnResult`，字段为 session_id、exit、error、usage、cost、billing、denials、stderr_tail。管理器根据 `job.cancel` 和退出码判定 `outcome ∈ {ok, stopped, failed}`，只产生一条错误信息；`done` 事件带上 `outcome`。
- **前端**：`renderTurnCard` 按 outcome 绘制，stopped 用中性的"已停止"卡片。点 Stop 后立即显示"正在停止…"；如果停止失败，要明确说出来。
- **测试**：用 `test_agent.py` 里的 `cli()` fake，Stop 之后 outcome 为 stopped，并且没有错误卡片。对四个适配器各跑一次失败的回合（用已有 fake 和录制的输出），每次恰好产生一条错误。

### 1.4 agent 运行中不允许撤销上一轮（A5）

- **问题**：`agent.py:346` 的 Undo 写入文件时，不会告诉正在运行的这一轮。撤销的改动因此被算进当前这一轮的 diff，在 Deep Code 下还会被再次回滚。
- **修法**：`undo` 在 `busy()` 时返回 409，消息为"agent 正在工作，结束后再撤销"。前端在一轮运行期间禁用 Undo 按钮，并加提示。见文末决定点 1。
- **测试**：运行中调用 undo 返回 409，文件不变。

### 1.5 OpenAI 接口：Stop 不卡、工具异常不中止整轮（A6）

- **问题**：Stop 时 `resp.close()` 会等待读线程持有的锁，可能要 6.5 秒，之后读线程抛出未被捕获的 `AttributeError`，会话也丢了。`_call` 只捕获 ToolError、KeyError、TypeError，所以一个 `ValueError` 或 `UnicodeDecodeError` 就会中止整轮。
- **修法**：
  - 停止时对底层 socket 调用 `shutdown`，不再 `close` 响应对象。
  - 读取循环额外捕获 `AttributeError` 和 `http.client.HTTPException`。
  - 被取消时也保存会话；请求出错时不保存，失败的请求不留半截对话（2026-10-16 复核后改定，与 `test_http_error_is_reported` 一致）。
  - `_call` 把任何异常转成工具错误交回给模型。
  - 遇到"上下文过长"类错误，提示"对话太长，请新开一个对话"。
- **测试**：用 FakeAPI 的慢速流测试 Stop 在 1 秒内结束、下一条消息能接着原会话。工具收到非数字的 offset 或非 UTF-8 文件时，返回工具错误，这一轮继续。

### 1.6 D2 `watch.js`：轮询结果与标签页对账（A1、A3、外部删除）

- **问题**：
  - 页面不校验项目标识，端口被另一个项目复用时，会把那个项目的文件载入，再自动保存回去（`common.js:14`、`app.js:584-589`）。
  - 首次轮询失败时，会话被保存成空的（`app.js:568-574`、`2250`）。
  - 外部删除的文件被当作"没变化"（`app.js:586`）。
  - 有冲突时，每次轮询都重新加载符号表（`app.js:584-591`）。
- **接口**：`Watch.reconcile(prev, tree, tabs) -> {foreign, treeChanged, reload[], conflicts[], gone[], symbols, pdf}`，纯函数。`pollOnce` 退化为按结果绘制。
- **服务端**：`/api/tree` 的应答带上 `project_key`。`common.js` 在拿到标识之后，所有请求都带上 `X-Prism-Project` 头；`server.py` 不匹配时返回 409 并标明 other-project。页面收到后停止轮询和保存，并显示一个阻挡式横幅："这个端口现在属于另一个项目，请从 Home 重新打开"。
- **会话恢复**：拿到第一次 200 之前不恢复也不保存会话；之后再恢复标签页。
- **自动保存**（`tabs.js`）：失败时按退避重试，标签页显示错误状态，直到保存成功。
- **外部删除**：显示横幅"文件已在磁盘上删除：另存为新文件或关闭"。
- **测试**：Node 里用录制的 `/api/tree` 应答和伪造的标签页测 `reconcile` 和保存重试；`test_server.py` 测试标识不匹配时返回 409。

### 1.7 agent 事件轮询：暂时失败要重试（A2）

- **问题**：`app.js:1817` 遇到任何非 200 就跳出循环，页面以为这一轮结束了：Stop 按钮消失，暂缓的编辑被保存进 agent 的 diff。服务端重启后也没有任何提示。
- **修法**：决策放进 `turn.js`：`Turn.pollFailed(status, attempts) -> "retry" | "lost"`。5xx 和网络错误一律退避重试。404 "unknown job" 判为 lost，并显示卡片"服务器在这一轮中途重启，结果未知，请检查文件；这一轮无法撤销"。
- **测试**：Node 里测 `pollFailed` 的各种情况；浏览器测试里让事件请求（`Q.events`）中途返回一次 502，确认这一轮继续，暂缓的编辑没有被保存进去。（`slow_api` 是 agent 调用的模型接口，不在事件轮询的路径上：它返回 502 只会让这一轮失败。）

### 1.8 服务端的其他稳定性问题（A8）

- **编译时的文件错误**：`Build.run`（`build.py:609`）捕获 `OSError`，转成一条诊断（"文件 X 被其他程序占用（OneDrive、杀毒软件）"），不再返回 500；`bbl.unlink` 也加上保护。
- **状态文件**：`registry.write_json` 在替换前先 fsync 临时文件，并保留一份 `.bak`；读取时如果主文件损坏，说明是哪个文件，Home 在列表上方提供“恢复上一版”按钮：把 `.bak` 放回，损坏的文件保留为 `.damaged`（2026-10-16 复核后改定：不自动恢复，以免悄悄换回旧数据）。
- **共享仓库切回独立仓库**（`hub.py:622-645`）：先为每个成员建好仓库、取回历史，全部成功后才改名共享的 `.git` 并改标记。中途失败时，原状完全保留，并说明哪个成员失败。
- **`move_folder`**（`registry.py:250-285`）：持锁只做校验，移动本身在锁外进行（路径已由 `instances.exclusive` 预留），再拿锁写回指向。
- **Home 的 GitHub 检查结果不显示**（`hub.py:240`）：在计算目标之前就把 `FETCH["running"]` 置为 True。
- **测试**：每一项各加一个 unittest。共享仓库这项的测试，是让第 k 个成员失败，再检查原状完整。

### 1.9 前端的其他稳定性问题

- 同步开关的状态和请求之间有竞争（`app.js:621,737`）：记住用户想要的值，请求进行中时不被轮询覆盖。
- 切换 provider 后，迟到的旧应答会套到新 provider 上（`loadAccount`、`loadCatalog`、`checkUsage`）：发请求时记下 provider，应答回来时不一致就丢弃。
- `ATT.pollSync` 永不停止（`app.js:1596`）：设上限，超过后标记为"未知"。
- 发送消息期间新加的附件丢失（`app.js:1696`）：只移除已经发出去的附件。
- `S.building` 在 PDF 加载期间一直为真（`app.js:877-901`）：编译结束就立即复位，再去加载 PDF。
- localStorage 被聊天记录塞满后，所有设置静默停止保存：按最近使用淘汰其他项目的记录；写入失败时提示用户。

---

## 第 2 阶段：性能（D4 + B 批）

### 2.1 D4 `repoview.facts`：仓库状态只算一次，并带缓存

- **问题**：`locate` 在每个请求里要跑 2 到 3 次；`git status` 有三套不同的解析（`server.py:77`、`hub.py:167`、`gitsync.py:298`）；Home 启动时每个项目要跑 30 到 40 个 git 进程；Home 上的 "N changed" 把不会被提交的文件也算了进去。
- **接口**：`facts(root, *, fresh=False) -> RepoFacts(kind, top, prefix, branch, ahead, behind, changes, target, blocked_reason, github)`，加上 `invalidate(root)`。缓存键是 git_dir、配置键，以及 HEAD、index、远端 refs 的 mtime。
- **实现**：`locate`、用 `sources.recordable` 过滤的状态解析、目标解析都放到它后面。`hub.git_info`、`fetch_targets`、`publish_info`、`github_of`、`server.git_status`、`project_diff`、`GitSync._remote_state` 都只调用它。
- **测试**：在临时仓库上用计数器替换 `gitenv.git`。没有变化时第二次调用 0 个进程；提交后状态正确更新；被忽略的产物不计入 changes。

### 2.2 `/api/tree` 没有变化时几乎零成本（B1）

- **修法**：
  - `Project` 按文件夹的 mtime 缓存目录遍历结果。
  - `git status` 只在 index 或 HEAD 变化后、或刚保存过文件时才重跑（通过 `facts`）。
  - 应答带一个签名，页面下次带上；签名没变时服务端只回 `{same: true}`，以及打开的标签页对应文件的 mtime。
- **测试**：`test_server.py` 中，连续两次请求时第二次不遍历、不启动 git；改动一个文件后签名随之变化。

### 2.3 同步线程闲着就不起进程（B2）

- **修法**：只有出现新提交或 ref 变化时才尝试 push。解析目标改为一次 `git config -z --list` 加一次 `rev-parse`，不再每次跑 12 个进程。
- **测试**：用 git 计数器测，空闲 1 分钟启动的 git 进程为 0；本地有新提交时照常推送。

### 2.4 Home 的扫描和抓取

- **修法**：
  - 每个项目的摘要（文件数、最近编辑时间、主文件、标题、PDF 的 mtime）按 mtime 缓存。
  - 标题只读 main.tex 的开头部分。
  - `fetch_targets` 并行执行。
  - 文件数到达上限时显示"≥ N"，不再静默截断。
- **测试**：Home 的文件计数与编辑器的文件列表在同一棵测试目录上一致；没有变化时第二次刷新不再遍历。

### 2.5 agent 每轮的快照与 diff（B4）

- **修法**：
  - 回合结束时，lstat 没有变化（size、mtime_ns、inode、mode 都一样）的文件，直接复用开始时的状态，不再重读、不再算哈希。
  - 快照预算优先用于可编辑的源文件，不再按遍历顺序扣减。
  - 超过约 1 MB 或 2 万行的文件，只报告"大文件已改动（字节数从 a 变成 b）"；每个文件的 diff 限制在 200 KB 以内。
  - stderr 只保留末尾一段，用环形缓冲。
  - 撤销历史设一个总字节上限。
- **测试**：一个大数据目录排在 `sections/` 前面时，`sections/*.tex` 仍然可以撤销；一轮没改大文件时，回合结束不重读它。

### 2.6 编辑器前端（B3）

- 按键时只切换当前标签页的 dirty 类，并且只在状态真正变化时切换。
- `AH.apply` 和 `applyDiagnostics` 放进 `cm.operation`；`AH.clear` 只重画标记有变化的那些文件。
- agent 回复的流式渲染按 `requestAnimationFrame` 节流；MathText 按 (tex, 宏) 缓存 KaTeX 结果；宏没有变化时不重绘历史回复。
- 实时文件框改为追加文本节点。
- 拖动分隔条按 `requestAnimationFrame` 节流。
- PDF 搜索结果只画新到的那一页；页码标签用二分查找。
- 同步菜单内容没有变化时不重建（这也修了点击丢失的问题）。
- 事件长轮询拿到第一个事件后再等约 40 ms 合并后续事件。
- 测试：MathText 缓存命中的 Node 检查；浏览器测试里 1000 次按键内标签栏只重绘一次（通过计数器检查）。

---

## 第 3 阶段：易用性（C 批）

| 项 | 修法 |
|---|---|
| Stop 看起来像出错 | 1.3 完成后自然解决，再加上"正在停止…"的状态 |
| agent 改文件时抢编辑器和键盘焦点 | 一轮开始后，如果你打开过别的文件或焦点不在编辑器，就不切换、不聚焦，只在卡片里给出"跳过去"的链接 |
| 保存被暂缓时操作静默失效 | 撤销、同步提交、拉取、解决冲突统一显示 `Tabs.unsavedWhy` 给出的原因 |
| agent 运行中关闭标签页时问错问题 | 对暂缓的保存说明"这一轮结束后会自动保存"，或者保留内容、等结束后再保存 |
| 通知 4 秒后消失 | 小型通知栈：普通提示照样 4 秒消失，错误一直保留到手动关掉 |
| 命令按钮覆盖输入框里的草稿 | 本地命令直接执行，不再经过输入框 |
| 原始超时文本 | 统一翻译成"GitHub 在 N 秒内没有响应，请检查网络，稍后会自动重试" |
| Home 的 N changed 清不掉 | 2.1 完成后自然解决 |
| 自更新失败只写进日志 | 在 Home 设置里显示最近一次自更新的结果 |
| Windows 选择文件夹时弹出控制台窗口 | 启动时加 `NO_WINDOW` |
| 长时间工具调用期间状态不动 | 状态行显示"距上一个事件 N 秒" |
| Codex 的推理过程被丢掉 | 映射成思考事件 |
| Deep Code 一轮结束前只显示 thinking | 运行期间追踪会话文件，实时显示工具调用 |
| 一次大改动清空已保存的聊天记录 | 保存时跳过过大的元素，不再到此为止；diff 折叠并限制长度 |
| 编译错误的行高亮在从磁盘重载后消失 | 重载后重新应用诊断 |
| 移除正在上传的附件不会取消上传 | 取消上传 |

每一项都要配一个浏览器测试或 Node 检查。

---

## 第 4 阶段：其余结构整理（D5）

1. **`httpbase.App`**：编辑器服务和 Home 服务共用启动、重启、交接、心跳、"已更新"标记，以及错误映射。两个服务各自只保留自己的路由。用进程内的伪造 instances 测试两者行为一致。
2. **统一工具词汇**：`job.tool(id, kind, path, lines, detail, native)`，路径统一为相对路径。这样编辑器标记和越权警告对四个后端都有效，前端不再写死 Claude 的工具名。用契约测试对四个适配器跑录制的原生输出。
3. **提示词包装统一构造**：适配器只声明 `file_tool_rule` 和指令的传递方式，由管理器拼接文本。
4. **请求只校验一次**：Deep Code、OpenAI 里重复的 preflight 和会话检查合并；Deep Code 的配置文件检查挪进 preflight，出错时说出文件名。
5. **`sharedrepo.py`**：把约 330 行共享仓库的 git 操作从 `hub.py` 移出来，按"计划 → 执行"组织，并记录步骤日志，失败后可以续做。
6. **`GitSync` 返回结果**：`pull()` 和 `resolve_mine()` 返回结果对象，不再依赖共享的 `self.error`；`status()` 只读线程维护好的快照，GET 请求里不再写入绑定。
7. **前端的小模块**：
   - `ChatLogic.compose()` 负责拼装消息，scope 作为权限边界也在其中计算。
   - TeX 补全拆成 `texcomplete.js`。
   - 删掉 `saveTab`、`slashPrompt` 这类纯转发，以及 `Q.*` 上不可能触发的 `.catch`。
   - 编译结果和回合卡片的描述函数放进纯模块。

---

## 已定（2026-10-16）

1. **agent 运行中禁止撤销上一轮**：服务端返回 409，前端禁用按钮并说明原因。
2. **agent 改文件时，只在你没碰别处时跟随**：你在聊天框打字或打开了别的文件，就不切换、不聚焦，只在卡片里给出"跳过去"的链接。
3. **直接提交到 main，每项一个提交**：只暂存这一项的改动；工作区里其他未提交的改动不混进来。
4. **先做后端，前端等另一个会话提交之后再做**：第 1 阶段的 1.1–1.5、1.8 和第 2 阶段的服务端项先做；1.6、1.7、1.9、2.6 和第 3 阶段的前端项等协调好之后再做。

## 顺序与依赖

```
第 1 阶段：1.1 proc.run ─┬─> 1.8 的 git 相关项
                        └─> 2.3 同步线程
           1.2 → 1.3 → 1.4 → 1.5          （后端，与前端无冲突，可以先做）
           1.6 watch.js → 1.7 → 1.9       （前端，等协调好再做）
第 2 阶段：2.1 facts ─┬─> 2.2 /api/tree
                     ├─> 2.3
                     └─> 2.4 Home
           2.5、2.6 相互独立
第 3 阶段：依赖 1.3、1.6、2.1
第 4 阶段：最后做；1、5、6 各自独立
```

## 前端执行状态（2026-10-16）

原有界面与设置改动已先拆成两个基线提交。1.6、1.7、1.9、2.6、第 3 阶段剩余前端项（含 Deep Code 实时工具事件配套）以及 4.7 已逐项修复并提交；阶段测试、浏览器复核和其余后端范围见 [report.md](report.md#前端补记2026-10-16)。复核后的修复与剩余待办见 [report.md](report.md#待办截至-c154a6d)。
