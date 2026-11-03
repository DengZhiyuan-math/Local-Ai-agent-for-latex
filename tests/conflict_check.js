"use strict";
const assert = require("assert"), fs = require("fs"), vm = require("vm");
const source = fs.readFileSync(require.resolve("../prism_local/static/app.js"), "utf8");
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

async function compareUnsavedConflict() {
  const elements = {}, requests = [];
  const tab = { path: "main.tex", conflict: "disk", doc: { getValue: () => "manual draft\n" } };
  const ctx = {
    $: id => elements[id] ||= {}, activeTab: () => tab, C: {}, S: {},
    esc: require("../prism_local/static/util.js").escapeHtml,
    diffHtml: require("../prism_local/static/util.js").diffHtml,
    ago: () => "just now", openPanel() {},
    Q: {
      diff: async () => { requests.push("git"); return { _status: 200, repo: true, diff: "", last: {
        at: 1, message: "Agent turn", hash: "abc123", diff: "-original\n+agent result\n" } }; },
      compareFile: async (path, content) => { requests.push([path, content]); return {
        _status: 200, diff: "--- main.tex (disk)\n+++ main.tex (my unsaved edits)\n@@ -1 +1 @@\n-agent result\n+manual draft\n" }; },
    },
  };
  vm.runInNewContext(section("function showBanner(", "\n/* ------------------------------------------------------------------ file tree")
    + section("let diffSeq =", "\n/* ------------------------------------------------------------------ PDF") + ";showBanner", ctx)(tab);
  await elements["#bn-diff"].onclick();
  assert.ok(elements["#diff"].innerHTML.includes("manual draft"),
    "the conflict comparison must include the unsaved manual draft, even after the Agent change was committed");
  assert.ok(elements["#diff"].innerHTML.includes("agent result"));
  assert.deepStrictEqual(requests, [["main.tex", "manual draft\n"]], "compare the buffer with disk rather than Git history");
}

async function comparisonOrdering() {
  let release, draft = "manual draft\n";
  const elements = {}, tab = { path: "main.tex", doc: { getValue: () => draft } };
  const ctx = { $: id => elements[id] ||= {}, S: {}, openPanel() {},
    diffHtml: require("../prism_local/static/util.js").diffHtml,
    Q: { compareFile: () => new Promise(r => { release = r; }),
      diff: async () => ({ _status: 200, repo: true, diff: "Git comparison requested later" }) } };
  const run = vm.runInNewContext(section("let diffSeq =", "\n/* ------------------------------------------------------------------ PDF")
    + ";({showConflictDiff, showDiff})", ctx);
  const old = run.showConflictDiff(tab);
  await run.showDiff("main.tex");
  release({ _status: 200, diff: "+old manual draft" });
  await old;
  assert.strictEqual(elements["#diff"].innerHTML, "Git comparison requested later", "a late conflict comparison cannot replace a newer view");
  const changed = run.showConflictDiff(tab);
  draft += "Typed during comparison.\n";
  release({ _status: 200, diff: "+old manual draft" });
  await changed;
  assert.ok(elements["#diff"].innerHTML.includes("Your draft changed"), "a comparison of an old draft must say so");
  const failed = run.showConflictDiff(tab);
  release({ _status: 404, error: "file <missing>" });
  await failed;
  assert.ok(elements["#diff"].innerHTML.includes("file &lt;missing&gt;"), "comparison errors are escaped and visible");
}

compareUnsavedConflict().then(comparisonOrdering).then(() => console.log("ok")).catch(e => { console.error(e); process.exitCode = 1; });
