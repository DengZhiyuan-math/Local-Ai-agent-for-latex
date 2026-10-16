"use strict";
const assert = require("assert"), Util = require("../prism_local/static/util.js");
const values = new Map([["prism.chat.old.p.log", '"old"'], ["prism.chat.recent.p.log", '"recent"'], ["prism.chat.paper.p.log", '"current"']]);
let pressure = false, clock = 1;
const removed = [];
const storage = { get length() { return values.size; }, key: (i) => [...values.keys()][i], getItem: (k) => values.get(k) ?? null,
  setItem: (k, v) => { if (pressure && values.has("prism.chat.old.p.log")) { const e = Error("full"); e.name = "QuotaExceededError"; throw e; } values.set(k, v); },
  removeItem: (k) => { removed.push(k); values.delete(k); } };
const store = Util.createStore(() => storage, { project: () => "paper", now: () => clock++ });
store.get("chat.recent.p.log", null); pressure = true;
assert.strictEqual(store.set("theme", "dark"), true);
assert.deepStrictEqual(removed, ["prism.chat.old.p.log"]);
assert.ok(values.has("prism.chat.paper.p.log"));
let warned = 0;
const blocked = Util.createStore(() => { throw Error("blocked"); }, { onError: () => warned++ });
assert.strictEqual(blocked.set("theme", "light"), false);
assert.strictEqual(warned, 1);
assert.strictEqual(Util.boundedLog(["earlier", "x".repeat(500000), "latest"]), "earlierlatest");
console.log("ok");
