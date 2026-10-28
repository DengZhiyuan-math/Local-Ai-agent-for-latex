# AI Agent 权限、额度、配置与 LaTeX 导航改进
Label: wayfinder:map

## Destination

在现有架构内修复 Web 工具授权和额度刷新，并实现可切换模型/推理强度及可点击 LaTeX label；用回归测试验证前后端行为。

## Notes

- 用户明确要求实施，覆盖 wayfinder 默认的仅规划模式；本地图包含实现与验证。
- 单一集成决策票贯穿本次会话；按 Web 权限 → 额度 → 模型配置 → label 导航推进。
- 使用 grilling 和 domain-modeling 确认边界；需求已明确，代码能回答的问题先检查代码。使用测试驱动的回归验证。
- 复用现有后端、服务、UI 与 symbols 扫描；保留已有未提交修改。
- 使用本地 Markdown tracker，不创建远程 issue、不发布、不安装 App。

## Decisions so far

- [现有 Agent 架构如何可靠地承载权限、额度、模型配置与 label 导航？](issues/01-integration.md)：复用现有后端、事件和符号服务完成实现；专项验证通过，真实服务边界及既有全量失败见报告。

## Not yet specified

无；本次实现与验证范围已经明确并完成。旧 CLI 和真实外部服务验证边界见报告。

## Out of scope

新 Agent 框架、部署、安装 App、实际模型任务及外部服务的付费执行。
