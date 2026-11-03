# Draft PR 27: review and repairs

This follow-up reviews [Draft PR 27](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/pull/27) against [specification 10](https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex/issues/10) and engineering tickets 11–22. The independent Standards and Spec reviews started at head `b32b3a852a739abc915b053b12506e0abdd827b7`, against base `1d7b7c9735cba777d985d635b97eaab8fdf5899a`. All 138 files in that comparison received review. Requirement coverage used the 29 scenarios and 98 clauses as a checklist, not as execution evidence.

## Standards

The review found **zero hard documented-rule violations** and **two optional refactoring observations**: the `controlled_cli` flag also represents API parent-owned writes, and five policy test bodies repeat across the controlled CLI suites. These observations do not establish incorrect behavior. Broad fixture refactoring or renaming across adapters was not needed for the repairs below.

## Spec

The review found **two P1 and six P2 defects**. All eight repairs passed independent bounded rechecks. The frozen production/test repair diff received a separate Standards recheck with no new findings. The complete regression result is recorded below separately.

| ID | Priority | Defect at the reviewed head | Repair |
| --- | --- | --- | --- |
| S1 | P1 | Conditional human instructions could immediately adopt notation, including a detached condition after a separator. | Keep the condition with its decision, request clarification, and reject unexplained trailing qualifications after a complete math definition. Exact formulas and ordinary verbal definitions remain usable. |
| S2 | P1 | Late retrieval of an older GitHub instruction could erase a newer refusal. | Compare verified decision origins for the exact subject and overlapping name/expression scopes, including other proposals and finite families. Later edits do not refresh an old comment's creation time. Unrelated subjects remain independent. |
| S3 | P2 | Existing `\mathbb R`, `\hat x`, and `\hat\theta` lost their parsed identity during evidence lookup. | Normalize supported single-token arguments consistently before whitespace removal. Preserve command and styled-identifier distinctions. |
| S4 | P2 | `\operatorname*{lim}` created fake new symbols. | Treat the optional star as layout. Standard operators remain exempt; custom operator names still require adoption. |
| S5 | P2 | Literal unbraced `\input chapters/proof` was absent from initialization and refresh coverage. | Reuse one include pattern across extraction and the source guard. Keep path and source-limit checks. |
| S6 | P2 | An extraction exclusion blocked a later explicit human adoption of the same notation. | Keep extraction corrections separate from current verified chat and new manual-adoption evidence. Preserve excluded historical evidence and effective refusals. |
| S7 | P2 | Case aliases such as `ReSeArCh/topic.md` bypassed the protected research namespace on a case-insensitive filesystem. | Reserve every case spelling of the top-level research namespace in the shared parent write gate. |
| S8 | P2 | A refused source consumed the refresh byte budget, hiding later sources that initialization had accepted. | Charge the aggregate budget only for accepted, decoded sources. |

The separate compatibility review found one Windows test failure: an unguarded `os.mkfifo` call. The FIFO fixture now skips only when that API is unavailable. Its missing-API branch was exercised in a separate process on this host; this is not native Windows acceptance.

## Validation and execution boundary

Final validation is recorded in [validation.json](validation.json), with exact source fingerprints in [source-manifest.json](source-manifest.json). The complete run finished successfully: **1570 tests: 1514 passed, 56 existing platform/boundary skips, zero failures/errors**, process exit 0, in 794.178 seconds. All 26 new regression methods were discovered and passed. All 458 source fingerprints matched before and after; there was no post-test source change. No real-provider/process or external-network audit block occurred. GitHub had no recorded checks/statuses/workflow runs at review start, and this tree contains no Actions workflow; the local result is not a CI pass. The earlier [integration bundle](../2026-10-29-research-pr/README.md) remains historical evidence for its recorded source versions.

Reproduce the full isolated local regression on macOS with the existing Python, Node and Git runtimes:

```sh
python3 docs/audits/2026-10-29-research-pr/validation/full-regression.py --final
```

The original runner and its exclusions are unchanged. Local Python protocol programs, temporary projects and fake GitHub adapters exercise the product paths. Real models/vendor CLIs, product GitHub synchronization, TeX execution, an installed App, native Windows and human product acceptance remain unrun. The audit guards do not claim to be an operating-system sandbox for every native descendant. Python 3.9 grammar compatibility was checked statically; a Python 3.9 runtime was not available.

The first full attempt exposed an existing HTTP fixture whose synthetic GitHub timestamp preceded a completed real-time editor approval. An isolated test reproduced the failure. That attempt was deliberately stopped with exit 130. The fixture now gives only the genuinely new rejection its actual UTC creation time; the later historical replay, failure marker, exact receipt checks and restart assertions remain intact. The complete GitHub approval HTTP module then passed 18/18. Only the subsequent complete run counts as final acceptance.

Cross-channel GitHub/editor ordering uses actual GitHub creation timestamps and verified local receipt timestamps. It assumes the trusted host UTC clock is accurate. It is not a causal ordering guarantee under arbitrary host/service clock skew. An edited older GitHub comment cannot acquire a new creation time.

The PR remains Draft. No merge, installation or human acceptance is implied. Raw local diagnostic logs and temporary receipt/private state are excluded from this published report.
