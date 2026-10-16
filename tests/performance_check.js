"use strict";
const assert = require("assert"), fs = require("fs"), vm = require("vm");
const source = fs.readFileSync(require.resolve("../prism_local/static/app.js"), "utf8");
let rebuilds = 0, toggles = 0, change;
const tab = { path: "a.tex", saveError: null, _tabState: JSON.stringify([false, null]) };
const el = { dataset: { path: tab.path }, classList: { toggle: () => toggles++ } };
const start = source.indexOf("function updateTabState(");
const update = start < 0 ? "" : source.slice(start, source.indexOf("\nfunction renderTabs()", start));
const line = source.slice(source.indexOf('cm.on("change",'), source.indexOf("\n", source.indexOf('cm.on("change",')));
vm.runInNewContext(update + "\n" + line, { cm: { on: (_, fn) => { change = fn; } }, activeTab: () => tab,
  renderTabs: () => rebuilds++, scheduleSave: () => {}, isDirty: () => true, $: () => ({ children: [el] }) });
for (let i = 0; i < 1000; i++) change();
assert.strictEqual(rebuilds, 0, "typing must preserve the tab DOM");
assert.ok(toggles <= 2, "dirty state changes only once");
console.log("ok");
