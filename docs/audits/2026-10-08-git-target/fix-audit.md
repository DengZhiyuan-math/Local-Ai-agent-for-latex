# 修复审计结果

日期：2026-10-08。结论：**常见误推路径已修复，但尚未完成方案验收。** 本次发现4项可复现遗漏，其中3项涉及隔离/目标正确性，1项影响目录重命名后的可用性。

## 审计对象及方法

已提交基线为`9c2f0ef28b2d0827272e746b58c157be7c94441c`，修复位于未提交工作区。使用`git diff HEAD`审阅已跟踪文件，并单独读取新增的gitenv.py、sessionmeta.py和测试。方案依据为[plan.md](plan.md)。

审计前记录28个产品/测试文件的SHA256，审计结束检查未发生变化，见[fix-audit-manifest.json](fix-audit-manifest.json)。因此本结论绑定具体工作区内容，而不是声称当前提交本身已经包含这些修复。

Code-review分别审查规范和方案符合性。主审独立复现遗漏，并运行相关现有测试。没有修改产品代码、推送代码或访问真实论文远端。本次只新增本地审计材料。

## Standards

**1项需修正，与下面Spec中的冲突读取问题重复，不另计独立缺陷。**

`gitsync.py:695–696`的`_blob()`仍使用`env={**os.environ, ...}`。同模块其他Git调用已使用`git_env()`。`_keep_both()`在715–716行通过它读取冲突stage。夹具注入GIT_DIR后，普通Git方法仍识别论文仓库，`_blob(':2:main.tex')`却读取外国仓库的冲突内容。

用户AGENTS要求优先复用已有实现；方案第二批第97行要求删除仓库选择变量。这里应直接复用git_env，不另写过滤逻辑。

其余改动基本使用标准库并复用既有GitSync、项目标识和MCP。未发现需要报告的严重抽象扩张或代码异味。不建议为本修复引入新框架。

## Spec

### 1. P1：DeepCode仍接受其他项目的会话

位置：`backend_deepcode.py:77–80,178–190`。

短路径沿用分隔符替换为连字符的项目编码。两个不同路径`a-b/c`与`a/b-c`产生同一会话目录。check_session只查索引id和文件是否在该目录，没有核对原始项目cwd。

实测：same_sessions_dir=true；foreign_session_accepted=true。这证明适配器的恢复前检查会放行错误项目的会话，未运行真实DeepCode模型来演示后续回答。

违反方案第117行：“未知或其他项目的session拒绝恢复”。

最小修正：保持原生CLI目录兼容，同时从原始项目元数据校验归属；无法验证的历史会话拒绝自动恢复。若CLI没有此元数据，使用编辑器已绑定的项目/session记录作检查。不能只在适配器私自改项目目录哈希算法，否则可能找不到CLI实际写入的记录。增加短路径碰撞和缺少归属元数据的回归。

### 2. P1：冲突合并读取仍受继承的Git环境影响

位置：`gitsync.py:694–699,715–716`。

通过GIT_DIR/GIT_WORK_TREE指定临时外国仓库。普通GitSync.git过滤变量并正确识别论文；_blob读取的HEAD文件及`:2:main.tex`均来自外国仓库。该方法被真实冲突恢复路径调用，可能返回错误版本或因外国索引没有对应stage而失败。

违反方案第97行的环境过滤要求。

最小修正：_blob使用env=git_env()。加入冲突stage夹具，并检查继承GIT_INDEX_FILE及动态Git配置变量的读取路径。

### 3. P2：正常目录重命名会使同步绑定失效

位置：`gitsync.py:414–417,437–438`。

绑定记录包含绝对git_dir，并通过整个candidate与bound比较决定是否允许同步。仅重命名论文目录后，remote、分支、URL和配置指纹都不变，git_dir路径改变，结果accepted=false。程序要求重新确认目标，尽管目标仓库没有改变。文件未丢失。

