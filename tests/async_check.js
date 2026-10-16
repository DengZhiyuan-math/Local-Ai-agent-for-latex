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
account().then(usageAndCatalog).then(syncToggle).then(() => console.log("ok")).catch(e => { console.error(e); process.exitCode = 1; });
