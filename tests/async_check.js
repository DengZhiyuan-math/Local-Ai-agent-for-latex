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
    const answer = (p) => p === "old" ? new Promise(r => { finish = r; }) : Promise.resolve({ _status: 200, rate: { id: "new" }, skills: ["new"] });
    const ctx = { C, providerSeq: { usage: 0, catalog: 0 }, catalog: null, Q: { usage: answer, commands: answer },
      $: () => ({}), renderQuota: r => drawn.push(r), store: { get: () => null }, toast: () => {} };
    const name = kind === "usage" ? "checkUsage" : "loadCatalog";
    const end = kind === "usage" ? '\n$("#quota").addEventListener' : "\nfunction sysNote";
    const run = vm.runInNewContext(section("async function " + name + "(", end) + ";" + name, ctx);
    const old = run(); C.provider = "new"; await run();
    finish({ _status: 200, rate: { id: "old" }, skills: ["old"] }); await old;
    assert.strictEqual(kind === "usage" ? drawn.at(-1).id : ctx.catalog.skills[0], "new", "late " + kind);
  }
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
}
async function attachments() {
  let calls = 0, aborted = false;
  const ctx = { document: { querySelectorAll: () => [] }, $: () => ({}), MAX_UPLOAD_SIZE: 25000000,
    Date: { now: () => 200000 }, nap: async () => {}, toast: () => {}, foreignProject: false,
    Q: { uploadSync: async () => { if (++calls > 5) throw Error("attachment polling did not stop"); return { _status: 503 }; } } };
  const att = vm.runInNewContext(section("const ATT = {", '\n$("#chat-add").onclick') + ";ATT", ctx);
  att.sync.set("a.png", { state: "syncing", at: 0 });
  await att.pollSync();
  assert.strictEqual(att.sync.get("a.png").state, "unknown");
  att.files = [{ id: 1, controller: { abort: () => { aborted = true; } } }];
  att.render = () => {}; att.remove(1);
  assert.ok(aborted && att.files.length === 0, "removing a pending upload aborts it");
}
async function compilePreview() {
  let releasePreview, releaseBuild, calls = 0, shown = 0;
  const S = { active: "a.tex" }, ctx = { S, C: {}, BUILD: { mode: "draft" }, $: () => ({}),
    saveAll: async () => ({ ok: true }), setCompileButton: () => {},
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
account().then(usageAndCatalog).then(syncToggle).then(attachments).then(compilePreview).then(heldClose).then(errorNotice).then(() => console.log("ok")).catch(e => { console.error(e); process.exitCode = 1; });
