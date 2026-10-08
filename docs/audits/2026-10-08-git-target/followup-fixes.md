# 四项审计遗漏的修复记录

日期：2026-10-08。本轮修复依据为 [fix-audit.md](fix-audit.md)。
已提交基线仍为 `9c2f0ef28b2d0827272e746b58c157be7c94441c`。
修复位于本地未提交工作区，没有推送产品改动。

## 修复范围

| 审计项 | 实现 | 回归证据 |
| --- | --- | --- |
| P1 新建仓库的绑定窗口 | 使用已有 `bind_target(expected)`，传入刚校验的公开目标。配置变化时拒绝绑定和推送，保留文件。 | `test_create_rejects_target_changed_before_binding`：模拟外部 gh 创建，实际运行 Git 解析；在绑定前将 origin 从 paper 改为 tool。断言报错、无绑定、无 push、无分支保护调用、文件保留。 |
| P1 DeepCode 会话目录碰撞 | 保持原生目录编码。核对索引 `originalPath`，并复用 `sessionmeta.matches` 核对该会话原生 system 消息中的 `root path` 和 `pwd`。最新会话发现也执行归属校验。 | `test_deepcode_collision_checks_original_session_project`：真实短路径碰撞；索引改写后仍拒绝外国会话。已有归属测试补充缺少索引或会话原始目录的拒绝情况。 |
| P1 冲突读取的 Git 环境污染 | `_blob()` 使用现有 `git_env()`；保持二进制读取。 | `test_conflict_merge_ignores_foreign_git_environment`：实际 pull 产生正文与图片冲突；外国仓库有匹配的冲突 stage。注入 GIT_DIR、GIT_WORK_TREE、GIT_INDEX_FILE、GIT_OBJECT_DIRECTORY 和动态 Git 配置后，两侧真实内容仍被保留。 |
| P2 目录重命名使绑定失效 | 使用已有 `public_target` 比较稳定目标，并单独核对本地分支。绝对 `git_dir` 作为位置记录，不参与远端身份比较；实际 top/git_dir 检查保留。 | `test_rename_and_move_keep_bound_remote_and_branch`、`test_moved_worktree_keeps_binding`：重命名、跨目录移动、worktree move、所属仓库移动与 repair。正常目标可继续同步；remote 变更仍被拒绝。 |

DeepCode 原生索引的 `originalPath` 会在每次保存时被当前项目覆盖。
只检查这个字段仍会放行旧的外国会话。
因此同时检查原生会话文件中的原始工作目录。
缺少所需元数据的历史会话须新建聊天；不从用户消息猜测归属。

## 先失败，再修复

使用 [TDD 技能](/Volumes/Codex-Workspace/live/home/zhdeng/.codex/skills/tdd/SKILL.md)，逐项运行一个回归，再做最小实现。

| 回归 | 修复前观测 | 修复后观测 |
| --- | --- | --- |
| 新建绑定窗口 | 无 error，错误目标被接受 | 返回 changed since confirmation；不调用 push |
| 会话目录碰撞 | 外国会话 check_session 返回 None | 拒绝外国会话；latest_session 不返回它 |
| 完整冲突合并 | 正文被替换为 FOREIGN | 正文保留本地和 coauthor 两侧；图片保留本地及 coauthor 副本 |
| 重命名及移动 | 仅 git_dir 改变就返回 Sync target changed | 保持绑定，临时远端收到预期分支内容 |

四项修复前失败与修复后通过的终态已在本轮工具输出记录。
没有回退其他已有修复。

## 验收与边界

| 检查 | 结果 | 原始输出 |
| --- | --- | --- |
| 目标、环境、会话、界面回归 | 22 项，0 失败、0 错误 | [输出](followup-target-tests.txt) |
| 全量回归 | 251 项，0 失败、0 错误、14 跳过；275.937 秒 | [输出](followup-full-tests.txt) |
| 增补的 worktree 所属仓库移动 | 1 项通过 | [输出](followup-worktree-tests.txt) |
| DeepCode 0.4.3 原生 CLI | 4 回合通过，包含本地恢复、Ask 写入拒绝、范围恢复、编译 MCP 和声明 Git 副作用的拒绝 | [输出](followup-deepcode-native.txt)，[脚本](deepcode_acceptance.py) |
| DeepCode 原生目录碰撞 | 两个项目真实共用目录；A 本地恢复成功。B 新会话将索引 originalPath 改写为 B，仍含 A 的 id。B 恢复 A 被拒绝且没有模型请求。 | [输出](followup-deepcode-collision.txt)，[脚本](followup-deepcode-collision.py) |
| Python 和 diff 检查 | py_compile、git diff --check 通过 | 本轮命令终态为 0 |
| Windows/Linux 实机 | 按用户要求跳过 | 未验收 |

全量回归使用 `PRISM_SKIP_TEX=1 python3 -m unittest discover -s tests`。
源码与审计基线的比较结果见 [followup-manifest.json](followup-manifest.json)。
该清单核对全部 28 个审计源文件，6 个变更、22 个保持不变。
已提交基线保持不变。

Git 回归只使用本机临时仓库和 bare 远端。
gh 创建及推送边界使用夹具，没有访问真实 GitHub 论文仓库。
DeepCode 使用真实 CLI，模型和编译服务使用本地模拟服务。
本轮没有真实 DeepSeek 模型或真实 TeX 编译验收。
这份记录是本地修复与回归证据，不替代独立审计或完整权限保证。

相比 [审计源码清单](fix-audit-manifest.json)，本轮仅更改：
`backend_deepcode.py`、`sessionmeta.py`、`gitsync.py`、`hub.py`、
`tests/fake_deepcode.py`、`tests/test_git_target.py`。
原有审计、探针、测试输出和方案保持不变。
各验收命令的终态和输出哈希见 [followup-results.json](followup-results.json)。
