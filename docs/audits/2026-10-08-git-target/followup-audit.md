# 四项修复的独立复核

日期：2026-10-08。提交基线：`9c2f0ef28b2d0827272e746b58c157be7c94441c`。
审计对象为当前未提交工作区，与 `followup-manifest.json` 的全部 28 个源文件哈希一致。
本轮仅新增审计记录，没有修改产品代码、提交或推送。

原四项问题已闭合。发现一个新引入的 P2 会话恢复回归。

| 审计轴 | 新问题数 | 最高优先级 |
| --- | ---: | --- |
| Standards：复用、简单性及实现约定 | 0 | 无 |
| Spec：目标绑定、环境隔离及会话归属行为 | 1 | P2：有效会话恢复被拒绝 |

## 原四项闭合情况

| 原问题 | 复核结论 |
| --- | --- |
| DeepCode 碰撞目录放行外国会话 | 原始 system 元数据核对阻止外国会话；发现以下恢复回归。 |
| 冲突读取继承外国 Git 环境 | `_blob()` 使用已有 `git_env()`。真实本地冲突回归覆盖正文及二进制内容。 |
| 重命名、移动及 worktree 修复丢失绑定 | 使用公开目标及本地分支比较；不再将绝对 `git_dir` 当作远端身份。目标变更仍被拒绝。 |
| 新建仓库绑定窗口接受改变后的目标 | `bind_target()` 接收已校验的公开目标。边界内配置改变时停止绑定和推送。 |

## [P2] 碰撞项目交替运行后，原项目无法恢复自己的会话

位置：`prism_local/backend_deepcode.py:185–188`。

`a-b/c` 与 `a/b-c` 共用 DeepCode 会话目录。A 创建会话后可以恢复。
B 保存新会话会将共享索引的 `originalPath` 改为 B，但保留 A 的会话和索引项。
此时 A 返回并恢复自己的会话，会被共享索引的目录检查提前拒绝。
A 会话的原始 system 元数据仍明确属于 A，且 `sessionmeta.matches()` 返回 True。
这会中断有效聊天的继续使用。外国会话仍被正确拒绝。

现有单元回归及原生碰撞验收只检查 A 在 B 保存前的恢复，以及 B 拒绝 A 的会话。
它们没有检查 B 保存后返回 A 的恢复。

最小修复：保留索引中的会话存在性检查，以会话原始 system 元数据判定项目归属。
移除共享索引 `originalPath` 必须等于当前项目的条件。
保留会话 ID、文件路径、符号链接和缺少元数据时的拒绝检查。
增加 A → B → A 的有效恢复回归，并继续断言两项目拒绝对方的会话。
原生碰撞验收也应在 B 保存后再次恢复 A。

独立离线诊断使用原生格式夹具，没有调用模型或 CLI：

```json
{
  "same_directory": true,
  "a_before_b_accepted": true,
  "session_metadata_still_matches_a": true,
  "a_after_b_rejected": true,
  "b_foreign_a_rejected": true
}
```

复现命令：`python3 docs/audits/2026-10-08-git-target/followup-audit-probe.py`。
脚本见 [followup-audit-probe.py](followup-audit-probe.py)，结果见 [followup-audit-probe.json](followup-audit-probe.json)。

## 验证证据及范围

- 独立重跑 `test_git_target`：22 项通过，14.209 秒。见 [原始输出](followup-audit-target-tests.txt)。
- 核对提供的全量回归终态：251 项，0 失败、0 错误、14 跳过。本轮没有再次运行全量套件。
- 全部 28 个源文件哈希与修复清单一致；5 份已有验收输出哈希与结果清单一致。
- 审阅 DeepCode 0.4.3 原生 CLI 证据及碰撞脚本。原生 CLI 哈希匹配清单。本轮没有再次运行原生 CLI。
- 原生验收使用本地模拟模型和编译服务。真实 DeepSeek、真实 TeX、Windows/Linux 未验收。
- Git 回归使用临时本地仓库；没有操作真实论文远端。

核对结果见 [followup-audit-results.json](followup-audit-results.json)。
