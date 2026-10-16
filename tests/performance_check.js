"use strict";
const assert = require("assert"), fs = require("fs"), vm = require("vm");
const source = fs.readFileSync(require.resolve("../prism_local/static/app.js"), "utf8");
let rebuilds = 0, toggles = 0, change;
const tab = { path: "a.tex", saveError: null, _tabState: JSON.stringify([false, null]) };
const el = { dataset: { path: tab.path }, classList: { toggle: () => toggles++ } };
const start = source.indexOf("function updateTabState(");
const update = start < 0 ? "" : source.slice(start, source.indexOf("\nfunction renderTabs()", start));
const handler = source.indexOf('cm.on("change",');
const line = source.slice(handler, source.indexOf("\n});", handler) + 4);
vm.runInNewContext(update + "\n" + line, { cm: { on: (_, fn) => { change = fn; } }, activeTab: () => tab,
  renderTabs: () => rebuilds++, scheduleSave: () => {}, stopFollowing: () => {}, isDirty: () => true,
  $: () => ({ children: [el] }) });
for (let i = 0; i < 1000; i++) change(null, { origin: "+input" });
assert.strictEqual(rebuilds, 0, "typing must preserve the tab DOM");
assert.ok(toggles <= 2, "dirty state changes only once");
const MathText = require("../prism_local/static/mathtext.js");
let formulas = 0;
const katex = { renderToString: () => { formulas++; return "<span>formula</span>"; } };
const render = (macros) => MathText.render("$x$ and $x$", { markdown: s => s, katex, macros });
render(); render();
assert.strictEqual(formulas, 1, "the same formula is typeset once across streamed redraws");
render({ "\\R": "R" });
assert.strictEqual(formulas, 2, "a changed project macro invalidates the formula");
const Util = require("../prism_local/static/util.js"), frames = [], drawn = [];
const batch = Util.frameBatch((key, value) => drawn.push([key, value]), { schedule: fn => { frames.push(fn); return frames.length; }, cancel: () => {} });
for (let i = 0; i < 1000; i++) batch.set("reply", i);
assert.strictEqual(frames.length, 1);
frames.shift()();
assert.deepStrictEqual(drawn, [["reply", 999]]);
const elements = new Map();
const dollar = (key) => {
  if (!elements.has(key)) elements.set(key, { _html: "", writes: 0, removeAttribute() {},
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v.replace(/&#39;/g, "'"); this.writes++; } });
  return elements.get(key);
};
const syncStart = source.indexOf("function htmlIfChanged(") >= 0 ? source.indexOf("function htmlIfChanged(") : source.indexOf("function renderSync(");
const sync = vm.runInNewContext(source.slice(syncStart, source.indexOf("\nfunction syncMenu", syncStart)) + ";renderSync", {
  $: dollar, S: {}, SYNC_TOGGLE: { value: Boolean }, SYNC_NOTICE: {}, document: { querySelectorAll: () => [] },
  esc: Util.escapeHtml, ago: () => "now", toast: () => {}, SyncState: require("../prism_local/static/syncstate.js") });
const state = { own: true, remote: true, enabled: true, kept: [{ path: "a'b.tex", line: 1, who: "A" }], clash: [] };
sync(state); sync(state);
assert.strictEqual(dollar("#sync-kept").writes, 1, "unchanged sync menus preserve their buttons");
const pdfSource = fs.readFileSync(require.resolve("../prism_local/static/pdfview.js"), "utf8");
let offsets = 0, globalClears = 0, pageClears = 0;
const label = {}, pdf = vm.runInNewContext(pdfSource + ";PV", { pdfjsLib: { GlobalWorkerOptions: {} },
  $: (key) => key === "#pdf-scroll" ? { scrollTop: 74250, clientHeight: 300 } : label,
  document: { querySelectorAll: () => { globalClears++; return []; } } });
pdf.views = Array.from({ length: 1000 }, (_, i) => ({ div: { get offsetTop() { offsets++; return i * 100; }, dataset: { page: i + 1 } } }));
pdf.updatePageLabel();
assert.strictEqual(label.textContent, "744 / 1000");
assert.ok(offsets <= 12, "page labels use logarithmic offset reads");
pdf.drawHits({ div: { querySelectorAll: () => { pageClears++; return []; } } }).then(() => {
  assert.strictEqual(globalClears, 0, "one rendered page cannot erase every page's hits");
  assert.ok(pageClears > 0); console.log("ok");
}).catch(e => { console.error(e); process.exitCode = 1; });
