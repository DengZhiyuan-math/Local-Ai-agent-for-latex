"use strict";
const assert = require("assert");
const Turn = require("../prism_local/static/turn.js");
for (const status of [0, 408, 429, 500, 502, 503]) assert.strictEqual(Turn.pollFailed(status, 50), "retry");
for (const status of [400, 403, 404, 409]) assert.strictEqual(Turn.pollFailed(status, 0), "lost");
assert.strictEqual(Turn.pollDelay(0), 1000);
assert.strictEqual(Turn.pollDelay(10), 30000);
assert.deepStrictEqual(Turn.result({ outcome: "stopped", exit: 1, stderr: "terminated" }), { outcome: "stopped", error: null });
assert.deepStrictEqual(Turn.result({ outcome: "failed", error: "precise failure" }), { outcome: "failed", error: "precise failure" });
assert.strictEqual(Turn.result({ outcome: "failed", error: null }).error, null, "an already reported error is not repeated");
assert.strictEqual(Turn.mayFollow({ started: 1, current: 1, focused: true, hidden: false }), true);
for (const state of [{ current: 2 }, { focused: false }, { hidden: true }])
  assert.strictEqual(Turn.mayFollow({ started: 1, current: 1, focused: true, hidden: false, ...state }), false);
let clock = 0;
const quiet = Turn.create({ now: () => clock });
quiet.feed([{ t: "tool", id: "one", name: "compile", summary: "a.tex" }]);
clock = 11000;
assert.ok(quiet.status.includes("last event 11s ago"));
console.log("ok");
