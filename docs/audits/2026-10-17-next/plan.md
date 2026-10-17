# 下一阶段工作方案

日期：2026-10-17。来源：[2026-10-16 代码库整理](../2026-10-16-codebase/report.md)已全部完成；同日对此前全部审计的遗留项做了只读核对（三路并行，关键结论已在代码中复核）。本方案只收录核对后仍成立的项，以及只能在真实环境确认的项。执行结果写入同目录的 `report.md`，格式同以前的审计。

## 目标

1. **先把会推错、会说错的地方补上**：同步目标、文档、Deep Code 的只读保证。
2. **补测试欠账**，让回归在受限环境里也可信。
3. **在真实环境里验收**一次：真实 GitHub、真实论文、真实 CLI。自动回归始终跳过真实 TeX 编译（`PRISM_SKIP_TEX=1`），模型调用也全是本地 fake。
4. 已知限制逐条决定：修，还是写明接受。

## 做法

沿用以往：每项一个提交，提交信息写行为变化；每个阶段结束跑完整回归（unittest、Node 检查、`tests/browser/run.py`），结果写进 `report.md`。只读核对的原始结论不另存，相关证据在各项中给出。

---

## 第 1 阶段：正确性（代码）

### 1.1 新建仓库在创建时就绑定到它自己（已定，2026-10-17）

- **问题**：`create_github_repo`（`publish.py:148-156`）发现有效推送目标与刚建的仓库不同，会拒绝并不写绑定。但 `422d38c` 以后，`RepoSettings.validate`（`synctarget.py:237-253`）会把未绑定项目当前的目标直接绑定。Home 看一眼（`hub.sync_facts` → `validate_target`）或编辑器的同步线程跑一次，就会绑定那个被拒的目标并开始自动同步。只读核对时用探针复现过：拒绝之后，下一次校验就绑定到了 `github.com/owner/tool`。
- **根因**：Prism 自己清楚刚建的是哪个仓库，却在建好之后再去读配置、猜目标；从建好到写下绑定之间，项目处于"未绑定"状态，自动绑定就会接受配置里的任何东西。
- **修法**（不需人工确认）：
  1. 建仓库的同一刻写下绑定，依据是 Prism 所建的仓库：`repository_identity = github.com/<owner>/<name>`，`branch_ref`、`local_branch` 取当前分支。这一步发生在 `gh repo create` 之前，并持有 `RepoSettings` 的锁。从此项目不再处于未绑定状态，自动绑定也就不会被触发。其余字段（remote、push_url、fetch_url）在第一次校验时补全：`validate` 对同一仓库、同一分支本来就会静默重绑。
  2. 远程和上游由 Prism 自己设：`origin` 指向新仓库，`branch.<b>.remote/merge` 指向它的分支。`create_github_repo` 本来就要求项目没有远程（`publish.py:127`），所以不会有旧设置干扰。
  3. 之后由已有规则自动处理：实际目标与绑定一致就同步；不一致就暂停推送，本地照常保存；配置恢复一致后自动继续，不需要任何操作。
  4. 唯一需要人看的情况：全局配置的 `insteadOf`/`pushInsteadOf` 把新仓库的地址改写成另一个仓库。暂停时给一句话：哪条规则改写到了哪里、怎么改。改好后自动恢复。
- **测试**（`test_git_target`）：
  - 建仓库过程中（mock 在 create 与 push 之间）并发调用 `validate_target` 和 Home 的 `sync_facts`，绑定始终是新仓库。
  - 全局 `insteadOf` 改写到另一个仓库：暂停，原因里点名那条规则，远端 B 收不到任何 ref；删掉规则后，下一次同步自动推到新仓库。
  - 正常情况：建好即同步，推送一次成功，与现在相同。

### 1.2 README 与自动绑定一致

- `README.md:200-204` 仍写着"首次远端同步前先 Confirm sync target"。改成现行行为：未绑定的项目自动绑定当前目标；目标换成另一个仓库或分支时暂停，等确认；新建仓库被拒的情况按 1.1 处理。

### 1.3 Deep Code 的版本检查

- **问题**：`unavailable()`（`backend_deepcode.py:244-249`）只看程序是否存在。Ask 回合的只读要靠 Deep Code 认得 overlay 里的权限类别名（`write-in-cwd`、`delete-in-cwd`、`mcp` 等，`:341-344`）。不认这些名字的版本会悄悄失效，只剩回合后的恢复兜底。
- **修法**：preflight 运行 `deepcode --version`（`proc.run`，带时限，结果按二进制的 mtime 缓存），低于已验证的版本（当前 0.4.3）时拒绝，并说明"请升级到 ≥ 0.4.3"。版本号读不出来时也拒绝，并给出原文。
- **测试**：`fake_deepcode.py` 加 `--version`，分别给出旧版本、可用版本和乱码各一例。

### 1.4 推送到校验过的地址（需决定，见决定点 2）

