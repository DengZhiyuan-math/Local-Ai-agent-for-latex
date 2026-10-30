# Executed fixture boundary

The bundled runner is the exact script used for the final1544-test local run. It discovers all current `tests/test*.py` modules, isolates PRISM/XDG/user-path defaults, clears inherited provider credentials, disables auto-update, and leaves HOME/CODEX_HOME unrepurposed. Individual protocol tests supply their own disposable profiles.

- Installed provider, GitHub-account, TeX and native-app executables are hidden or refused. Temporary Python protocol programs remain runnable.
- Git defaults to file transport. Only explicit loopback test URLs may additionally use HTTP. Python process/socket audit guards record prohibited launches/connections; the final run recorded none. These guards do not prove a global OS boundary over all native descendants.
- A scoped fixture selects Windows winget/known-location behavior for `test_tools.Tools` only, preserving global POSIX path identity. The output-folder regression separately uses standard-library Windows path semantics on the current host.
- Three explicit macOS app boundary exclusions are `test_macos_app.MacApp.test_browser_selection_scripts_compile_natively`, `test_macos_app.MacApp.test_bundle_runs_launcher_with_quoted_paths`, and `test_macos_app.MacApp.test_installer_refuses_an_unrelated_existing_app`.
- Existing `PRISM_SKIP_TEX=1`, platform and missing-native-CLI skips remain. `PRISM_NATIVE_CODEX` is absent. Exact56 skip IDs/reasons are in validation.json; no unexpected failure became a skip.
- Native Python sandbox/process/file-watch tests remain enabled. The Hub cache fixture fixes only the quiet public notification counter, explicitly verifies changed-counter invalidation, and closes its native watches at test cleanup. Production watcher/cache code is unchanged.
- Browser acceptance used an owned headless Chrome profile, scripted local API, actual controlled CLI adapters with Python protocol programs, FakeIssueAdapter, and real local bare GitSync.66 visible and7 restart checks passed. Temporary browser/service processes and ports were cleaned up.

No real model/vendor inference, product GitHub, TeX, installed App, native Windows or human product acceptance is asserted. Prior browser evidence predates the path/configuration follow-up; unchanged research/frontend source fingerprints and the final full run bind those components to this publication.
