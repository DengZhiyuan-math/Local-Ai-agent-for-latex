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
  const race = Tabs.createTabStore({ saveFile: async () => ({ _status: 409, conflict: true, gone: true }) });
  const deleted = race.add("race.tex", doc, 1); version++;
  assert.strictEqual((await race.save(deleted)).reason, "gone");
  assert.strictEqual(deleted.conflict, "gone", "a deletion discovered by saving offers Save as");
}

async function staleTrees() {
  let version = 0, content = "a";
  const doc = { changeGeneration: () => version, isClean: (g) => g === version, getValue: () => content };
  let writes = 0;
  const store = Tabs.createTabStore({ saveFile: async () => ({ _status: 200, mtime: 1 + ++writes }) });
  const t = store.add("a.tex", doc, 1);
  // A tree asked for before a save finished arrives after it, while you type on.
  const asked = store.stamp();
  version++; content = "ab";
  assert.ok((await store.save(t)).ok);
  version++; content = "abc";
  const late = Watch.reconcile(tree(1), tree(1), [{ ...t, dirty: true }], asked);
  assert.deepStrictEqual([late.conflicts, late.reload, late.gone], [[], [], []], "a tree older than the save is no change on disk");
  assert.deepStrictEqual(Watch.reconcile(tree(1), tree(2), [{ ...t, dirty: true }], store.stamp()).conflicts, [],
    "the next tree sees the saved file");
  assert.deepStrictEqual(Watch.reconcile(tree(2), tree(3), [{ ...t, dirty: true }], store.stamp()).conflicts, ["a.tex"],
    "a later change on disk still is one");
  // A tab opened after the tree was asked for is not missing from it.
  const before = store.stamp();
  const fresh = store.add("new.tex", doc, 5);
  assert.deepStrictEqual(Watch.reconcile(tree(2), tree(2), [{ ...fresh, dirty: true }], before).gone, []);
  // A deleted file that comes back as it was (the Recycle Bin) can be saved again.
  store.diskSeen(t, null);
  assert.strictEqual(t.conflict, "gone");
  assert.deepStrictEqual(Watch.reconcile(tree(2), tree(2), [{ ...t, dirty: true }], store.stamp()).conflicts, ["a.tex"]);
  assert.strictEqual(store.diskSeen(t, 2), "same");
  assert.strictEqual(t.conflict, null);
  assert.ok((await store.save(t)).ok, "its edits are saved");
}

async function backoff() {
  let version = 1;
  const doc = { changeGeneration: () => version, isClean: (g) => g === version, getValue: () => "x" };
  const queue = [];
  const store = Tabs.createTabStore({ saveFile: async () => ({ _status: 0, error: "offline" }),
    schedule: (fn, ms) => { queue.push({ fn, ms }); return queue.length; }, cancel: () => {} });
  const t = store.add("a.tex", doc, 1);
  version++;
  await store.save(t);
  for (let i = 0; i < 7; i++) await queue[queue.length - 1].fn();
  assert.deepStrictEqual(queue.map((q) => q.ms), [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000],
    "retries back off, up to thirty seconds");
}

saves().then(staleTrees).then(backoff).then(() => console.log("ok")).catch((e) => { console.error(e); process.exitCode = 1; });
