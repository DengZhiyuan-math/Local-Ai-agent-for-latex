"use strict";
const assert = require("assert"), fs = require("fs"), vm = require("vm");
const source = fs.readFileSync(require.resolve("../prism_local/static/app.js"), "utf8");
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
async function account() {
  const el = {}, C = { provider: "old" }; let finish;
  const ctx = { C, providerSeq: { account: 0 }, $: () => el,
    Q: { account: (p) => p === "old" ? new Promise(r => { finish = r; }) : Promise.resolve({ _status: 200, account: { email: "new@example.com" } }) } };
  const run = vm.runInNewContext(section("async function loadAccount()", "\nfunction loadProvider()") + ";loadAccount", ctx);
  const old = run(); C.provider = "new"; await run();
  finish({ _status: 200, account: { email: "old@example.com" } }); await old;
  assert.ok(el.textContent.includes("new@example.com"), "a late old account cannot replace the new provider");
}
async function usageAndCatalog() {
  for (const kind of ["usage", "catalog"]) {
    let finish;
    const C = { provider: "old" }, drawn = [];
    const providers = { old: { usage_limits: true }, new: { usage_limits: true } };
    const answer = (p) => p === "old" ? new Promise(r => { finish = r; }) : Promise.resolve({ _status: 200, rate: { id: "new", unifiedWindows: {}, at: 200 }, skills: ["new"] });
    const ctx = { C, prov: () => providers[C.provider], ChatLogic: require("../prism_local/static/chatlogic.js"),
      providerSeq: { usage: 0, catalog: 0 }, catalog: null, Q: { usage: answer, commands: answer },
      $: () => ({}), renderQuota: r => drawn.push(r), store: { get: () => null }, toast: () => {} };
    const name = kind === "usage" ? "checkUsage" : "loadCatalog";
    const end = kind === "usage" ? '\n$("#quota").addEventListener' : "\nfunction sysNote";
    const run = vm.runInNewContext(section("async function " + name + "(", end) + ";" + name, ctx);
    const old = run(); C.provider = "new"; await run();
    finish({ _status: 200, rate: { id: "old", unifiedWindows: {}, at: 100 }, skills: ["old"] }); await old;
    assert.strictEqual(kind === "usage" ? drawn.at(-1).rate.id : ctx.catalog.skills[0], "new", "late " + kind);
  }
}
async function providerQuota() {
  const C = { provider: "old" }, queried = [], elements = {};
  const P = { byId: { old: { usage_limits: true }, fresh: { usage_limits: true } } };
  const ctx = { C, P, catalog: null,
    $: (id) => elements[id] ||= { classList: { toggle() {} } },
    store: { get: () => false, set() {} }, esc: x => x, icon: () => "",
    loadProvider() {}, loadAccount() {}, renderModelControls() {}, loadModels() {}, renderNotationGuard() {},
    checkUsage: (...args) => queried.push([C.provider, ...args]) };
  const run = vm.runInNewContext(section("let lastUsage =", "\nasync function checkUsage(") +
    section("function setProvider(", "\nfunction renderProviders(") + ";({renderQuota, setProvider})", ctx);
  const old = { identity: "old", rate: { at: Date.now() / 1000,
    unifiedWindows: { five_hour: { utilization: .5 } } }, stale: false };
  run.renderQuota(old);
  run.setProvider("fresh", true);
  assert.strictEqual(P.byId.fresh.usage.rate, null, "a provider without usage cannot inherit the previous snapshot");
  assert.strictEqual(P.byId.fresh.usage.stale, true);
  assert.ok(elements["#quota"].innerHTML.includes("Usage limits unavailable"));
  assert.ok(!elements["#quota"].innerHTML.includes("50% left"));
  assert.strictEqual(P.byId.old.usage, old, "switching providers keeps the previous provider's own snapshot");
  run.setProvider("old", true);
  assert.ok(elements["#quota"].innerHTML.includes("50% left"));
  assert.deepStrictEqual(queried, [["fresh", true, false], ["old", true, false]]);
}
async function syncToggle() {
  const SyncState = require("../prism_local/static/syncstate.js");
  const calls = [], replies = [];
  const toggle = SyncState.createToggle({ send: (value) => { calls.push(value); return new Promise(r => replies.push(r)); } });
  const run = toggle.change(true);
  assert.strictEqual(toggle.value(false), true);
  toggle.change(false);
  assert.strictEqual(toggle.value(true), false, "polls cannot override the latest intention");
  replies.shift()({ _status: 200 }); await new Promise(r => setImmediate(r));
  assert.deepStrictEqual(calls, [true, false], "writes are serialized");
  replies.shift()({ _status: 200 }); await run;
  assert.strictEqual(toggle.value(false), false);
  // A status asked for before a write (or during one) is older than it: not drawn.
  const before = toggle.stamp();
  assert.ok(toggle.fresh(before), "nothing written since");
  const again = toggle.change(true);
  assert.ok(!toggle.fresh(toggle.stamp()), "asked while a write runs");
  replies.shift()({ _status: 200 }); await again;
  assert.ok(!toggle.fresh(before), "asked before the write that has ended since");
  assert.ok(toggle.fresh(toggle.stamp()));
}
async function attachments() {
  let calls = 0, aborted = false; const cancelled = [];
  const ctx = { document: { querySelectorAll: () => [] }, $: () => ({}), MAX_UPLOAD_SIZE: 25000000,
    Date: { now: () => 200000 }, nap: async () => {}, toast: () => {}, foreignProject: false,
    Q: { uploadSync: async () => { if (++calls > 5) throw Error("attachment polling did not stop"); return { _status: 503 }; },
      uploadCancel: async (id) => { cancelled.push(id); return { _status: 200 }; } } };
  const att = vm.runInNewContext(section("const ATT = {", '\n$("#chat-add").onclick') + ";ATT", ctx);
  att.sync.set("a.png", { state: "syncing", at: 0 });
  await att.pollSync();
  assert.strictEqual(att.sync.get("a.png").state, "unknown");
  att.files = [{ id: 1, uploadId: "p-1", path: null, controller: { abort: () => { aborted = true; } } },
    { id: 2, uploadId: "p-2", path: "prism-uploads/b.png", controller: { abort: () => {} } }];
  att.render = () => {}; att.remove(1);
  assert.ok(aborted && att.files.length === 1, "removing a pending upload aborts it");
  // The server may have it all the same (the body had arrived), and a finished one it has.
  att.remove(2);
  assert.deepStrictEqual(cancelled, ["p-1", "p-2"], "removing a chip takes its upload back");
  assert.strictEqual(att.files.length, 0);
}
// What a page asks for outside api() names its project too, so another project that has
// taken the port refuses it: the PDF (a header), a file opened in a new tab (the URL).
async function projectBound() {
  const asked = [];
  const pv = vm.runInNewContext(fs.readFileSync(require.resolve("../prism_local/static/pdfview.js"), "utf8") + ";PV", {
    pdfjsLib: { GlobalWorkerOptions: {} }, $: () => ({}), projectKey: "paper-1",
    fetch: async (url, opts) => { asked.push(opts); return { ok: false }; } });
  pv.main = "main.tex"; await pv.load(5, "main.tex");
  assert.strictEqual(asked[0]?.headers?.["X-Prism-Project"], "paper-1", "the PDF names its project");
  const Client = require("../prism_local/static/client.js");
  assert.strictEqual(Client.assetUrl("figs/a b.png", "paper-1"), "/api/asset?path=figs%2Fa%20b.png&project_key=paper-1");
  assert.strictEqual(Client.assetUrl("a.png", null), "/api/asset?path=a.png");
  // After a restart, only this project's new server counts, not another one on the port;
  // after a folder rename, the project in the renamed folder.
  const wait = (answers, folder) => vm.runInNewContext(section("async function waitForNewServer(", "\nfunction reloadPage()") + ";waitForNewServer", {
    S: { server: 1, watch: { path: "C:\\p\\paper" } }, projectKey: "paper-1",
    fetch: async () => ({ ok: true, json: async () => answers.shift() }),
    Util: { waitForRestart: async (probe, isNew) => { for (let i = 0; i < 3; i++) { const a = await probe(); if (a && isNew(a)) return true; } return false; } },
  })(folder);
  const other = { server: 2, project_key: "other-2", path: "C:\\p\\other" };
  assert.notStrictEqual(await wait([other, other, other]), true, "another project's server is not this one's");
  assert.strictEqual(await wait([other, { server: 3, project_key: "paper-1", path: "C:\\p\\paper" }]), true);
  assert.strictEqual(await wait([other, { server: 3, project_key: "paper-v2-1", path: "C:\\p\\paper v2" }], "paper v2"), true);
  assert.notStrictEqual(await wait([other, { server: 3, project_key: "x-3", path: "C:\\q\\paper v2" }, other], "paper v2"), true);
}
async function compilePreview() {
  let releasePreview, releaseBuild, calls = 0, shown = 0;
  const S = { active: "a.tex" }, ctx = { S, C: {}, BUILD: { mode: "draft" }, $: () => ({}),
    TABS: { saveAll: async () => ({ ok: true }) }, setCompileButton: () => {},
    Q: { build: () => ++calls === 1 ? Promise.resolve({ _status: 200 }) : new Promise(r => { releaseBuild = r; }) },
    showBuild: () => ++shown === 1 ? new Promise(r => { releasePreview = r; }) : Promise.resolve() };
  const run = vm.runInNewContext(section("async function compile(", '\n$("#btn-compile").onclick') + ";compile", ctx);
  const first = run(); await new Promise(r => setImmediate(r));
  assert.strictEqual(S.building, false, "PDF loading must not keep the build busy");
  const second = run(); await new Promise(r => setImmediate(r));
  assert.strictEqual(S.building, true);
  releasePreview(); await first;
  assert.strictEqual(S.building, true, "an older preview cannot reset a newer build");
  releaseBuild({ _status: 200 }); await second;
}
async function heldClose() {
  let confirmations = 0; const notices = [], tab = { path: "a.tex" };
  const run = vm.runInNewContext(section("async function closeTab(", "\nfunction updateTabState(") + ";closeTab", {
    S: { tabs: [tab] }, C: { editTurn: true }, isDirty: () => true, saveTab: async () => false,
    toast: text => notices.push(text), Tabs: require("../prism_local/static/tabs.js"), confirm: () => { confirmations++; return false; } });
  await run(tab.path);
  assert.strictEqual(confirmations, 0, "held saves keep the tab instead of asking to discard it");
  assert.ok(notices[0].includes("turn ends"));
}
async function errorNotice() {
  const notices = [];
  const run = vm.runInNewContext(section("function toast(", '\ndocument.querySelectorAll(".gutter")') + ";toast", {
    notice: (text, error) => notices.push({ text, error }), $: () => ({}), toastTimer: null, toastPrev: null,
    setTimeout: () => 1, clearTimeout: () => {} });
  run("Save failed: fixture");
  assert.strictEqual(notices[0]?.error, true, "failures use the persistent notification stack");
}
async function localChip() {
  const input = { value: "my unfinished draft" }, commands = []; let click;
  const body = source.indexOf('const a = e.target.closest("[data-insert]")');
  const start = source.lastIndexOf('$("#chat-log").addEventListener', body);
  vm.runInNewContext(source.slice(start, source.indexOf("\n});", body) + 4), {
    $: (selector) => selector === "#chat-log" ? { addEventListener: (_, fn) => { click = fn; } } : input,
    chatSend: () => {}, updateScope: () => {}, runLocal: (name, arg) => commands.push([name, arg]) });
  click({ target: { closest: () => ({ dataset: { insert: "/model fast" } }) } });
  assert.strictEqual(input.value, "my unfinished draft");
  assert.deepStrictEqual(commands, [["model", "fast"]]);
}
async function manualSave() {
  let builds = 0;
  const run = vm.runInNewContext(section("async function saveActive()", "\n/* Autosave") + ";saveActive", {
    activeTab: () => ({}), clearTimeout: () => {}, TABS: { save: async () => ({ ok: false, reason: "held" }) },
    $: () => ({ checked: true }), compile: () => builds++, toast: () => {}, Tabs: require("../prism_local/static/tabs.js") });
  await run(); assert.strictEqual(builds, 0, "a failed manual save must not trigger auto-compile");
}
async function turnEndSaves() {
  const Tabs = require("../prism_local/static/tabs.js");
  const common = fs.readFileSync(require.resolve("../prism_local/static/common.js"), "utf8");
  for (const outcome of ["changed", "deleted", "unchanged", "unreachable", "exception", "unreachable-gone", "exception-gone"]) {
    const preexistingGone = outcome.endsWith("-gone"), gone = outcome === "deleted" || preexistingGone;
    const exception = outcome.startsWith("exception"), failed = exception || outcome.startsWith("unreachable");
    const deferred = () => { let resolve, reject; const promise = new Promise((r, j) => { resolve = r; reject = j; }); return { promise, resolve, reject }; };
    const events = deferred(), tree = deferred(), treeStarted = deferred();
    const C = { provider: "fixture" }, writes = [], notices = [], elements = {}, errors = [];
    const doc = { text: "Original paragraph.\n", generation: 0,
      getValue() { return this.text; }, changeGeneration() { return this.generation; },
      isClean(g) { return g === this.generation; } };
    const TABS = Tabs.createTabStore({ isHeld: () => C.editTurn, notify: text => notices.push(text),
      saveFile: async body => { writes.push(body); return outcome === "unchanged"
        ? { _status: 200, mtime: 2 } : { _status: 409, conflict: true, gone }; } });
    const tab = TABS.add("main.tex", doc, 1);
    const S = { tabs: TABS.tabs, files: [{ path: tab.path, mtime: 1 }], pdfMtime: null, pdfMain: null };
    const context = { C, S, TABS, Tabs, openAsked: 0, foreignProject: false, BUILD: { loaded: true },
      RESEARCH: {}, PS: { open: false }, UNREACHABLE: "server not reachable",
      Watch: require("../prism_local/static/watch.js"), Turn: require("../prism_local/static/turn.js"),
      Q: { events: () => events.promise, tree: () => { treeStarted.resolve(); return tree.promise; } },
      $: selector => elements[selector] ||= { classList: { add() {}, remove() {} }, querySelectorAll: () => [],
        scrollHeight: 0, scrollTop: 0, clientHeight: 0, checked: false },
      document: { visibilityState: "visible" }, setInterval: () => 1, clearInterval() {}, clearTimeout() {},
      AV: { render() {}, end() {} }, AH: { clear() {}, on() {}, complete() {} },
      Util: { frameBatch: () => ({ flush() {}, set() {} }) }, SYNC_TOGGLE: { stamp: () => 0 },
      isDirty: TABS.dirty, activeTab: () => tab, toast: text => notices.push(text), bindProject: () => true,
      setUndoBusy() {}, renderModelControls() {}, loadResearch() {}, saveChatLog() {}, renderTurnCard() {},
      loadAccount() {}, showProjectName() {}, renderTree() {}, loadSymbols() {},
      console: { error: error => errors.push(error) },
    };
    // Use the real poller: a call made during an existing poll waits for another
    // run. The delayed tree response below therefore blocks turn reconciliation.
    const pollerSource = common.slice(common.indexOf("function poller("), common.indexOf("\n/* theme */"));
    const run = vm.runInNewContext('"use strict";\n' + pollerSource + "\n" +
      section("async function pollOnce()", "\n/* ------------------------------------------------------------------ GitHub sync */") +
      "\nconst poll = poller(pollOnce, 2000, 30000);\n" +
      section("async function saveActive()", '\nwindow.addEventListener("blur"') +
      section("async function followTurn(", "\nfunction permissionCard(") +
      ";({followTurn, autosave, saveActive})", context);
    const turn = run.followTurn("turn-1", "fixture", "edit");
    doc.text += "My manual addition.\n"; doc.generation++;
    if (preexistingGone) TABS.diskSeen(tab, null);
    await run.autosave(tab);
    assert.strictEqual(writes.length, 0, "manual edits wait while the Agent is editing");
    events.resolve({ _status: 200, events: [{ t: "done", changed: outcome === "unchanged" || failed
      ? [] : [{ path: tab.path, deleted: gone }] }], done: true });
    await treeStarted.promise;
    // An already scheduled autosave, Cmd/Ctrl+S, or window blur can occur while
    // the final disk poll is in flight. None may write the old editor version.
    await run.autosave(tab);
    await run.saveActive();
    const beforeReconcile = [...writes], jobWhileReconciling = C.job;
    if (exception) tree.reject(new Error("fixture tree failure"));
    else tree.resolve(failed ? { _status: 0, error: "fixture offline" }
      : { _status: 200, project_key: "paper", files: gone ? [] : [{ path: tab.path, mtime: outcome === "unchanged" ? 1 : 2 }],
        pdf_mtime: null, pdf_main: null });
    await turn;
    await new Promise(r => setImmediate(r));
    assert.strictEqual(beforeReconcile.length, 0,
      "turn-end autosave must stay held until the Agent's changed/deleted files are reconciled");
    assert.strictEqual(jobWhileReconciling, "turn-1", "a new turn waits until the old turn has reconciled its files");
    assert.strictEqual(C.job, null, "cleanup releases the turn even when its final tree refresh fails");
    assert.strictEqual(C.editTurn, false, "cleanup always releases the save hold");
    assert.strictEqual(writes.length, outcome === "unchanged" ? 1 : 0,
      "only a reconciled, unchanged file may save the queued manual text automatically");
    assert.strictEqual(tab.conflict, outcome === "unchanged" ? null : gone ? "gone" : "disk",
      "a failed refresh must preserve the deleted-file conflict and its Save as action");
    assert.strictEqual(doc.getValue(), "Original paragraph.\nMy manual addition.\n", "manual text remains available");
    assert.strictEqual(TABS.dirty(tab), outcome !== "unchanged");
    assert.ok(!notices.some(text => text.includes("not saved") || text.includes("Save failed")),
      "normal turn reconciliation must not send a stale save and report a save error");
    if (failed) assert.strictEqual(notices.filter(text => text.includes("Compare with disk")).length, 1,
      "a failed final refresh explains how to resolve the held manual edits once");
    assert.strictEqual(errors.length, exception ? 1 : 0);
  }
}
account().then(usageAndCatalog).then(providerQuota).then(syncToggle).then(attachments).then(projectBound).then(compilePreview).then(heldClose).then(errorNotice).then(localChip).then(manualSave).then(turnEndSaves).then(() => console.log("ok")).catch(e => { console.error(e); process.exitCode = 1; });