违反方案第71行：“移动/重命名目录不应仅因路径字符串变化而丢失绑定”。

最小修正：将稳定的仓库/分支绑定字段与本地位置字段分开比较。仍保留实际top/git_dir检查，但不要仅凭绝对路径变化判定远端目标变更。补充重命名、移动和worktree回归。

### 4. P1：新建仓库校验后的绑定窗口可接受另一个目标

位置：`hub.py:951–957`。

create_github_repo先解析并检查目标是否是刚创建的论文仓库，随后调用无expected的bind_target()。后者重新解析配置并保存当前候选。如果外部配置在两步之间改变，已检查的论文目标被新的工具目标替换；后续validate_target看到的是新绑定，会放行它。

确定性夹具在这个边界将origin由owner/paper改为owner/tool，实际执行目标解析和绑定，模拟gh创建与push调用。结果：reported_created_url=owner/paper；bound_push_identity=owner/tool；would_pass_target_validation=true；reported_error=null。没有实际网络调用或推送。

违反方案第67行“使用刚创建的仓库和分支进行绑定”，以及推送前配置变化时停止的要求。

最小修正：使用已有expected机制，调用`sync.bind_target(gitsync.public_target(candidate))`。发生变化应拒绝绑定并保留本地文件。增加创建后、绑定前的配置变化回归。

## 已通过的检查

- origin/pushurl不一致、pushRemote/pushDefault冲突、多个push URL、mirror和URL重写会阻止常见自动误推。
- push显式指定remote和完整refspec，并关闭隐式tag及子模块推送。
- 未绑定、目标变化、detached HEAD、本地上游等状态有阻止反馈。
- SSH/HTTPS形式的同一GitHub仓库可以识别；目标URL认证信息得到脱敏。
- 正常共享仓库、worktree和非origin上游通过相关测试。
- 保存、附件、关闭flush的目标变更测试通过。
- 前端会话按project_key/provider隔离，错误或缺少project_key的AI请求被拒绝。
- 主要Git调用已集中使用环境过滤；DeepCode设置中的仓库变量会在preflight被阻止。

这些通过项说明修复方向正确，不能覆盖本次新增夹具找到的边界。

## 测试结果

| 命令范围 | 实际终态 |
| --- | --- |
| test_git_target、test_gitsync、test_collab、test_sync_buttons、test_backend_contracts | 80项，249.305秒，OK |
| test_server、test_hub、test_lifecycle、test_agent | 110项，47.817秒，OK，2项跳过 |
| 本次新增诊断探针 | 四项遗漏均复现；源码运行前后哈希一致 |

命令使用`PRISM_SKIP_TEX=1 PYTHONPATH=tests:prism_local python3 -m unittest ...`。合计190项，含2项跳过。未执行完整discover；不得称为全仓库测试全部通过。未设置PRISM_NATIVE_CODEX，原生sandbox opt-in部分未执行。本次没有真实TeX、真实模型、Windows/Linux或另一台电脑的验收。

现有测试通过，但缺少上述负面场景。不能以“测试全绿”宣布隔离修复完成。测试终态摘要见[fix-audit-tests.txt](fix-audit-tests.txt)。

## 复现与建议处理顺序

```sh
python3 docs/audits/2026-10-08-git-target/fix-audit-probe.py
```

该诊断使用macOS的/private/tmp，Git、Python标准库和临时夹具。输出记录当前行为，不是期望所有字段为true的验收门禁。结果见[fix-audit-results.json](fix-audit-results.json)。

先修复新建仓库绑定窗口、DeepCode归属校验及_blob环境遗漏，再处理目录重命名的兼容。每项增加能够在当前代码失败的回归，然后重跑对应现有测试。最后完成真实DeepCode及Windows/Linux验收。

规范轴：1项，最严重的是Git环境过滤遗漏。方案轴：4项，最严重的是错误目标绑定、跨项目会话恢复和外国冲突内容读取。两轴有1项重复，独立问题共4项。
