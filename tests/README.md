# Fixture tests

The application uses only the Python standard library. On Python 3.9 and 3.10,
the tests additionally use `tomli` to independently parse generated TOML.

```sh
python -m pip install -r tests/requirements.txt
python tests/run_ci.py
```

The runner discovers every `test*.py` module and writes `.ci-results/results.json`.
The report includes failures, exact skip reasons, discovered test names, source
drift and blocked process/network attempts. Any failure, drift or blocked attempt
fails the run. Use `--modules test_projectconfig test_research_language` for a
focused diagnosis, or `--output-dir PATH` to choose the report directory.
Failures print their traceback immediately, so a later deadline cannot hide an
earlier failure. `--shard 1/2` and `--shard 2/2` split sorted modules into two
disjoint groups. Together they execute every discovered test; module and class
fixtures remain together. Reports record the shard and total discovery count.

PRs, including Draft PRs, run the full suite on Ubuntu, Intel macOS and Windows,
with Python 3.9 and 3.14 and Node 24. The workflow uses a read-only repository
token, does not persist checkout credentials, and uploads the JSON report even
when tests fail. It also runs on pushes to `main` and by manual dispatch.
Each environment runs two module shards, for twelve full-suite jobs. Windows
also checks known portability regressions before the full suite and uploads
that compatibility report separately. No quick check replaces full execution.
A quick real HTTP startup check precedes full discovery, so a broken fixture
cannot produce hundreds of readiness failures before its first diagnostic.
Its `.ci-results/startup/results.json` is uploaded separately from the full
suite's report; a startup pass never substitutes for complete test execution.
CI startup and long-running process diagnostics include bounded thread dumps;
failed setup also cleans up its server and temporary directory.

Each run uses private temporary PRISM/XDG/home defaults, clears inherited provider
credentials, and limits Git transport and Python HTTP fixtures to local hosts.
Installed vendor CLIs and TeX tools are hidden and guarded. These are Python audit
controls, not a global OS sandbox for native descendants. No models, product
GitHub synchronization, TeX distribution or vendor CLI are installed or enabled.
The real Codex opt-in tests remain disabled. Native Windows process, watcher,
file-sharing and installer-shell fixtures run on Windows.

The controlled CLI HTTP/protocol fixtures require the actual macOS sandbox;
other platforms retain the shared policy and API HTTP tests. Three temporary
macOS app compile/sign/AppleScript tests require `--native-app-fixtures`. CI
explicitly enables them on its disposable hosts. They build or inspect only
temporary app paths and do not install into `/Applications`. The default local
command leaves those three tests skipped. Ordinary platform and missing-tool
skips remain explicit in each report; failures are never converted to skips.

The historical audit runner under `docs/audits/2026-10-29-research-pr/validation/`
remains unchanged for its recorded local evidence. New CI uses this maintained,
portable entry point.
