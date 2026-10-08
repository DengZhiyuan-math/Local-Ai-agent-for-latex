# Git 目标与项目会话修复记录

日期：2026-10-08。基线：`9c2f0ef28b2d0827272e746b58c157be7c94441c`。
本次为本地未提交改动。保留此前 AI 后端适配改动。没有提交、推送或发布产品改动。

## 已实现

| 批次 | 改动 |
| --- | --- |
| 统一同步目标 | 在现有 `prism-local.json` 保存 remote、完整分支 ref、实际 fetch/push URL、仓库身份及配置指纹。界面、编辑器和 Home 使用同一解析结果。已有项目首次远端同步需在 Git 菜单确认；Prism 新建仓库按已知仓库和分支绑定。 |
| 目标校验 | 拒绝冲突的 pushRemote/pushDefault、多个 URL、mirror、不同 fetch/push 仓库、detached HEAD 和本地 upstream。remote、分支或相关配置变化时保留本地编辑和提交，暂停远端同步。确认请求再次校验，拒绝过期的菜单目标。 |
| 显式同步 | push 明确指定 remote 和 `HEAD:refs/heads/...`，关闭 follow-tags 和子模块递归推送。fetch 明确指定同一 remote 与分支。没有裸 push 或默认远端 fetch。共享仓库和 worktree 继续使用现有 GitSync；worktree 的保护 hook 使用 Git 原生 hooks 路径。 |
| Git 环境 | 公共 `gitenv.git_env` 清理仓库选择变量和 Git 配置注入变量，保留 SSH、凭证、代理及平台变量。GitSync、编辑器、Home、启动器和 AI 使用它。DeepCode 用户/项目设置若再次注入这些变量，在回合开始前拒绝并报告配置来源与变量名。 |
| 会话归属 | info/ping 返回规范目录和项目标识。会话、聊天记录、选区记录和 context 按项目标识与 provider 保存。旧页面、错误项目请求和无法核实的会话被拒绝。DeepCode 检查当前项目索引与会话文件；Codex/Claude 检查原生 cwd 元数据；API 会话记录项目归属。 |
| 诊断 | AI 启动记录 job、provider、project_key、cwd 和 session。目标校验及实际 fetch/push 记录 Git top、脱敏目标、结果和失败阶段。日志不记录模型提示、论文正文或凭证值。 |

自动同步开关保留原有含义：关闭自动提交、附件同步及关闭时的 flush。用户明确点击 Save now 或 Get changes 仍可同步，但必须通过目标校验。附件被阻止时显示“已本地提交，同步暂停”，不会误报已推送。

## 验收

所有 Git 远端均为临时本机 bare 仓库。没有访问真实论文或工具远端。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 全量回归 | 246 项，0 失败、0 错误、14 跳过；255.650 秒 | [原始输出](fixes-tests.txt) |
| 相关 Git/附件/HTTP 回归 | 46 项通过 | [原始输出](targeted-tests.txt) |
| 最后界面逻辑与 Home 检查 | 35 项通过 | [原始输出](final-ui-hub-tests.txt) |
| Codex 原生权限回归 | 12 项通过；不调用模型 | [原始输出](native-contract-tests.txt) |
| 真实 Git dry-run | 正常绑定选择论文目标；pushRemote/pushurl 冲突未发起推送；继承的 GIT_DIR 被清理；所有远端 refs 不变 | [结果](fixed-probe-results.json)、[后继探针](fixed_probe.py) |
| DeepCode 0.4.3 原生 CLI | 4 回合通过。新会话、恢复会话、Ask、范围恢复及编译工具行为保持正常；声明 mutate-git-log/network 的 commit/push 调用均被权限拒绝，HEAD 和远端 refs 不变；跨项目恢复被拒绝 | [原始输出](deepcode-acceptance-results.txt)、[验收脚本](deepcode_acceptance.py) |
| Python、JavaScript、diff 检查 | py_compile、node --check、git diff --check 通过 | 本机命令终态为 0 |
| Windows/Linux 实机 | 按用户要求跳过，未验收 | 本机为 macOS；未启动其他项目的虚拟机 |

全量测试设置 `PRISM_SKIP_TEX=1`。DeepCode 使用真实 CLI，但模型和编译服务为本地模拟服务。没有验证真实 DeepSeek 模型或真实 TeX 编译。界面测试执行实际 JavaScript 函数与 DOM 夹具，没有进行浏览器视觉验收。

DeepCode 的类别权限不等于操作系统沙箱。上述拒绝结果只证明这些标记正确的工具调用被阻止，不能证明任意 shell 命令不可绕过。Codex 原生路径权限是独立验证的能力。

每次网络操作前重新校验目标，并复用应用锁。其他进程仍可在校验之后修改 Git 配置；应用锁不能保证与外部配置修改原子化。没有据此宣称完整权限保证。

原始 [诊断报告](report.md)、[探针](probe.py) 和 [方案](plan.md) 保持不变。本次修复已复现的程序缺口，不认定原截图事件的根因。当前源码校验值见 [source-hashes.json](source-hashes.json)。
