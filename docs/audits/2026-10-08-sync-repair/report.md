# 论文同步修复验收

日期：2026-10-08。平台：本机 macOS。

## 原因与修复

1. Agent 的完整 Undo 快照包含 `build/` 编译产物。`GitSync.after_turn()` 将快照路径直接传给 `git add`，被 Git 的忽略规则拒绝，未完成提交。现在复用自动保存已有的 `changes()` 筛选，再限定为本回合路径。Undo 快照保持完整。
2. 论文仓库的 `.git/objects/pack/` 中有 AppleDouble 元数据文件 `._pack-2f2a555809d8bee591b5c14c8f0b51d17592bfe8.idx`。Git 将它当成索引，报 `non-monotonic index`。该文件已备份隔离。真实 `pack-*.idx` 和 pack 在隔离前后均通过 `git verify-pack -v`。

## 测试

- 新增临时仓库测试先在旧实现中复现忽略 `build/` 导致提交失败，再在修复后通过。
- `test_gitsync`：15 项全部通过。覆盖忽略文件、已跟踪的忽略文件、删除、独立用户编辑、仅有忽略文件的回合、共享仓库前缀和本地 bare remote 推送。
- `test_git_target test_backend_contracts test_agent test_collab`：100 项全部通过，耗时 213.551 秒。
- `git diff --check`：通过。
- Windows/Linux 真实验收按用户要求跳过。本次没有重跑全量测试，也没有新增真实模型请求。

## 本机恢复结果

- 项目：`papers/DengZhiyuan-math/Average-over-Borel-subgroups`。
- 通过编辑器的 **Save to GitHub now** 恢复同步。操作前核对当前目标与已绑定目标一致，无阻断原因。
- 目标：`https://github.com/DengZhiyuan-math/Average-over-Borel-subgroups.git`，`origin`，`refs/heads/main`。
- 提交：`52077333eee8772ca30d19e94692ef8662e999c3`，`Saved: main.tex, refs.bib`。提交仅包含 `main.tex`、`refs.bib`。
- `git ls-remote` 返回的远端 main 与本地 HEAD 相同。论文工作区干净，编辑器显示 **Saved to GitHub**，`error=null`，`ahead=0`，`behind=0`。
- 通过编辑器的 **Update: restart** 加载修复。服务 PID 从 `47437` 变为 `3188`，路径和端口 `8819` 保持一致，`updated=false`。
- 8 个源文件和编译产物在恢复同步及重启后均与备份逐字节一致。
- 产品仓库中的修复仅在本地，未提交或推送。其他已有修改保留。

## 可恢复备份

备份目录：

`papers/.sync-repair-backups/2026-10-08-cffa80d6-ff30-4100-8a60-bb66f53adf90/`

- `current/`：当前 8 个文件。
- `before-agent/`：回合前的 `main.tex` 和 `refs.bib`，来源提交 `b30ce834d64ea56efb435cd7f5b0c0427c4225b5`。
- `git-index.before.bin`：恢复前的暂存索引。
- `agent-done.json`：已完成回合的结果与差异记录。
- `record.json`：恢复前状态和 SHA-256 清单。
- `AppleDouble-pack-index.bin`：隔离的旁文件，SHA-256 `b8be5152faa30faaa5da481a2695155fb1f366cb511d19bcc6d9042582a0681b`。

重启清除了旧回合的内存 Undo。浏览器保留的历史按钮不能恢复该内存记录；回合前源文件可从上述备份恢复。新回合仍使用原有的完整 Undo 快照。
