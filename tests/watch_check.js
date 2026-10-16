"use strict";
const assert = require("assert");
const Watch = require("../prism_local/static/watch.js");
const Tabs = require("../prism_local/static/tabs.js");
const tree = (mtime = 1) => ({ project_key: "paper", files: [{ path: "a.tex", mtime, git: "" }], folders: [], order: ["a.tex"], pdf_mtime: null });
const tab = { path: "a.tex", mtime: 1, dirty: true };
assert.strictEqual(Watch.reconcile(tree(), { ...tree(), project_key: "other" }, [tab]).foreign, true);
assert.deepStrictEqual(Watch.reconcile(tree(), tree(2), [tab]).conflicts, ["a.tex"]);
assert.deepStrictEqual(Watch.reconcile(tree(2), tree(2), [tab]).reload, []);
assert.strictEqual(Watch.reconcile(tree(2), tree(2), [tab]).symbols, false, "a standing conflict does not reload symbols");
assert.deepStrictEqual(Watch.reconcile(tree(), { ...tree(), files: [] }, [tab]).gone, ["a.tex"]);
assert.deepStrictEqual(Watch.reconcile(tree(), tree(2), [{ ...tab, dirty: false }]).reload, ["a.tex"]);
assert.deepStrictEqual(Watch.reconcile(tree(), tree(2), [{ ...tab, inFlight: true }]).conflicts, []);

async function saves() {
  let version = 0, content = "old", calls = 0;
  const doc = { changeGeneration: () => version, isClean: (g) => g === version, getValue: () => content };
  const queue = [], changes = [];
  const store = Tabs.createTabStore({
    saveFile: async (body) => { calls++; return calls === 1 ? { _status: 0, error: "offline" } : { _status: 200, mtime: 2 }; },
    schedule: (fn, ms) => { queue.push({ fn, ms }); return queue.length; }, cancel: () => {},
    onChange: (t, why) => changes.push(why),
  });
  const t = store.add("a.tex", doc, 1);
  version++; content = "new";
  assert.strictEqual((await store.save(t)).reason, "failed");
  assert.ok(t.saveError && queue.length === 1, "a failed save is visible and retried");
  content = "newest"; version++;
  await queue.shift().fn();
  assert.strictEqual(calls, 2);
  assert.strictEqual(store.dirty(t), false);
  assert.strictEqual(t.saveError, null);
  store.diskSeen(t, null);
  version++;
  assert.strictEqual((await store.save(t)).reason, "gone", "autosave cannot resurrect a deleted file");
  console.log("ok");
}
saves().catch((e) => { console.error(e); process.exitCode = 1; });
