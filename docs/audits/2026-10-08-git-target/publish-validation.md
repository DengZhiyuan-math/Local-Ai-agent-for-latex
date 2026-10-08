# 发布前验收

日期：2026-10-08。提交基线：`9c2f0ef28b2d0827272e746b58c157be7c94441c`。
发布目标：`DengZhiyuan-math/Local-Ai-agent-for-latex` 的 `main`。

本次发布包括已审计的 AI 后端适配、Git 同步目标与环境隔离、项目会话归属及 DeepCode 交替恢复修复。
全部 28 个源文件与最终修复清单哈希一致。既有审计与验收证据保留。

当前代码重新运行全量回归：251 项，0 失败、0 错误、14 跳过；246.036 秒。
JavaScript 语法和 `git diff --check` 通过。

全量命令：`PRISM_SKIP_TEX=1 python3 -m unittest discover -s tests`。
本轮未重新运行原生 CLI。此前 DeepCode 0.4.3 的五回合证据使用本地模拟模型。
真实 DeepSeek、真实 TeX 和 Windows/Linux 未验收。

[原始测试输出](publish-full-tests.txt) · [源码及输出哈希](publish-validation-results.json)
