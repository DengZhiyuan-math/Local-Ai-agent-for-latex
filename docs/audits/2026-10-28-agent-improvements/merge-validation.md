# PR 集成验证

日期：2026-10-28。PR 分支：`D_m/agent-improvements`。集成基线：`e5392e0938b6970079916a96afc890fbc75cdbfa`（`origin/main`）。

## 最终范围

本地工作已按五个主题保存：HTTP 静态资源并发、编译结果凭证、SHA-256 条件保存、Agent 四项改进，以及旧同步验收资料。创建 PR 前合入当前 main，保留其 162 个后续提交中的功能。

合并时保留了现有请求校验、项目/嵌套仓库边界、编辑目标、集中错误/Stop 处理、stderr 上限、后台工具进度、搜索、流式批处理、Undo、snippets 和数学缓存。

集成调整：

- 额度同步事件在 `done` 之前发出，维持新版完成轮询语义，并增加 done-last 回归断言。
- Web 持久授权使用当前 `registry.write_state`；控制查询使用带项目边界的 `agent_env(root=...)`。
- label 导航使用当前 `TABS.saveAll()`。
- 数学 label 通过 `MathText.render()` 的可选回调添加链接，保留现有公式缓存并转义标签文本及属性。
- Claude 失败测试替身改用真实双向初始化协议。

## 自动验证

| 验证 | 结果 | 日志 |
| --- | --- | --- |
| 四项功能专项 | 48 项通过 | [专项](validation/pr-targeted-tests.txt) |
| 既有 Agent | 86 项通过 | [Agent](validation/pr-agent-tests.txt) |
| 后端契约 | 22 项通过 | [后端契约](validation/pr-backend-contracts.txt) |
| 服务/项目/设置 | 运行 178 项，跳过 2 项，其余通过 | [服务](validation/pr-services-tests.txt) |
| 全量 | 运行 776 项，跳过 52 项，9 个失败、2 个错误 | [全量](validation/pr-full-tests.txt) |
| 未修改当前 main 的对照 | 复现全部 11 条失败/错误，名称逐条一致 | [原有基线](validation/pr-main-baseline.txt)、[新增模块基线](validation/pr-main-extra-baseline.txt) |

全量未通过的项目：

- 原生 FSEvents 启动/通知和依赖通知的外部编辑自动同步：3 个失败、1 个错误。
- 安装/程序发现测试：3 个失败、1 个错误。
- Safari/Chrome 原生 AppleScript：2 个失败。
- POSIX 下 `C:/x` 输出目录：1 个失败。

这些检查在未修改的 `e5392e0` 副本、相同本机环境中产生相同结果。本 PR 保留这些既有检查，未以跳过或修改断言隐藏失败。

Python 编译、前端语法和 `git diff --check` 通过。独立合并审计未发现阻止 PR 的回归。

## 隔离 UI 验证

使用最新合并后的真实编辑器、HTTP 服务和 Claude adapter，CLI 及额度均由本地协议替身提供：

- 模型目录加载；选择 deep/high 后请求参数生效。
- WebSearch 在 UI 等待授权；会话批准后同一请求继续。
- 切换 fast 后保留历史，并发送 `--resume fixture-chat --model fast`，不重复请求已授权搜索。
- label 打开多文件源码的第 2 行；外部插入一行后再次点击定位第 3 行。
- 请求完成更新额度；手动查询失败保留原成功时间，并显示 stale 和错误。
- 新版默认编辑目标随已打开文件变化；页面未记录 console error。

没有真实模型推理或 Web 工具调用，未合并 PR、部署或安装 App。原始实现阶段的 573 项结果保留在 [原报告](report.md)，本文件记录提交 PR 前的最新结果。
