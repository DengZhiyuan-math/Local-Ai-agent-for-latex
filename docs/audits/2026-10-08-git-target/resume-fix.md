# DeepCode 碰撞项目交替恢复修复

日期：2026-10-08。依据：[独立复核](followup-audit.md)。
产品改动位于本地未提交工作区，没有推送。

`DeepCode.check_session()` 已删除共享索引 `originalPath` 必须等于当前项目的条件。
项目归属继续由 `sessionmeta.matches()` 核对会话原始元数据。
会话 ID、索引成员、文件路径、符号链接和缺少元数据的拒绝检查保留。
不增加依赖、会话数据库或新的目录编码。

单元回归模拟两个真实碰撞目录中的原生会话文件与索引。
A 创建会话后，B 保存会话并覆盖索引目录字段。
A 再恢复自己的会话必须成功。
A 改写索引后，B 的有效会话也必须继续通过检查。
两个项目始终拒绝对方的会话。

| 检查 | 本轮结果 | 证据 |
| --- | --- | --- |
| 修复前回归 | 1 项失败；A 返回后被拒绝 | [失败记录](resume-fix-red.txt) |
| 最小修复后的同一回归 | 1 项通过 | [通过记录](resume-fix-green.txt) |
| `test_git_target` 与 `test_agent.DeepCodeBackend` | 26 项，0 失败、0 错误、0 跳过；18.486 秒 | [原始输出](resume-fix-tests.txt) |
| DeepCode 0.4.3 原生 CLI | 5 回合通过：A 新会话、A 恢复、B 新会话、A 恢复、B 恢复；跨项目请求在模型调用前被拒绝 | [原始输出](resume-fix-native.txt)，[后继脚本](resume-fix-native.py) |
| Python、diff | py_compile 与 git diff --check 通过 | 本轮命令终态为 0 |

原生验收使用本地模拟模型。
真实 DeepSeek、真实 TeX 和 Windows/Linux 本轮未验收。
Windows/Linux 保持此前用户要求的跳过状态。
本轮没有重跑全量套件；此前 251 项结果属于此前源码版本。

相对上次审计的 28 个源文件，本轮仅改动
`prism_local/backend_deepcode.py` 和 `tests/test_git_target.py`。
另 26 个源文件保持不变。
已有审计、探针、清单、原生验收脚本和输出均保持原始哈希。
新增验收脚本在原脚本基础上补充交替恢复检查。
源码与输出哈希见 [resume-fix-results.json](resume-fix-results.json)。
