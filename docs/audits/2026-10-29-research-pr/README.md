# Research notation and project lifecycle integration

This review bundle covers the twelve engineering slices of [Spec: Research initialization and conversational approval of new notation](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/10), their integration repairs, and the subsequent cross-platform output-folder fix. GitHub Issues remain the authoritative specification and tracker.

## Resulting behavior

- Explicit initialization records the manuscript baseline, terms and complete notation. Known manuscript notation remains usable; new AI notation stays pending until a source-bound human decision is saved.
- Ordinary chat and designated human GitHub replies share direct adoption, batch decisions, families, durable refusal and reopening rules. A new refusal can withdraw an approval. Quoted text, stale versions, untrusted accounts, old comment edits and replay cannot manufacture authority.
- API, Claude Code, Codex and Deep Code use the shared policy through controlled tools. Session/model changes preserve decisions. Stale whole-file writes and Undo preserve external saves.
- Continuing topics are recorded in Markdown and can use GitHub issues with project lifecycle checks. Actual clones retain the original topic ID/path and received text while keeping local approval authority independent. Unknown GitSync receipts require source-bound confirmation.
- Windows drive, drive-relative, UNC and slash-prefixed drive output folders fall back to project-local `build` on every host. Existing `/x` to `x` normalization remains unchanged.

## Validation

| Check | Result |
| --- | --- |
| Final full discovery | **1544 tests: 1488 passed, 56 existing platform/boundary skips, zero failures/errors**, process exit0; 791.139 seconds |
| Source integrity | 458 source fingerprints, no drift during the final run |
| Visible browser | 66 checks plus7 server-restart checks passed on the earlier research candidate; those research/frontend sources remain unchanged |
| Requirement mapping | 29 parent scenarios and98 engineering clauses; named unit/HTTP cases reran in final discovery; visible interaction also uses the browser record |
| Independent review | No remaining actionable Standards/Spec findings on the research candidate; the later path and cache-fixture deltas were separately reviewed |

[validation.json](validation.json) records exact skip IDs/reasons and scope. [coverage.json](coverage.json) maps requirements to current named tests; its rows are supporting evidence, not independent test counts. [browser-checks.json](browser-checks.json) preserves the check names without private fixture state. [source-manifest.json](source-manifest.json) records the tested source candidate.

The original `C:/x != build` assertion was preserved. Public Config coverage now checks11 Windows path forms and all three artifact paths. A separate pre-existing scan-cache timing failure was reproduced with both original and fixed Config: native notification changes invalidate the scan despite unchanged source stats. The quiet-cache fixture now fixes that input, a new0-to1 counter test requires invalidation, and each Hub test releases its native watches with the existing reset API. Production scanner code did not change. Intermediate failed/interrupted runs and the initial exit139 during native teardown remain in the local audit archive; none were reported as passing process results.

## Reproduce the recorded local boundary

On macOS, with existing Python, Node and Git:

```sh
python3 docs/audits/2026-10-29-research-pr/validation/full-regression.py --final
```

The exact validated runner is included. It writes ignored result/discovery metadata next to itself; redirect stdout/stderr when a console log is wanted. Its temporary-directory choice and native sandbox checks are host-specific; this command is not a claim of native Windows acceptance. [execution-boundary.md](execution-boundary.md) explains what ran and what was excluded. The ordinary repository command remains `python3 -m unittest discover -s tests`; the isolated runner avoids invoking installed real services and toolchains during this fixture acceptance.

Real models/vendor CLIs, product GitHub execution, TeX, installed App and human product acceptance remain unrun. The actual adapters use local Python protocol programs and a fake GitHub service. Python audit hooks and local-only Git routing are documented controls, not a global OS sandbox proof for every native descendant.

## Engineering tickets

| Ticket | Parent scenarios |
| --- | --- |
| [[研究知识 1] 项目初始化与研究基线报告](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/11) | 1, 2, 3, 4, 5, 20, 27, 28 |
| [[研究知识 2] 增量记号识别与可信来源补录](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/12) | 3, 6, 8, 9, 10, 11, 20, 23, 27 |
| [[研究知识 3a] API 新符号保护与待决提案](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/13) | 6, 7, 11, 12, 18, 19, 27, 28 |
| [[研究知识 3b] API 普通文字决定与持久审批闭环](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/14) | 12, 13, 15, 17, 20, 27, 28 |
| [[研究知识 4] 批量决定、直接指令与持久拒绝](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/15) | 7, 8, 14, 15, 16, 17, 20, 27, 28, 29 |
| [[研究知识 5] Claude Code 的完整受控流程](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/16) | 6, 7, 8, 12, 13, 14, 15, 16, 17, 18, 19, 20, 27, 28, 29 |
| [[研究知识 6] Codex 的完整受控流程](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/17) | 6, 7, 8, 12, 13, 14, 15, 16, 17, 18, 19, 20, 27, 28, 29 |
| [[研究知识 7] Deep Code 的完整受控流程](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/18) | 6, 7, 8, 12, 13, 14, 15, 16, 17, 18, 19, 20, 27, 28, 29 |
| [[研究知识 8] 自动维护项目内研究主题记录](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/19) | 17, 19, 20, 21, 22, 27 |
| [[研究知识 9] GitHub 研究主题记录与项目生命周期同步](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/20) | 20, 21, 25, 26, 27 |
| [[研究知识 10] 通过 GitHub 人类回复审批符号](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/21) | 13, 14, 16, 17, 20, 24, 25, 26, 27 |
| [[研究知识 11] GitSync 协作中的记录与符号来源核验](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/22) | 10, 16, 17, 20, 22, 23, 27 |

## Provenance and publication

The verified source digest is `8af8528d56b2868ff0586267ea771cbae1775488f619d7ec5d721420fe7457d0`, based on `1d7b7c9735cba777d985d635b97eaab8fdf5899a`. Publication adds this bounded review bundle and repository-agent documentation. One trailing blank line in `tests/test_deepcode_research_http.py` was removed after the staged whitespace check; the complete AST including source locations is identical, and appending one LF reconstructs the validated bytes. `source-manifest.json` records both the validated and publication digests. Product and executable test statements are unchanged. Large raw diagnostics, temporary projects/profiles, generated before-snapshots and the duplicate `.scratch` specification stay local. Three tracked diagnostic-folder markers keep the existing failure-log destinations available in a fresh clone while ignoring their generated contents. The canonical closing evidence is also recorded in the [integration result](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/10#issuecomment-6505533153) and [green follow-up](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/10#issuecomment-6508765515).

The parent specification stays open for human review. This Draft PR does not merge or install the implementation.