- **问题**：`push()` 在锁内用 `validate_target(fresh=True)` 做完校验，但推送时传的是 remote 名（`gitsync.py:452`），git 会重新读配置。校验之后、推送之前，外部改了 pushurl 或 URL 重写，就会推到别处。`2026-10-08-git-target/fixes.md:38` 当时已承认这一点。
- **修法**：改为 `git push -- <target.push_url> HEAD:<branch_ref>`。推送成功后用 `update-ref` 把跟踪引用移到 HEAD（按 URL 推送时 git 不会更新它）。fetch 同样改为按 `fetch_url` 拉取，到已有的 `+branch_ref:tracking_ref`。
- **风险**：凭据助手按 URL 查找，不受影响。但 `insteadOf`/`pushInsteadOf` 也会作用于直接写出的 URL。要确认 `target.push_url` 已是改写后的结果，不会被再改写一次；用 `synctarget` 现有的 11 种配置对照测试覆盖。
- **测试**：校验之后、推送之前（mock 在两者之间）改 pushurl，远端裸仓库 B 没有收到任何 ref，A 收到；跟踪引用、ahead/behind 与原实现一致。

### 1.5 嵌套项目里 agent 看不到外层仓库（低）

- **问题**：项目在别的仓库里（比如 prism-local 的 `examples/`）时，自动同步已经关了。但 agent 的 git 仍会找到外层仓库，读到它的 origin 和历史（`backends.py:109-125`；`GIT_CEILING_DIRECTORIES` 在 `gitenv.SELECTORS` 里被剥掉了）。
- **修法**：`agent_env` 接收项目的 Place；kind 为 nested 时，设 `GIT_CEILING_DIRECTORIES=<项目的父目录>`。这样项目内的 git 找不到外层仓库，项目自己的 `.git` 不受影响。
- **测试**：嵌套项目里，以 agent_env 运行 `git rev-parse --show-toplevel` 应失败；own 项目照常成功。

---

## 第 2 阶段：测试欠账

| 项 | 修法 |
|---|---|
| followups 的 O9（置顶失败的提示）、O10（"server not reachable" 恢复后消失）只做过语法检查 | 浏览器测试各加一项：Home 置顶时 mock 一次 409；编辑器 tree 先失败一次再恢复，检查状态栏 |
| 两个测试子进程不关句柄（`test_gitsync.py:195`、`test_server.py:236`） | 子进程代码改用 `with`，或在结束前显式 close；在设了 `PYTHONWARNINGS=always::ResourceWarning` 的情况下跑一次确认 |
| `test_backend_contracts` 只能从 `tests/` 运行（直接 `from test_agent import`） | 文件开头把 `tests/` 加入 `sys.path`，与其他测试文件一致 |
| `test_proc` 靠 `tasklist` 判断进程是否存活，在受限沙箱里误报 | 改用 `OpenProcess` 加 `GetExitCodeProcess`（ctypes）判断，不依赖外部命令 |

---

## 第 3 阶段：真实环境验收（需要你来操作）

全部用一次性的临时仓库或副本，不碰正式论文的 GitHub 仓库。我准备好脚本和检查清单，每一步写明"做什么、应看到什么"；你执行，我记录到 `report.md`。

1. **真实论文**（"Average over Borel-subgroups" 的副本）：打开、编辑、编译（真实 TeX）、Push/Pull、从 Home 重启、改名，再打开 Home 看卡片上的 N changed 和 ahead/behind。
2. **真实 GitHub**（临时私有仓库）：从 Home 新建仓库、绑定、首次推送、分支保护；再按 1.1、1.4 的场景改一次 pushurl，确认远端没有收到。
3. **Codex**：真实账号下，一轮 Edit 加一轮 Ask；顺带测每轮 `codex login status` 的耗时（`backend_codex.py:162`）。如果明显，改为缓存。
4. **Deep Code**（如果你用）：Windows 上新会话、恢复会话、Ask 拒绝写入；配 1.3 的版本检查。
5. **视觉输入**：OpenAI 兼容接口和 Codex 各附一张图，看模型的回答能否对上图的内容。

做不到、也不打算做的，写进 `report.md` 作为未覆盖：Linux 和 macOS 原生运行、MXC、跨盘移动的真实失败、真实 Ollama/DeepSeek。

---

## 第 4 阶段：维护

- 推送：本地 main 比 `origin/main` 多 102 个提交（见决定点 4）。
- `.claude/` 加入 `.gitignore`（目前里面只有 agent 的 worktree）；旧 worktree 由你删除。

## 有意保留（不在本阶段做，除非你要）

- Deep Code 的 deny 是权限类别，不是 OS 沙箱。
- 回合中新建的嵌套元数据只在事后提示，不会预先拦截。
- OpenAI 接口的 PDF 附件；Codex 和 Deep Code 的 Skills 目录。
- 2.2 里没做的目录遍历缓存和 `{same: true}` 增量应答，以及从 `DirEntry` 取 mtime。

---

## 决定点

1. **1.1 被拒的新仓库**：已定（2026-10-17）。要求更自动、更稳：建仓库时就绑定到所建的仓库，不一致时暂停，配置恢复一致后自动继续，不设人工确认。
2. **1.4 按 URL 推送**：A（建议）做，消除校验之后到推送之前这段窗口；B 保持现状，写明这是已知限制。
3. **第 3 阶段**：你方便时一起做，大约一小时；也可以只做第 1、2 项。
4. **推送到 origin**：第 1、2 阶段跑完回归之后推一次；还是现在先推，以后每个阶段推一次。

## 顺序

```
第 1 阶段：1.2（无依赖）→ 1.1（决定点 1）→ 1.3 → 1.4（决定点 2）→ 1.5
第 2 阶段：四项相互独立，可与第 1 阶段穿插
           ── 回归，写 report.md，按决定点 4 推送 ──
第 3 阶段：在第 1 阶段之后（1.1、1.3、1.4 要在真实环境里再验一次）
第 4 阶段：随时
```
