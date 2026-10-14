# 第二阶段 D：实例与进程生命周期

开始于 2026-10-14，于 2026-10-15 完成本地实现和验证（Europe/Berlin）。基线及验证后 HEAD 均为 `773805a6402ecd11f2aa77ad2413a67048e47cbc`。本报告及 results.json 记录 D 提交前的验证快照；当时未暂存、未提交、未推送。随后按用户要求单独提交 D，独立的 Codex Windows 沙盒修复不包含在该提交中。

## 实施

1. `instances.py` 统一只读实例查询、启动、服务注册、重启交接和目录移动排他。状态区分 ready、unresponsive、busy、absent；慢实例不会被当作未运行。
2. 启动使用进程间文件锁、操作记录和随机启动 token。服务 PID、创建时间、应用及根目录一起校验；兼容旧实例记录，并验证 Windows venv 启动器 PID 与实际服务器 PID 不同的情况。
3. 启动和重启交接等待有期限，失败清理子进程；等待线程持有并回收 Popen。重启时子进程先以启动 token 接管全部预留路径的操作记录，父进程确认接管后释放锁并退出，子进程取得锁后完成移动和就绪发布。锁交接空隙也受子进程的忙碌记录保护。这样 Windows 旧进程及 venv 启动器能释放工作目录，不再阻塞新进程改名。交接结果明确区分“已接管”和失败；打开操作仍等待实际 ready 状态。Home、编辑器和启动器都使用同一套协议。
4. 直接 CLI 启动也发布规范实例记录。自定义 ready 文件作为别名保留；别名在项目内部时随目录移动。Home 重启保持实际监听端口，目录改名后的再次重启不重放旧移动参数。
5. Home 改名、共享/独立仓库改组锁定源和目标路径，改组还在锁内重新校验计划，并拒绝重叠结构操作。编辑器改名重启也同时预留旧、新路径。
6. `registry.move_folder` 在移动前读取并校验项目列表，移动与列表更新共用元数据锁。列表写入失败尝试回滚；回滚失败报告实际位置并保留 `state/moves/*.json` 恢复记录。跨盘移动若部分复制后失败，只执行一次，报告源、目标及恢复记录，避免再次移动到已经存在的目标内部。恢复记录供核对和修复路径使用，不会自动覆盖之后的项目列表。

## 验证

| 检查 | 结果 | 记录 |
|---|---|---|
| Windows 工作目录阻塞，修复前 | 1 项失败，15.250 秒，能捕获问题 | [cwd-red-tests.txt](cwd-red-tests.txt) |
| 最终交接协议专项 | 37 项全部通过，19.076 秒 | [handoff-tests.txt](handoff-tests.txt) |
| 最终完整回归 | 435 项：411 通过、24 跳过，0 失败、0 错误，527.332 秒 | [full-tests.txt](full-tests.txt) |
| 目录与改组专项 | 20 项通过，25.703 秒 | [move-tests.txt](move-tests.txt) |
| 跨盘失败保护及元数据回归 | 8 项通过，0.063 秒 | [move-guard-tests.txt](move-guard-tests.txt) |
| 重启、改名和并发打开压力 | 20 轮、120 项全部通过，4 个独立进程并发，55.015 秒 | [handover-stress-results.json](handover-stress-results.json) |
| 输入、同步按钮及合作者推送压力 | 100/100 轮通过，4 个独立进程并发，490.282 秒 | [sync-stress-results.json](sync-stress-results.json) |
| 基线夹具警告复现 | 2 项通过，复现相同 2 条警告，6.298 秒 | [baseline-fixture-warnings.txt](baseline-fixture-warnings.txt) |

两组压力测试均无资源警告。生命周期场景覆盖 Home 连续重启、外部及项目内 ready 别名、改名后的再次重启、直接 CLI 去重、Windows 工作目录及真实 venv 启动器。同步场景每轮核对输入和合作者修改均到达远端、按钮无错误、仓库无未完成状态。跨盘失败分支使用注入故障验证；真实移动和 Git 改组在本机临时目录中运行。

完整回归使用 Windows 本机 Python/Git，命令为 `PRISM_SKIP_TEX=1 PYTHONWARNINGS=always::ResourceWarning python -m unittest discover -s tests -v`，未启用 `PRISM_NATIVE_CODEX`。24 项跳过包括真实 TeX 12 项、需显式开启的原生 Codex 探针 3 项、其他平台专用检查 8 项、符号链接检查 1 项。没有执行真实模型或 GitHub 账户操作；Git 传输测试使用本地裸仓库。

显式启用子进程资源警告后，完整回归中有两条既有夹具警告：`test_gitsync` 的子进程持锁退出，及 `test_server` 的子进程持有 `main.tex` 退出，均未显式关闭该句柄。用未包含 D 改动的基线提交独立导出重跑，复现同样警告。测试检查的是操作系统在进程退出后释放文件；本轮没有改动这两个夹具，也没有把结果描述为完全无资源警告。此前启动后台服务器后丢弃 Popen 的警告未再出现。

## 范围与保留检查

以下 Codex 修复文件的 SHA-256 与接手 D 时完全一致：`prism_local/backend_codex.py`、`README.md`、`tests/test_codex_discovery.py`、`tests/test_codex_runtime.py`。显式 Windows 沙盒配置、CLI 查找和 hooks 隔离改动均保留。

13 个 D 代码及测试文件的 SHA-256 在最后一次完整回归开始后保持不变；语法检查、`git diff --check` 通过，暂存区为空。完整统计、源文件哈希及保留校验见 [results.json](results.json)。

本轮没有开展 B4、C、H、G。D 的生产改动限于 `instances`、`registry`、`httpbase`、`server`、`hub`、发布通知入口及启动器。未修改论文文件、未暂存、未提交、未推送。

本轮更换了进程内部交接协议。应用改动时，需要先完整停止旧 Home/编辑器进程，再从启动器重新启动一次；旧进程在内存中保留的 F 协议不能通过其原有热重启锁与新协议衔接。旧格式的健康实例记录仍可查询和复用。当前运行服务的切换不属于本轮源码修改及测试。
