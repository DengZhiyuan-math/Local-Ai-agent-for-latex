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
assert.strictEqual(Turn.mayFollow({ started: 1, current: 1, touched: false, hidden: false }), true, "an untouched turn is followed");
for (const state of [{ current: 2 }, { touched: true }, { hidden: true }])
  assert.strictEqual(Turn.mayFollow({ started: 1, current: 1, touched: false, hidden: false, ...state }), false);
// Context and deleted lines are not additions. A diff can contain several hunks,
// literal "+++" source text, and a final line without a newline.
assert.deepStrictEqual(Turn.changedLines({ diff: ["--- a/main.tex", "+++ b/main.tex", "@@ -2,4 +2,4 @@",
  " context", "-old", "+new", "+second", "-removed", " tail", "@@ -20 +20 @@", "-before", "+++literal"].join("\n") }),
[[2, 4], [19, 20]]);
assert.deepStrictEqual(Turn.changedLines({ diff: "@@ -2,2 +2 @@\n context\n-deleted\n" }), []);
assert.deepStrictEqual(Turn.changedLines({ diff: "@@ -0,0 +1,2 @@\n+one\n+two\n" }), [[0, 2]]);
assert.deepStrictEqual(Turn.changedLines({ diff: "Binary file changed" }), []);
assert.deepStrictEqual(Turn.changedLines({ lines: [[0, 1], [8000, 8002]], diff: "display truncated" }), [[0, 1], [8000, 8002]]);
assert.deepStrictEqual(Turn.changedLines({ lines: [], diff: "@@ -1 +1 @@\n-old\n+new\n" }), []);
let clock = 0;
const quiet = Turn.create({ now: () => clock });
quiet.feed([{ t: "tool", id: "one", name: "compile", summary: "a.tex" }]);
clock = 11000;
assert.ok(quiet.status.includes("last event 11s ago"));
console.log("ok");
