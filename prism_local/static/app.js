/* prism-local — editor client. Talks only to prism_local/server.py.
   Loaded after common.js (helpers, theme) and pdfview.js (PV). */
"use strict";

/* ------------------------------------------------------------------ state */
const S = {
  files: [], folders: [], order: [], tabs: [], active: null, symbols: { labels: [], bibkeys: [], outline: [], macros: [] },
  ready: false, restoring: true, watch: null,
  diagnostics: [], pdfMtime: null, pdfMain: null, building: false,
};
// The open files and how each is saved (tabs.js); S.tabs is its list.
const TABS = Tabs.createTabStore({
  saveFile: Q.saveFile, isStopped: () => foreignProject, isHeld: () => C.editTurn, notify: (m) => toast(m),
  onChange: (t, what) => {
    updateTabState(t); showBanner(t);
    if (what === "saved") scheduleSymbols();
    if (what === "back") scheduleSave(t);              // its file is there again: your edits go in
  },
});
S.tabs = TABS.tabs;

/* ------------------------------------------------------------------ home */
// The Home page (hub.py) lists all projects. Its tab calls itself "prism-home", so this
// finds and reuses it when it opened us; the server starts the Home page if needed.
$("#btn-home").onclick = async () => {
  const w = window.open("", "prism-home");
  let fresh = true;
  try { fresh = !!w && w.location.href === "about:blank"; } catch { fresh = false; }
  if (w && fresh) w.document.body.innerHTML = '<p style="font:15px system-ui;color:#6f6a60;margin:40vh auto;text-align:center">Opening your projects…</p>';
  const r = await Q.home();
  if (r.url) {
    // An existing Home tab only needs to load again if its server had to be restarted.
    if (!w) location.href = r.url;
    else { if (fresh || r.started) w.location.href = r.url; w.focus(); }
  } else {
    if (w && fresh) w.close();
    alert("Could not open the Home page: " + (r.error || "unknown error") + (r.log ? "\n\n" + r.log : ""));
  }
};

/* ------------------------------------------------------------------ editor */
const cm = CodeMirror($("#editor"), {
  lineNumbers: true, lineWrapping: true, matchBrackets: true, autoCloseBrackets: "()[]{}$$",
  styleActiveLine: true, indentUnit: 2, tabSize: 2, indentWithTabs: false,
  // Fold sections, environments and \[ … \] from the gutter (texfold.js).
  foldGutter: true, gutters: ["CodeMirror-linenumbers", "CodeMirror-foldgutter"],
  foldOptions: { widget: "…", minFoldSize: 1 },
  extraKeys: {
    "Ctrl-Q": (ed) => ed.foldCode(ed.getCursor()),
    "Ctrl-K Ctrl-0": "foldAll", "Cmd-K Cmd-0": "foldAll",
    "Ctrl-K Ctrl-J": "unfoldAll", "Cmd-K Cmd-J": "unfoldAll",
    "Cmd-S": () => saveActive(), "Ctrl-S": () => saveActive(),
    "Cmd-Enter": () => compile(), "Ctrl-Enter": () => compile(),
    "Cmd-J": () => forwardSync(), "Ctrl-J": () => forwardSync(),
    "Ctrl-Space": (ed) => showCompletions(ed, true),
    "Cmd-/": "toggleTexComment", "Ctrl-/": "toggleTexComment",
    // Find and replace (below).
    "Cmd-F": () => findOpen(false), "Ctrl-F": () => findOpen(false),
    "Cmd-Alt-F": () => findOpen(true), "Ctrl-H": () => findOpen(true),
    F3: () => findStep(1), "Shift-F3": () => findStep(-1),
    // Snippets first (see below): expand, next field, out of a bracket, & in a matrix row.
    Tab: () => SNIP.tab(indentOrSpaces),
    "Shift-Tab": () => SNIP.shiftTab(),
    Enter: () => SNIP.enter(),
    "Shift-Enter": "newlineAndIndent",
    // A snippet's fields first, then the find bar.
    Esc: () => { const r = SNIP.escape(); if (r === CodeMirror.Pass && !findBar.hidden) return findClose(); return r; },
  },
});
function indentOrSpaces(ed) { return ed.somethingSelected() ? ed.indentSelection("add") : ed.replaceSelection("  "); }
CodeMirror.commands.toggleTexComment = (ed) => {
  const from = ed.getCursor("from").line, to = ed.getCursor("to").line;
  const lines = []; for (let l = from; l <= to; l++) lines.push(ed.getLine(l));
  const allC = lines.every((t) => /^\s*%/.test(t) || !t.trim());
  ed.operation(() => {
    for (let l = from; l <= to; l++) {
      const t = ed.getLine(l);
      if (allC) { const m = t.match(/^(\s*)% ?/); if (m) ed.replaceRange(m[1], { line: l, ch: 0 }, { line: l, ch: m[0].length }); }
      else if (t.trim()) ed.replaceRange("% ", { line: l, ch: t.match(/^\s*/)[0].length });
    }
  });
};
/* ------------------------------------------------------------------ find and replace */
// The bar over the open file (findlogic.js decides what matches): every match is marked
// and counted, the current one selected. Replace changes the selected match and goes to the
// next; Replace all changes every one in one step, which one ⌘Z takes back.
const FIND_MARKS = 2000;               // matches marked in the text (all of them are counted)
const findBar = $("#find"), findQ = $("#find-q"), findR = $("#find-r");
const FIND = {
  opts: { caseSensitive: false, wholeWord: false, regex: false, ...store.get("find.opts", {}) },
  list: [], more: false, error: null, cur: -1, marks: [], curMark: null, timer: null,
};
const findOpts = () => ({ ...FIND.opts, query: findQ.value });
const regexEscape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
function findOpen(replace) {
  if (!activeTab()) return;
  const sel = cm.hasFocus() ? cm.getSelection() : "";     // a match selected by the bar is not new
  if (sel && !sel.includes("\n") && sel.length < 200) findQ.value = FIND.opts.regex ? regexEscape(sel) : sel;
  findBar.hidden = false;
  if (replace) findShowReplace(true);
  const input = replace && findQ.value ? findR : findQ;
  input.focus(); input.select();
  findRun(true);
}
function findClose() {
  findBar.hidden = true;
  findUnmark();
  FIND.list = []; FIND.cur = -1;
  cm.focus();
}
function findShowReplace(on) {
  $("#find-replace").hidden = !on;
  $("#find-more").setAttribute("aria-expanded", String(on));
}
function findUnmark() {
  for (const m of FIND.marks) m.clear();
  if (FIND.curMark) FIND.curMark.clear();
  FIND.marks = []; FIND.curMark = null;
}
// Finds again in the open file. select: go to the first match from the selection on (you
// typed in the bar); otherwise the current match is the one the cursor is in, if any.
function findRun(select) {
  clearTimeout(FIND.timer);
  if (findBar.hidden || !activeTab()) return;
  const r = FindLogic.matches(cm.getValue(), findOpts());
  Object.assign(FIND, { list: r.list, more: r.more, error: r.error || null });
  findUnmark();
  cm.operation(() => {
    for (const x of r.list.slice(0, FIND_MARKS))
      FIND.marks.push(cm.markText(cm.posFromIndex(x.from), cm.posFromIndex(x.to), { className: "cm-find-hit" }));
  });
  const from = cm.indexFromPos(cm.getCursor("from")), to = cm.indexFromPos(cm.getCursor("to"));
  FIND.cur = select ? r.list.findIndex((x) => x.from >= from) : r.list.findIndex((x) => x.from <= from && to <= x.to);
  if (select && FIND.cur < 0 && r.list.length) FIND.cur = 0;                 // from the top again
  if (select && FIND.cur >= 0) findSelect(); else findShow();
}
// Selects the current match and shows it.
function findSelect() {
  const x = FIND.list[FIND.cur];
  if (!x) return findShow();
  const from = cm.posFromIndex(x.from), to = cm.posFromIndex(x.to);
  cm.setSelection(from, to);
  cm.scrollIntoView({ from, to }, 80);
  findShow();
}
function findShow() {
  if (FIND.curMark) { FIND.curMark.clear(); FIND.curMark = null; }
  const x = FIND.list[FIND.cur];
  if (x) FIND.curMark = cm.markText(cm.posFromIndex(x.from), cm.posFromIndex(x.to), { className: "cm-find-cur" });
  const n = FIND.list.length, count = $("#find-count");
  count.textContent = FIND.error || (!findQ.value ? "" : !n ? "No results"
    : (FIND.cur >= 0 ? `${FIND.cur + 1} of ` : "") + n.toLocaleString() + (FIND.more ? "+" : ""));
  count.classList.toggle("bad", !!FIND.error);
  findQ.classList.toggle("bad", !!FIND.error);
}
// The next (d = 1) or previous (-1) match; from the cursor when none is current.
function findStep(d) {
  if (findBar.hidden) return findOpen(false);
  findRun(false);
  const n = FIND.list.length;
  if (!n) return;
  if (FIND.cur >= 0) FIND.cur = (FIND.cur + d + n) % n;
  else {
    const at = cm.indexFromPos(cm.getCursor(d > 0 ? "to" : "from"));
    FIND.cur = d > 0 ? FIND.list.findIndex((x) => x.from >= at) : FIND.list.findLastIndex((x) => x.to <= at);
    if (FIND.cur < 0) FIND.cur = d > 0 ? 0 : n - 1;
  }
  findSelect();
}
function findReplaceOne() {
  findRun(false);
  const x = FIND.list[FIND.cur];
  const at = cm.indexFromPos(cm.getCursor("from"));
  if (!x || at !== x.from || cm.indexFromPos(cm.getCursor("to")) !== x.to) {
    // No match selected yet: select the one the cursor is in, or the next, and replace it on the next click.
    FIND.cur = FIND.list.findIndex((y) => y.to > at);
    if (FIND.cur < 0 && FIND.list.length) FIND.cur = 0;
    return findSelect();
  }
  const text = FindLogic.expand(findR.value, x.m, findOpts());
  cm.replaceRange(text, cm.posFromIndex(x.from), cm.posFromIndex(x.to), "+find");
  findRun(false);
  const after = x.from + text.length;
  FIND.cur = FIND.list.findIndex((y) => y.from >= after);
  if (FIND.cur < 0 && FIND.list.length) FIND.cur = 0;
  findSelect();
}
function findReplaceAll() {
  const opts = findOpts(), { list } = FindLogic.matches(cm.getValue(), opts, Infinity);
  if (!list.length) return;
  // From the last to the first, so each match's place is still where it was found.
  cm.operation(() => {
    for (let i = list.length - 1; i >= 0; i--) {
      const x = list[i];
      cm.replaceRange(FindLogic.expand(findR.value, x.m, opts), cm.posFromIndex(x.from), cm.posFromIndex(x.to), "+find");
    }
  });
  toast(`Replaced ${Util.plural(list.length, "match", "matches")}.`);
  findRun(false);
}
function findToggle(opt) {
  FIND.opts[opt] = !FIND.opts[opt];
  store.set("find.opts", FIND.opts);
  findOptsShown();
  findRun(true);
}
function findOptsShown() {
  for (const b of findBar.querySelectorAll("[data-opt]")) b.setAttribute("aria-pressed", String(!!FIND.opts[b.dataset.opt]));
}
findOptsShown();
// The bar's own keys. Alt+C/W/R by the key's place: on a Mac, Option+C types "ç".
const FIND_OPT_KEYS = { KeyC: "caseSensitive", KeyW: "wholeWord", KeyR: "regex" };
function findKeys(e) {
  if (e.key === "Escape") { e.preventDefault(); findClose(); }
  else if (e.altKey && !e.ctrlKey && !e.metaKey && FIND_OPT_KEYS[e.code]) { e.preventDefault(); findToggle(FIND_OPT_KEYS[e.code]); }
  else if (e.key === "F3") { e.preventDefault(); findStep(e.shiftKey ? -1 : 1); }
  else return false;
  return true;
}
findQ.addEventListener("input", () => { clearTimeout(FIND.timer); FIND.timer = setTimeout(() => findRun(true), 120); });
findQ.addEventListener("keydown", (e) => {
  if (findKeys(e)) return;
  if (e.key === "Enter") { e.preventDefault(); clearTimeout(FIND.timer); findStep(e.shiftKey ? -1 : 1); }
});
findR.addEventListener("keydown", (e) => {
  if (findKeys(e)) return;
  if (e.key === "Enter" && e.altKey && (e.ctrlKey || e.metaKey)) { e.preventDefault(); findReplaceAll(); }
  else if (e.key === "Enter") { e.preventDefault(); findReplaceOne(); }
});
findBar.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.opt) findToggle(b.dataset.opt);
  else if (b.dataset.step) findStep(+b.dataset.step);
  else if (b.dataset.close) findClose();
});
$("#find-more").onclick = () => { const on = $("#find-replace").hidden; findShowReplace(on); (on ? findR : findQ).focus(); };
$("#find-one").onclick = () => findReplaceOne();
$("#find-all").onclick = () => findReplaceAll();
$("#btn-find").onclick = () => findOpen(false);
// Your edits, an agent's, a reload from disk: the matches are found again a moment later.
cm.on("changes", () => { if (!findBar.hidden) { clearTimeout(FIND.timer); FIND.timer = setTimeout(() => findRun(false), 150); } });
/* ------------------------------------------------------------------ snippets */
// Shortcuts that expand as you type, after obsidian-latex-suite (snippets.js). Yours, and how
// they expand, are set in Home → Settings (~/.prism-local/snippets.js and settings.json), so
// they are read again whenever you come back to this page.
const SNIP_SET = { on: true, fraction: true, matrix: true };
const SNIP = Snippets.attach(cm, {
  enabled: () => SNIP_SET.on, fraction: () => SNIP_SET.fraction, matrix: () => SNIP_SET.matrix,
});
let snipContent;                 // the file as last read: unchanged, it is not compiled again
async function loadSnippets() {
  const r = await Q.snippets();
  if (r._status !== 200) return;
  for (const k of Object.keys(SNIP_SET)) SNIP_SET[k] = r[k] !== false;
  if (r.content === snipContent) return;
  snipContent = r.content;
  try {
    const u = Snippets.load(r.content);
    SNIP.setUser(u.list, u.defaults);
  } catch (e) {                  // Home → Settings says what is wrong; the built-in ones work
    SNIP.setUser([], true);
    console.warn("snippets file:", e.message || e);
  }
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") loadSnippets(); });
window.addEventListener("focus", () => loadSnippets());

cm.getWrapperElement().style.display = "none";
cm.on("change", (ed, ch) => {
  const t = activeTab(); if (t) { updateTabState(t); scheduleSave(t); }
  if (ch.origin !== "setValue") stopFollowing();      // you typed (a reload from disk does not count)
});
cm.on("inputRead", (ed, ch) => { if (/[{,]/.test(ch.text.join("")) || /\\[A-Za-z]*$/.test(lineBefore(ed))) showCompletions(ed, false); });

function modeFor(path) { return path.endsWith(".md") ? "markdown" : "prism-stex"; }
function activeTab() { return S.tabs.find((t) => t.path === S.active) || null; }
const isDirty = (t) => TABS.dirty(t);

// The file of a tab being opened, asked for once: two quick clicks share the request (and
// get one tab), and the files of the last session download together (see init).
const opening = new Map();       // path -> the /api/file request
function fileRequest(path) {
  if (!opening.has(path)) opening.set(path, Q.file(path));
  return opening.get(path);
}
// The tab of `path`, its file fetched if it is not open yet; null if it cannot be had.
async function loadTab(path) {
  let t = S.tabs.find((x) => x.path === path);
  if (t) return t;
  const r = await fileRequest(path);
  opening.delete(path);
  t = S.tabs.find((x) => x.path === path);            // opened meanwhile, by the same request
  if (t) return t;
  if (r._status !== 200) { toast(`Cannot open ${path}: ${r.error || r._status}`); return null; }
  return TABS.add(path, CodeMirror.Doc(r.content, modeFor(path)), r.mtime);
}
// Each file asked for to be shown counts: the last one asked for is the one in front, even
// if an earlier one's file arrives after it (see init: the tabs of the last session).
let openAsked = 0;
async function openFile(path, line, following = false) {
  if (foreignProject) return;
  if (C.job && !following) { C.follow = false; $("#agent-view").hidden = true; }
  if (S.files.some((f) => f.path === path && f.editable === false)) {
    window.open(Client.assetUrl(path, projectKey), "_blank", "noopener");
    return;
  }
  const asked = ++openAsked;
  const t = await loadTab(path);
  if (asked !== openAsked) { renderTabs(); return; }   // another file asked for since: it goes in front
  if (!t) return;
  S.active = path;
  cm.swapDoc(t.doc);
  AH.apply(path);
  findRun(false);                    // the find bar, open, counts in this file
  cm.getWrapperElement().style.display = "";
  $("#empty-editor").hidden = true;
  applyDiagnostics();
  renderTabs(); renderTree(); showBanner(t);
  persistSession();
  if (line) jumpToLine(line);
  if (!$("#history").hidden && !$("#panel").classList.contains("collapsed")) showHistory(path);   // follows the file
  cm.focus();
  cm.refresh();
}

// The last tab closed: no file in the editor, nor a find bar over it.
function showNoFile() {
  S.active = null; cm.getWrapperElement().style.display = "none"; $("#empty-editor").hidden = false;
  if (!findBar.hidden) { findBar.hidden = true; findUnmark(); }
}

function jumpToLine(line) {
  const l = Math.max(0, Math.min(line - 1, cm.lineCount() - 1));
  cm.setCursor({ line: l, ch: 0 });
  const top = cm.charCoords({ line: l, ch: 0 }, "local").top;
  cm.scrollTo(null, top - cm.getScrollInfo().clientHeight / 3);
  const h = cm.addLineClass(l, "background", "cm-line-flash");
  setTimeout(() => cm.removeLineClass(h, "background", "cm-line-flash"), 1400);
}

async function closeTab(path) {
  const t = S.tabs.find((x) => x.path === path);
  // Held for the turn, saved at its end (not a deleted or conflicting file: that is never saved by itself).
  if (t && isDirty(t) && C.editTurn && !t.conflict) return toast(Tabs.unsavedWhy({ reason: "held" }) + " Keep this tab open until then.");
  if (t && isDirty(t) && !t.conflict) await TABS.save(t);      // autosave may still be pending
  if (t && isDirty(t) && !confirm(`${path} has unsaved changes. Close anyway?`)) return;
  TABS.remove((x) => x.path === path);
  if (S.active === path) {
    const next = S.tabs[S.tabs.length - 1];
    if (next) return openFile(next.path);
    showNoFile();
  }
  renderTabs(); renderTree(); persistSession();
}

function updateTabState(t) {
  const state = JSON.stringify([isDirty(t), t.saveError]);
  if (state === t._tabState) return;
  t._tabState = state;
  const el = [...$("#tabs").children].find((el) => el.dataset.path === t.path);
  if (!el) return;
  el.classList.toggle("dirty", isDirty(t)); el.classList.toggle("save-failed", !!t.saveError);
  el.title = t.saveError || t.path;
}
function renderTabs() {
  for (const t of S.tabs) t._tabState = JSON.stringify([isDirty(t), t.saveError]);
  $("#tabs").innerHTML = S.tabs.map((t) => `
    <div class="tab ${t.path === S.active ? "active" : ""} ${isDirty(t) ? "dirty" : ""} ${t.saveError ? "save-failed" : ""}" data-path="${esc(t.path)}" title="${esc(t.saveError || t.path)}">
      <span class="name">${esc(t.path.split("/").pop())}</span><span class="close" data-close="${esc(t.path)}">×</span>
    </div>`).join("");
  AH.refresh();
}
$("#tabs").addEventListener("click", (e) => {
  const c = e.target.closest("[data-close]"); if (c) { e.stopPropagation(); return closeTab(c.dataset.close); }
  const t = e.target.closest(".tab"); if (t) openFile(t.dataset.path);
});
$("#tabs").addEventListener("auxclick", (e) => { const t = e.target.closest(".tab"); if (t && e.button === 1) closeTab(t.dataset.path); });

function persistSession() { if (!S.ready || S.restoring || foreignProject) return; store.set("session", { tabs: S.tabs.map((t) => t.path), active: S.active }); }

/* ------------------------------------------------------------------ save & external changes */
// Saving: one at a time per tab, held during an agent's Edit turn, busy git waited out,
// a file changed on disk never overwritten (tabs.js).
async function saveActive() {
  const t = activeTab(); if (!t) return;
  clearTimeout(t.saveTimer);
  const saved = await TABS.save(t);
  if (!saved.ok) return toast(Tabs.unsavedWhy(saved));
  if (saved.ok && $("#auto-compile").checked) compile();
}

/* Autosave, as in Overleaf: an edit is saved a moment after you stop typing, and every
   open file is saved when you switch away. A file that changed on disk meanwhile is not
   overwritten: the save stops with the conflict banner, and autosave waits until the
   banner is resolved. */
const AUTOSAVE_MS = 800, AUTOCOMPILE_MS = 1500;
function scheduleSave(t) {
  clearTimeout(t.saveTimer);
  t.saveTimer = setTimeout(() => autosave(t), AUTOSAVE_MS);
}
async function autosave(t) {
  if (!S.tabs.includes(t) || t.conflict || !isDirty(t)) return;
  if ((await TABS.save(t)).ok && $("#auto-compile").checked) scheduleCompile();
}
function flushSaves() {
  for (const t of S.tabs) { clearTimeout(t.saveTimer); autosave(t); }
}
window.addEventListener("blur", flushSaves);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flushSaves(); });

// Auto-compile: shortly after the last edit is saved; if a build is running, once it ends.
let compileTimer = null;
// active: the file whose document to build; null lets the server choose (after an agent turn).
function scheduleCompile(active = undefined) {
  clearTimeout(compileTimer);
  compileTimer = setTimeout(function go() {
    if (S.building) { compileTimer = setTimeout(go, 1000); return; }
    compile(false, active);
  }, AUTOCOMPILE_MS);
}
// Every edited tab: {ok}, or {ok: false, reason} (Tabs.unsavedWhy says it).

// `discard`: the author chose the disk version over unsaved edits (the conflict banner).
async function reloadFromDisk(t, discard = false) {
  const r = await Q.file(t.path);
  if (r._status !== 200) return;
  const what = TABS.loaded(t, r.content, discard);   // typed into while it was fetched: "kept"
  if (what === "kept") return;
  if (what === "replace") {                          // else the same text: keep cursor and undo
    const cur = t.doc.getCursor(), scroll = t === activeTab() ? cm.getScrollInfo() : null;
    t.doc.setValue(r.content);
    AH.apply(t.path);                                // setValue drops line classes
    t.doc.setCursor(cur);
    if (scroll) cm.scrollTo(scroll.left, scroll.top);
  }
  TABS.synced(t, r.mtime);
  if (t === activeTab()) applyDiagnostics();
}

function showBanner(t) {
  const b = $("#banner");
  if (!t || t !== activeTab() || !t.conflict) { b.hidden = true; return; }
  b.hidden = false;
  if (t.conflict === "gone") {
    b.innerHTML = `<b>${esc(t.path)}</b> was deleted on disk. <button id="bn-save-as">Save under a new name</button> <button id="bn-close">Close tab</button>`;
    $("#bn-close").onclick = () => closeTab(t.path);
    $("#bn-save-as").onclick = async () => {
      if (C.editTurn) return toast(Tabs.unsavedWhy({ reason: "held" }));
      const path = prompt("Save as a new project file", t.path);
      if (!path || path === t.path) return;
      const r = await Q.saveFile({ path, content: t.doc.getValue(), base_mtime: 0 });
      if (r._status !== 200) return toast(r.error || "Could not save the new file", true);
      TABS.remove((x) => x === t); S.active = null;
      await poll(); await openFile(path);
    };
    return;
  }
  b.innerHTML = `<b>${esc(t.path)}</b> was changed on disk (e.g. by Claude Code) while you have unsaved edits.
    <button id="bn-reload">Load disk version (discard mine)</button>
    <button id="bn-keep">Keep mine (overwrite disk)</button>
    <button id="bn-diff">Show git diff</button>`;
  $("#bn-reload").onclick = () => reloadFromDisk(t, true);
  $("#bn-keep").onclick = () => TABS.save(t, true);
  $("#bn-diff").onclick = () => showDiff(t.path);
}

/* ------------------------------------------------------------------ file tree & outline */
function renderTree() {
  if ($("#tree input")) return;        // a name is being typed (askName draws it afterwards)
  const groups = new Map();
  const rank = (p) => { const i = S.order.indexOf(p); return i < 0 ? 999 : i; };
  const files = [...S.files].sort((a, b) => rank(a.path) - rank(b.path) || a.path.localeCompare(b.path));
  for (const d of S.folders) groups.set(d, []);          // empty folders are shown too
  for (const f of files) {
    const dir = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/")) : "";
    if (!groups.has(dir)) groups.set(dir, []);
    groups.get(dir).push(f);
  }
  const dirOrder = ["", "tex", "tex/sections", "bib", "notes"];
  const dirs = [...groups.keys()].sort((a, b) => {
    const ia = dirOrder.indexOf(a), ib = dirOrder.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
  let html = "";
  for (const d of dirs) {
    if (d) html += `<li class="folder" data-dir="${esc(d)}" title="${esc(d)}">${esc(d)}/</li>`;
    for (const f of groups.get(d)) {
      html += `<li data-path="${esc(f.path)}" class="${f.path === S.active ? "active" : ""}" title="${esc(f.path)}">
        ${esc(f.path.split("/").pop())}${f.git ? `<span class="git" title="git status">${esc(f.git)}</span>` : ""}</li>`;
    }
  }
  $("#tree").innerHTML = html;
  AH.refresh();
}
$("#tree").addEventListener("click", (e) => { const li = e.target.closest("li[data-path]"); if (li && !li.querySelector("input")) openFile(li.dataset.path); });

/* The Files sidebar's menu (right-click), as Explorer or Finder offers it. Copy and Paste
   use the system's clipboard, so files go to and come from Explorer, Finder and other
   projects; where the system has none (Linux), a copy stays in this page, for this project. */
const parentDir = (p) => p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "";
const within = (p, root) => p === root || p.startsWith(root + "/");
let fileClip = null;              // paths copied here, where the system has no file clipboard

function fileMenu(target) {      // target: {path, dir}, or null for the project itself
  const folder = !target ? "" : target.dir ? target.path : parentDir(target.path);
  const fresh = [["New file…", () => askNew(folder, "file")], ["New folder…", () => askNew(folder, "folder")]];
  if (!target) return [...fresh, "-", ["Paste", () => pasteInto("")], "-",
    [`Open project folder in ${FILE_MANAGER}`, () => fileAction("reveal", { path: "" })]];
  return [
    ...(target.dir ? fresh : [["Open", () => openFile(target.path)]]),
    "-",
    [`Show in ${FILE_MANAGER}`, () => fileAction("reveal", { path: target.path })],
    "-",
    ["Copy", () => copyItems([target.path])],
    ["Paste", () => pasteInto(folder)],
    "-",
    ["Rename…", () => askRename(target)],
    [`Delete (to ${TRASH})`, () => trashItems(target), "danger"],
  ];
}
$("#tree").closest("section").addEventListener("contextmenu", (e) => {
  const li = e.target.closest("#tree li[data-path], #tree li[data-dir]");
  e.preventDefault();
  if (li?.querySelector("input")) return;
  const target = li && { path: li.dataset.path ?? li.dataset.dir, dir: li.dataset.dir !== undefined };
  showMenu(null, fileMenu(target), { x: e.clientX, y: e.clientY });
});

async function fileAction(action, body) {
  const r = await Q.fileAction(action, body);
  if (r._status !== 200) { toast(r.error || `Could not ${action}`, true); return null; }
  return r;
}
async function copyItems(paths) {
  const r = await fileAction("copy", { paths }); if (!r) return;
  fileClip = r.system ? null : paths;
  toast(r.system ? `Copied: paste it here, in ${FILE_MANAGER} or in another project`
    : `Copied, to paste in this project (${r.message})`);
}
async function pasteInto(folder) {
  const r = await fileAction("paste", { folder, paths: fileClip || undefined }); if (!r) return;
  await poll();
  toast((r.pasted.length ? `Pasted ${r.pasted.join(", ")}` : "Nothing was pasted")
    + (r.skipped.length ? `\nLeft out: ${r.skipped.join("; ")}` : ""));
}

// A name typed in the sidebar itself: Enter or leaving the box keeps it, Escape cancels.
// The tree is not redrawn meanwhile (renderTree waits), so the box stays.
function askName(li, initial, placeholder, done) {
  const input = Object.assign(document.createElement("input"), { value: initial, placeholder, spellcheck: false });
  li.replaceChildren(input);
  input.focus();
  const dot = initial.lastIndexOf(".");
  input.setSelectionRange(0, dot > 0 ? dot : initial.length);
  let over = false;
  const finish = async (keep) => {
    if (over) return;
    over = true;
    const name = input.value.trim();
    li.textContent = keep && name ? name : initial;    // until renderTree draws the tree again
    if (keep && name && name !== initial) await done(name);
    renderTree();
  };
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") finish(true);
    else if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}
const treeItem = (target) => $("#tree").querySelector(target.dir
  ? `li[data-dir="${CSS.escape(target.path)}"]` : `li[data-path="${CSS.escape(target.path)}"]`);

function askRename(target) {
  const li = treeItem(target); if (!li) return;
  askName(li, target.path.split("/").pop(), "", async (name) => {
    const tabs = S.tabs.filter((t) => within(t.path, target.path));
    for (const t of tabs) if (isDirty(t) && !t.conflict) { const saved = await TABS.save(t); if (!saved.ok) return toast(Tabs.unsavedWhy(saved)); }   // its text goes with it
    const r = await fileAction("rename", { path: target.path, name }); if (!r) return;
    const moved = (p) => r.path + p.slice(target.path.length);
    for (const t of tabs) t.path = moved(t.path);
    if (S.active && within(S.active, target.path)) S.active = moved(S.active);
    renderTabs(); persistSession(); await poll();
  });
}
function askNew(folder, kind) {
  const li = document.createElement("li");
  const header = folder && $("#tree").querySelector(`li[data-dir="${CSS.escape(folder)}"]`);
  if (header) header.after(li); else $("#tree").prepend(li);
  askName(li, "", kind === "folder" ? "folder name" : "name.tex", async (name) => {
    const r = await fileAction("new", { folder, name, kind }); if (!r) return;
    await poll();
    if (r.kind === "file" && S.files.some((f) => f.path === r.path && f.editable)) openFile(r.path);
  });
}
async function trashItems(target) {
  const unsaved = S.tabs.filter((t) => within(t.path, target.path) && isDirty(t)).map((t) => t.path);
  if (!confirm(`Move ${target.dir ? "the folder " : ""}${target.path} to the ${TRASH}?`
    + (unsaved.length ? `\n\nUnsaved edits in ${unsaved.join(", ")} will be lost.` : ""))) return;
  const r = await fileAction("trash", { paths: [target.path] }); if (!r) return;
  TABS.remove((t) => within(t.path, target.path));
  if (S.active && within(S.active, target.path)) {
    const next = S.tabs[S.tabs.length - 1];
    if (next) openFile(next.path);
    else showNoFile();
  }
  renderTabs(); persistSession(); await poll();
  toast(`Moved ${target.path} to the ${TRASH}`);
}

const MAX_UPLOAD_SIZE = 25 * 1024 * 1024;
async function uploadFile(file, destination = "attachment", signal, uploadId) {
  if (file.size > MAX_UPLOAD_SIZE) throw new Error("File is larger than 25 MB.");
  const data = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    const abort = () => { reader.abort(); reject(new DOMException("Upload cancelled", "AbortError")); };
    reader.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    reader.onloadend = () => signal?.removeEventListener("abort", abort);
    if (signal?.aborted) return abort();
    signal?.addEventListener("abort", abort, { once: true });
    reader.readAsDataURL(file);
  });
  const r = await Q.upload({ name: file.name || "pasted.png", data, destination, upload_id: uploadId, signal });
  if (r.error || !r.path) throw new Error(r.error || "Upload failed.");
  return r;
}

// The same native file-drop handling serves the Files sidebar and agent attachments.
function bindFileDrop(zone, overlay, add) {
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  const reset = () => { depth = 0; overlay.hidden = true; };
  zone.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth++; overlay.hidden = false; });
  zone.addEventListener("dragover", (e) => { if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
  zone.addEventListener("dragleave", () => { if (depth > 0 && --depth === 0) reset(); });
  zone.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); reset();
    if ([...(e.dataTransfer.items || [])].some((item) => item.webkitGetAsEntry?.()?.isDirectory))
      return toast("Drop files, not folders.");
    const files = [...e.dataTransfer.files];
    if (files.length) add(files);
    else toast("Drop files, not folders.");
  });
  window.addEventListener("dragend", reset);
  window.addEventListener("drop", reset);
}

async function importProjectFiles(files) {
  let added = 0, failed = 0;
  const status = $("#files-status");
  for (const [i, file] of files.entries()) {
    status.textContent = `Adding ${i + 1}/${files.length}: ${file.name}…`;
    try { await uploadFile(file, "project"); added++; await poll(); }
    catch (e) { failed++; toast(`${file.name}: ${e.message || e}`, true); }
  }
  status.textContent = `${added} file${added === 1 ? "" : "s"} added${failed ? ` · ${failed} failed` : ""}`;
}
let fileImportQueue = Promise.resolve();
bindFileDrop($("#sidebar"), $("#files-drop"), (files) => {
  fileImportQueue = fileImportQueue.then(() => importProjectFiles(files));
});

const KIND_ABBR = { theorem: "Thm", proposition: "Prop", lemma: "Lem", corollary: "Cor", conjecture: "Conj", claim: "Claim",
  definition: "Def", assumption: "Asm", example: "Ex", problem: "Prob", remark: "Rem", notation: "Not", hypothesis: "Hyp",
  question: "Q", exercise: "Exer", observation: "Obs", fact: "Fact", note: "Note", algorithm: "Alg", condition: "Cond" };
// The badge of a theorem-like environment, from the name it prints: \newtheorem{lem}{Lemma} -> "Lem".
function kindAbbr(kind) {
  const title = ((S.symbols.env_titles || {})[kind] || kind).replace(/\*$/, "");
  const key = title.toLowerCase();
  return KIND_ABBR[key] || KIND_ABBR[kind] || (title.length <= 5 ? title : title.slice(0, 4)).replace(/^./, (c) => c.toUpperCase());
}
function renderOutline() {
  const labelAt = new Map(S.symbols.labels.map((l) => [l.file + ":" + l.line, l.label]));
  const envs = new Set(S.symbols.environments || []);
  let html = "";
  for (const o of S.symbols.outline) {
    const isEnv = envs.has(o.kind) || KIND_ABBR[o.kind];
    const label = labelAt.get(o.file + ":" + o.line) || labelAt.get(o.file + ":" + (o.line + 1)) || "";
    const text = isEnv
      ? `<span class="kind">${esc(kindAbbr(o.kind))}</span>${o.title || label ? esc(o.title || label) : `<span class="muted">line ${o.line}</span>`}`
      : esc(o.title);
    html += `<li class="lvl-${isEnv ? "env" : o.kind}" data-file="${esc(o.file)}" data-line="${o.line}" title="${esc(o.file)}:${o.line}${label ? "  " + esc(label) : ""}">${text}</li>`;
  }
  $("#outline").innerHTML = html || `<li class="file-sep">No sections yet.</li>`;
}
$("#outline").addEventListener("click", (e) => { const li = e.target.closest("li[data-file]"); if (li) openFile(li.dataset.file, +li.dataset.line); });
$("#btn-refresh-outline").onclick = () => loadSymbols();
// Asked for from several places: only the answer to the latest request is used.
let symbolsSeq = 0;
async function loadSymbols() {
  const seq = ++symbolsSeq;
  const r = await Q.symbols();
  if (seq === symbolsSeq && r._status === 200) { S.symbols = r; renderOutline(); redrawReplies(); }
}
// The replies are drawn again when the project's macros change: the first time they are
// known (a restored log was drawn before them, and before the files its links point to)
// and after you edit a \newcommand.
let repliesMacros = null;
function redrawReplies() {
  const now = JSON.stringify(S.symbols.katex_macros || {});
  if (now === repliesMacros) return;
  repliesMacros = now;
  for (const el of $("#chat-log").querySelectorAll(".msg.assistant[data-md]")) el.innerHTML = renderMd(el.dataset.md);
}
// Labels, outline and macros follow your own edits too, a moment after they are saved
// (a new \label is offered by \cref{ right away).
let symbolsTimer = null;
function scheduleSymbols() { clearTimeout(symbolsTimer); symbolsTimer = setTimeout(loadSymbols, 1000); }

/* The project's name, as the Home page lists it: click it to rename (only the name shown
   changes; the folder keeps its own). */
function showProjectName(name, folder) {
  const el = $("#projname");
  S.projectFolder = folder;
  if (el.querySelector("input")) return;          // being renamed: leave it
  el.textContent = name;
  el.title = name === folder ? "Rename this project" : `Rename this project (folder: ${folder})`;
  document.title = name + " · prism-local";
}
function renameProject() {
  const el = $("#projname");
  if (el.querySelector("input")) return;
  const old = el.textContent, input = document.createElement("input");
  input.value = old; input.spellcheck = false; input.maxLength = 120;
  input.setAttribute("aria-label", "Project name");
  input.placeholder = S.projectFolder || "";
  el.textContent = ""; el.append(input); input.focus(); input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const name = input.value.trim();
    input.remove();
    if (!save || name === old) return showProjectName(old, S.projectFolder);
    showProjectName(name || S.projectFolder, S.projectFolder);
    if (C.job || S.building) { showProjectName(old, S.projectFolder); return toast("Wait until the agent's turn or the build has finished."); }
    const saved = await TABS.saveAll();
    if (!saved.ok) { showProjectName(old, S.projectFolder); return toast(Tabs.unsavedWhy(saved)); }
    let r = await Q.renameProject(name);
    if (r._status === 404) {
      // This project's server started before renaming existed: restart it with the new
      // code, rename, and load the page again.
      toast("Updating this project's server to rename it…");
      const ok = await restartServer();
      if (ok !== true) { showProjectName(old, S.projectFolder); return toast("Could not rename: " + ok); }
      r = await Q.renameProject(name);
      if (r._status === 200) {
        if (r.moving) { toast(`Renaming the folder to “${r.folder}”…`); await waitForNewServer(r.folder); }
        return reloadPage();
      }
    }
    if (r._status !== 200) { showProjectName(old, S.projectFolder); return toast("Could not rename: " + (r.error || "unknown error")); }
    if (r.moving) {
      // The folder is renamed too: the server stops, and starts again in it on this port.
      toast(`Renaming the folder to “${r.folder}”…`);
      const ok = await waitForNewServer(r.folder);
      if (ok === true) return reloadPage();
      return toast("Could not rename the folder: " + ok);
    }
    showProjectName(r.name, S.projectFolder);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
    e.stopPropagation();                          // not the editor's shortcuts
  });
  input.addEventListener("blur", () => finish(true));
}
$("#projname").addEventListener("click", renameProject);
$("#projname").addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === "F2") && e.target === $("#projname")) { e.preventDefault(); renameProject(); } });

// What changed on disk: files, the PDF, the sync state. Every 2 s (30 s while the tab is
// hidden, at once when it is shown again), never two at a time (see poller); poll() asks
// for one more now.
const poll = poller(pollOnce, 2000, 30000);
const UNREACHABLE = "server not reachable";
async function pollOnce() {
  // The build settings come with the first poll (at startup, or until the server answers).
  if (foreignProject) return;
  const asked = TABS.stamp(), syncAsked = SYNC_TOGGLE.stamp();
  const r = await Q.tree();
  const st = $("#build-status");
  if (r._status !== 200) { st.textContent = UNREACHABLE; st.className = "status err"; return; }
  if (st.textContent === UNREACHABLE) { st.textContent = ""; st.className = "status"; }   // it answers again
  showProjectName(r.name || r.root, r.root);
  if (r.move_error && !S.moveErrorShown) { S.moveErrorShown = true; toast(r.move_error, true); }
  if (!bindProject(r.project_key)) return;
  if (!BUILD.loaded) await loadConfig();
  const action = Watch.reconcile(S.watch, r, S.tabs.map((t) => ({ ...t, dirty: isDirty(t) })), asked);
  if (action.foreign) return stopForProject();
  S.ready = true; S.watch = r;
  S.files = r.files; S.order = r.order || []; S.folders = r.folders || [];
  if (action.treeChanged) renderTree();
  for (const path of action.gone) { const t = TABS.get(path); if (t) TABS.diskSeen(t, null); }
  for (const path of action.conflicts) { const t = TABS.get(path); if (t) TABS.diskSeen(t, r.files.find((f) => f.path === path).mtime); }
  for (const path of action.reload) { const t = TABS.get(path); if (t) await reloadFromDisk(t); }
  if (action.symbols) loadSymbols();
  if (!S.building && (r.pdf_mtime !== S.pdfMtime || r.pdf_main !== S.pdfMain)) showPdf(r.pdf_mtime, r.pdf_main);
  if (r.updated && $("#btn-restart").hidden) toast("prism-local was updated: click “Update: restart” to use the new version");
  $("#btn-restart").hidden = !r.updated;
  S.server = r.server;
  if (r.sync && SYNC_TOGGLE.fresh(syncAsked)) renderSync(r.sync);   // not one older than a switch
}

/* ------------------------------------------------------------------ GitHub sync */
// The project's changes are committed to its repository and pushed (gitsync.py): after you
// stop editing, after each agent turn, and from this menu. The button says how far that is.
const SYNC_TOGGLE = SyncState.createToggle({
  send: (on) => Q.sync(on ? "on" : "off"),
  onState: (r) => { if (r || S.sync) renderSync(r || S.sync); },
  onError: (r) => toast("Could not change sync: " + (r.error || r._status)),
});
const SYNC_NOTICE = { seen: null };      // the number of the last sync notice shown
function htmlIfChanged(selector, html) {
  const el = $(selector), previous = el._prismMarkup;
  if (!previous || previous.source !== html || previous.rendered !== el.innerHTML) {
    el.innerHTML = html; el._prismMarkup = { source: html, rendered: el.innerHTML };
  }
}
function renderSync(st) {
  S.sync = st;
  $("#sync-box").hidden = false;
  const b = $("#btn-sync"), label = $("#sync-label");
  // Without a repository, or one that is not on GitHub, the menu offers to create one.
  $("#sync-publish").hidden = st.own && st.remote;
  for (const el of document.querySelectorAll('#sync-menu [data-sync], #sync-menu label.dd-item')) el.hidden = !st.own;
  $("#sync-bind").hidden = !st.own || !st.target || !st.blocked_reason;
  S.githubUrl = st.github || "";
  $("#sync-github").hidden = !S.githubUrl;
  if (S.githubUrl) $("#sync-github").href = S.githubUrl;
  else $("#sync-github").removeAttribute("href");
  const { text, cls, detail, tech } = SyncState.describeSync(st, ago);     // syncstate.js
  label.textContent = text;
  b.className = "ghost sync-" + cls;
  b.title = detail.replace(/<[^>]+>/g, "");
  const last = st.last_commit ? `<br>Last saved ${ago(st.last_commit.at)}: <i>${esc(st.last_commit.message)}</i>` : "";
  // What git says, for those who want to know: folded away.
  htmlIfChanged("#sync-detail", esc(detail) + last + (tech ? `<details class="sync-tech"><summary>Details</summary>${esc(tech)}</details>` : ""));
  $("#sync-auto").checked = SYNC_TOGGLE.value(st.enabled);
  // Where you and a co-author changed the same lines, both versions were kept: list them.
  const kept = (st.own && st.kept) || [];
  $("#sync-kept").hidden = !kept.length;
  htmlIfChanged("#sync-kept", kept.length ? `<div class="dd-note">Both versions kept where you and a co-author changed the same lines.
      Keep what you want and delete the <code>% [prism-local]</code> lines:</div>` + kept.map((k) =>
    `<button type="button" class="dd-item" data-kept="${esc(k.path)}" data-line="${k.line || 1}">${esc(k.path)}${k.line ? ":" + k.line : ""}
       <small>${k.side ? "GitHub's version saved as " + esc(k.side) : "with " + esc(k.who)}</small></button>`).join("") + "<hr>" : "");
  // The automatic merge could not be done: compare, combine, then keep the combination.
  const clash = (st.own && st.clash) || [];
  $("#sync-clash").hidden = !clash.length;
  htmlIfChanged("#sync-clash", clash.map((f) =>
    `<button type="button" class="dd-item" data-theirs="${esc(f)}">Compare with GitHub's version: ${esc(f)}</button>`).join("")
    + (clash.length ? `<button type="button" class="dd-item" data-sync="resolve">I've combined them: keep my version of the clashing lines</button><hr>` : ""));
  // Each notice once, by its number (several requests return it), and only a recent one:
  // a page opened later does not repeat what happened before it.
  if (st.notice && st.notice_id !== SYNC_NOTICE.seen && st.notice_age < 60)
    toast(st.notice + (/[.)]$/.test(st.notice) ? "" : ".") + " The editor shows the new versions.");
  if (st.notice_id != null) SYNC_NOTICE.seen = st.notice_id;
}
function syncMenu(open) { $("#sync-menu").hidden = !open; $("#btn-sync").setAttribute("aria-expanded", String(open)); }
$("#btn-sync").onclick = async (e) => {
  e.stopPropagation(); syncMenu($("#sync-menu").hidden);
  if (!$("#sync-menu").hidden && !$("#sync-publish").hidden) {
    // Created in a terminal since the editor started? Look once, now that you ask.
    const st = await Q.recheck();
    if (st && st._status === 200) { delete st._status; renderSync(st); }
    if (!$("#sync-publish").hidden) loadPublish();
  }
  if (!$("#sync-menu").hidden && !S.githubUrl) {
    const r = await Q.github();
    S.githubUrl = r.github || "";
    if (S.githubUrl) { $("#sync-github").href = S.githubUrl; $("#sync-github").hidden = false; }
  }
};
$("#sync-menu").addEventListener("click", async (e) => {
  e.stopPropagation();
  const b = e.target.closest("[data-sync]"); if (!b) return;
  syncMenu(false);
  if (b.dataset.sync === "history") return showHistory();
  if (b.dataset.sync === "bind" && !confirm(`Sync this project with ${S.sync.target.repository_identity} on GitHub from now on?\n\nDetails: branch ${S.sync.target.branch_ref}, fetch ${S.sync.target.fetch_url}, push ${S.sync.target.push_url}`)) return;
  if (b.dataset.sync === "resolve" && !confirm("Keep your version of the lines you and GitHub both changed?\n\n"
      + "Do this after editing the file so it holds what both of you want: where both changed the same lines, "
      + "yours is kept; everything else of theirs comes in. Their version stays in their commits (History).")) return;
  if (["resolve", "commit", "pull"].includes(b.dataset.sync)) { const saved = await TABS.saveAll(); if (!saved.ok) return toast(Tabs.unsavedWhy(saved)); }
  // Your open files go to disk first: committed by Save now, and out of the way of what Get
  // changes brings in (no "changed on disk" clash with text not saved yet).

  renderSync({ ...S.sync, state: b.dataset.sync === "pull" ? "pulling" : "committing", error: null });
  const r = await Q.sync(b.dataset.sync, b.dataset.sync === "bind" ? S.sync.target : undefined);
  if (r && r._status === 200) renderSync(r);
  else if (r && r.error) toast(r.error, true);
  await poll();
});
// Create a private GitHub repository for the project (publish.publish_project, through this server).
async function loadPublish() {
  const f = $("#sync-publish"), go = $("#pub-go"), note = $("#pub-note");
  if (f.dataset.loaded) return;
  note.textContent = "Checking…"; go.disabled = true;
  const r = await Q.publishInfo();
  if (!r || r._status !== 200) { note.textContent = "Could not check: " + ((r && r.error) || "server not reachable"); return; }
  f.dataset.loaded = "1";
  if (!f.elements.name.value) f.elements.name.value = r.name;
  const pub = Util.publishState(r), gh = pub.gh;
  $("#pub-owner").textContent = `github.com/${gh.account || "…"}/ · private`;
  $("#pub-private").innerHTML = pub.leaveOut.map((d) =>
    `<label class="dd-check"><input type="checkbox" name="leave_out" value="${esc(d.dir)}" checked> Keep <code>${esc(d.dir)}/</code> (${esc(d.what)}) off GitHub</label>`).join("");
  note.textContent = {
    nested: `This folder is inside another repository (${r.top}), so it cannot have one of its own. Move the project out of it first.`,
    login: (gh.error || "The GitHub CLI is not logged in.") + " Then reopen this menu.",
    shared: `This project is part of the repository in ${r.top}: that whole repository goes to GitHub.`,
    ok: "Build output and LaTeX's auxiliary files stay out (.gitignore). From then on, changes are saved and pushed automatically.",
  }[pub.why];
  go.disabled = pub.blocked;
}
$("#sync-publish").addEventListener("click", (e) => e.stopPropagation());
$("#sync-publish").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, go = $("#pub-go");
  if (C.job || S.building) return toast("Wait until the agent's turn or the build has finished.");
  const saved = await TABS.saveAll();
  if (!saved.ok) return toast(Tabs.unsavedWhy(saved));
  go.disabled = true; go.textContent = "Creating…";
  const r = await Q.publish({
    name: f.elements.name.value.trim(),
    leave_out: [...f.querySelectorAll('input[name="leave_out"]:checked')].map((i) => i.value),
  }).catch(() => ({ error: "server not reachable" }));
  go.disabled = false; go.textContent = "Create private GitHub repository";
  if (r._status === 404) return toast("This editor runs an older version: click “Update: restart” at the top, then try again.");
  if (r.error) { $("#pub-note").textContent = r.error; return; }
  delete f.dataset.loaded;
  S.githubUrl = r.url; $("#sync-github").href = r.url; $("#sync-github").hidden = false;
  if (r.sync) renderSync(r.sync);
  syncMenu(false);
  toast(r.created ? `Created ${r.url}; this project now syncs with it.` : `Already on GitHub: ${r.url}`);
  await poll();
});
$("#sync-kept").addEventListener("click", (e) => {
  const b = e.target.closest("[data-kept]"); if (!b) return;
  e.stopPropagation(); syncMenu(false);
  openFile(b.dataset.kept, +b.dataset.line);
});
$("#sync-clash").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-theirs]"); if (!b) return;
  e.stopPropagation(); syncMenu(false);
  const seq = ++diffSeq;
  const r = await Q.theirs(b.dataset.theirs);
  if (seq !== diffSeq) return;                        // the diff view was asked for again
  if (r._status !== 200) return toast(r.error || "Could not get GitHub's version", true);
  const head = r.content === null ? `GitHub has deleted ${r.path}.` : `What GitHub's version of ${r.path} has that yours does not (+), and what yours has instead (-):`;
  $("#diff").innerHTML = diffHtml([head, "", r.diff || "(the same)"].join("\n"));
  openPanel("diff");
});
$("#sync-auto").onchange = (e) => SYNC_TOGGLE.change(e.target.checked);
document.addEventListener("click", (e) => { if (!e.target.closest("#sync-box")) syncMenu(false); });

// History: the commits that changed the open file; one of them shows what it changed and
// can be put back (as an edit of yours, which is saved and committed like any other).
// Only the latest request's answer is shown: of the list (the file can change while its
// history loads), and of the commit shown (a newer click, or a new list).
let histSeq = 0, histShowSeq = 0;
async function showHistory(path = S.active) {
  openPanel("history");
  const list = $("#hist-list"), view = $("#hist-view");
  const seq = ++histSeq; histShowSeq++;
  view.innerHTML = "";
  if (!path) { list.innerHTML = `<li class="none">Open a file to see its history.</li>`; return; }
  list.innerHTML = `<li class="none">Loading the history of ${esc(path)}…</li>`;
  const r = await Q.log(path);
  if (seq !== histSeq) return;
  const commits = r.commits || [];
  if (!commits.length) { list.innerHTML = `<li class="none">${esc(r.error || `No commits of ${path} yet.`)}</li>`; return; }
  list.innerHTML = `<li class="hist-file">${esc(path)} · ${commits.length} version${commits.length > 1 ? "s" : ""}</li>` + commits.map((c, i) =>
    `<li data-rev="${esc(c.hash)}" data-path="${esc(path)}"><span class="hist-when" title="${esc(new Date(c.at * 1000).toLocaleString())}">${esc(ago(c.at))}</span>`
    + `<span class="hist-msg">${esc(c.message)}</span><span class="hist-hash">${esc(c.hash.slice(0, 7))}${i === 0 ? " · latest" : ""}</span></li>`).join("");
}
$("#hist-list").addEventListener("click", async (e) => {
  const li = e.target.closest("li[data-rev]"); if (!li) return;
  document.querySelectorAll("#hist-list li.sel").forEach((x) => x.classList.remove("sel")); li.classList.add("sel");
  const seq = ++histShowSeq;
  const r = await Q.show(li.dataset.rev, li.dataset.path);
  if (seq !== histShowSeq) return;
  if (r._status !== 200) { $("#hist-view").innerHTML = `<div class="hist-bar">${esc("Could not show this version: " + (r.error || "HTTP " + r._status))}</div>`; return; }
  const diff = (r.diff || "").split("\n").filter((l) => !/^(diff --git|index |--- |\+\+\+ )/.test(l)).join("\n");
  $("#hist-view").innerHTML = `<div class="hist-bar"><b>${esc(li.querySelector(".hist-msg").textContent)}</b>`
    + (r.content != null ? `<button class="tiny" id="hist-restore">Restore this version</button>` : "") + `</div>`
    + `<pre>${diff ? diffHtml(diff) : "(this commit did not change the text)"}</pre>`;
  const btn = $("#hist-restore");
  if (btn) btn.onclick = async () => {
    await openFile(li.dataset.path);
    const t = activeTab(); if (!t || t.path !== li.dataset.path) return;
    t.doc.setValue(r.content);                // an edit of yours: ⌘Z takes it back, autosave records it
    toast(`Restored ${t.path} as of ${li.querySelector(".hist-when").textContent}. Undo with ⌘Z.`);
  };
});
$("#panel-tabs").addEventListener("click", (e) => { const b = e.target.closest('[data-panel="history"]'); if (b) showHistory(); });

// prism-local was updated while this server ran: restart it with the new code. Resolves
// to true once the new server answers (or to an error message). The page then reloads.
async function restartServer() {
  if (C.job || S.building) return "Wait until the agent's turn or the build has finished.";
  const saved = await TABS.saveAll();
  if (!saved.ok) return Tabs.unsavedWhy(saved);
  const r = await Q.restart();
  if (r.error) return r.error;
  return waitForNewServer();
}
// The server stopped to start again (restart, folder rename): wait until the new one answers,
// and is this project's, not another that took the port meanwhile. After a folder rename
// (`folder`, its new name) it is the project in the renamed folder, or in the old one if
// renaming did not work.
async function waitForNewServer(folder) {
  const tree = () => fetch("/api/tree", { cache: "no-store" }).then((x) => x.ok && x.json());
  const moved = folder && S.watch?.path ? S.watch.path.replace(/[^\\/]+$/, () => folder) : null;
  const ours = (t) => t.project_key === projectKey || (moved !== null && t.path === moved);
  return await Util.waitForRestart(tree, (t) => t.server !== S.server && ours(t))
    || "The server did not come back. Reopen the project from the Home page.";
}
function reloadPage() {
  if (pdfChannel) pdfChannel.postMessage({ type: "reload" });     // the pop-out PDF too
  location.reload();
}
$("#btn-restart").onclick = async () => {
  const b = $("#btn-restart"); b.disabled = true; b.textContent = "restarting…";
  const ok = await restartServer();
  if (ok === true) return reloadPage();
  b.disabled = false; b.textContent = "Update: restart";
  toast(ok);
};

/* ------------------------------------------------------------------ completion */
function lineBefore(ed) { const c = ed.getCursor(); return ed.getLine(c.line).slice(0, c.ch); }
// What to complete (\ref, \cite, \commands): chatlogic.js decides, this draws.
function computeHints(ed, explicit) {
  const cur = ed.getCursor(), found = TexComplete.completions(lineBefore(ed), S.symbols, explicit);
  if (!found) return null;
  const list = found.items.map((i) => ({ text: i.text, render: (el) => { el.innerHTML = `${esc(i.text)}<span class="hint-kind">${esc(i.kind)}</span>`; } }));
  return { list, from: { line: cur.line, ch: cur.ch - found.word.length }, to: cur };
}

function showCompletions(ed, explicit) {
  if (ed.state.completionActive) return;          // the open widget re-queries by itself
  if (!computeHints(ed, explicit)) {
    if (explicit && TexComplete.CITE_RE.test(lineBefore(ed)) && !S.symbols.bibkeys.length) toast("No BibTeX entries found in the project's .bib files.");
    return;
  }
  ed.showHint({ hint: (e) => computeHints(e, explicit), completeSingle: false });
}

async function loadConfig() {
  const r = await Q.config();
  if (r._status !== 200) return;
  BUILD.loaded = true;
  BUILD.modes = r.modes || [];
  BUILD.cmds = r.build || {};
  BUILD.main = String(r.main || "main.tex").replace(/\\/g, "/").replace(/^(\.\/)+/, "");
  const want = store.get("buildmode", "draft");
  BUILD.mode = BUILD.modes.includes(want) ? want : BUILD.modes[0] || "";
  renderCompileMenu();
  if (r.error) { $("#build-info").textContent = r.error; toast(r.error, true); }
}


/* ------------------------------------------------------------------ build */
// While a build runs, the Compile button stops it.
function setCompileButton(running) {
  const b = $("#btn-compile");
  b.classList.toggle("stop", running);
  b.querySelector("svg").outerHTML = icon(running ? "stop" : "play");
  $("#compile-label").textContent = running ? "Stop" : "Compile";
  b.title = running ? "Stop the build" : keys(`Save all and compile (${(MODE_INFO[BUILD.mode] || [BUILD.mode])[0]}) (⌘↵)`);
}

// The result of a build, yours or the agent's (its compile tool): status, problems, PDF.
async function showBuild(r) {
  const st = $("#build-status");
  if (!r.cancelled) S.diagnostics = r.diagnostics || [];      // a stopped build keeps the last list
  $("#output").textContent = r.output || "";
  const description = Util.describeBuild(r, S.diagnostics), errs = description.errors;
  st.className = "status " + description.cls; st.textContent = description.text;
  st.title = description.title; $("#build-info").textContent = description.title ? "Last build: " + description.title : "";
  renderProblems();
  applyDiagnostics();
  if ((r.exit !== 0 && !r.cancelled) || errs) openPanel(errs || !r.output ? "problems" : "output");
  await showPdf(r.pdf_mtime, r.main);
}

async function compile(clean = false, active = S.active) {
  if (S.building) return;
  // Taken at once: an auto-compile coming while this one saves (git may hold the saves for
  // a while) is not a second build, which would answer "busy" and reset the button.
  S.building = true;
  let released = false;
  try {
    if (C.editTurn) toast("Compiling the files on disk; your edits are saved when the agent's turn ends.");
    else { const saved = await TABS.saveAll(); if (!saved.ok) return toast(Tabs.unsavedWhy(saved)); }
    const mode = BUILD.mode;
    const st = $("#build-status");
    st.className = "status busy"; st.textContent = clean ? `recompiling from scratch (${mode})…` : `compiling (${mode})…`;
    setCompileButton(true);
    const r = await Q.build({ mode, clean, active });
    if (r.busy) { st.className = "status warn"; st.textContent = "a build is already running"; return; }
    if (r._status !== 200) { st.className = "status err"; st.textContent = "build failed: " + (r.error || "HTTP " + r._status); return; }
    S.building = false; setCompileButton(false); released = true;
    await showBuild(r);
  } catch (e) {
    const st = $("#build-status");
    st.className = "status err"; st.textContent = "build request failed: " + e;
  } finally {
    if (!released) { S.building = false; setCompileButton(false); }
  }
}
$("#btn-compile").onclick = () => S.building ? Q.buildStop() : compile();
$("#btn-clean-build").onclick = () => { compileMenu(false); compile(true); };

/* Compile menu: build mode and auto-compile, on the ▾ half of the Compile button. */
const BUILD = { loaded: false, modes: [], cmds: {}, main: "main.tex", mode: store.get("buildmode", "draft") };
const MODE_INFO = {
  draft: ["Draft", "continue on errors"], strict: ["Strict", "stop at first error"], check: ["Check", ""],
};
function renderCompileMenu() {
  $("#build-modes").innerHTML = BUILD.modes.map((m) => {
    const [name, note] = MODE_INFO[m] || [m, ""];
    return `<label class="dd-item" title="${esc(BUILD.cmds[m] || "")}"><input type="radio" name="build-mode" value="${esc(m)}" ${m === BUILD.mode ? "checked" : ""}> ${esc(name)}${note ? `<small>${esc(note)}</small>` : ""}</label>`;
  }).join("") || `<div class="dd-item"><small>No build command (see README)</small></div>`;
  updateCompileLabel();
}
function updateCompileLabel() {
  const name = (MODE_INFO[BUILD.mode] || [BUILD.mode || "—"])[0];
  $("#compile-mode-label").innerHTML = esc(name) + ($("#auto-compile").checked ? '<span class="auto-tag">AUTO</span>' : "");
  if (!S.building) $("#btn-compile").title = keys(`Save all and compile (${name}) (⌘↵)`);
}
function compileMenu(open) {
  $("#compile-menu").hidden = !open;
  $("#btn-compile-menu").setAttribute("aria-expanded", String(open));
}
$("#btn-compile-menu").onclick = (e) => { e.stopPropagation(); compileMenu($("#compile-menu").hidden); };
$("#compile-menu").addEventListener("click", (e) => e.stopPropagation());
$("#build-modes").addEventListener("change", (e) => {
  BUILD.mode = e.target.value; store.set("buildmode", BUILD.mode); updateCompileLabel(); compileMenu(false);
});
document.addEventListener("click", () => compileMenu(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") compileMenu(false); });
$("#auto-compile").checked = store.get("autocompile", false);
$("#auto-compile").onchange = (e) => { store.set("autocompile", e.target.checked); updateCompileLabel(); };
updateCompileLabel();

function renderProblems() {
  const d = S.diagnostics;
  const errs = d.filter((x) => x.severity === "error").length;
  const b = $("#problem-count");
  b.textContent = d.length ? String(d.length) : ""; b.className = "badge" + (errs ? "" : " warn");
  $("#problems").innerHTML = d.length ? d.map((x, i) => `
    <li data-i="${i}"><span class="sev ${esc(x.severity)}">${esc(x.severity)}</span><span class="loc">${esc(x.file || "?")}${x.line ? ":" + x.line : ""}</span>${esc(x.message)}</li>`).join("")
    : `<li class="none">No errors or warnings from the build.</li>`;
}
$("#problems").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-i]"); if (!li) return;
  const d = S.diagnostics[+li.dataset.i];
  if (d.file) openFile(d.file, d.line);
});

let diagMarks = [];
function applyDiagnostics() { cm.operation(applyDiagnosticMarks); }
function applyDiagnosticMarks() {
  for (const [doc, h, cls] of diagMarks) doc.removeLineClass(h, "background", cls);
  diagMarks = [];
  for (const d of S.diagnostics) {
    const t = S.tabs.find((x) => x.path === d.file);
    if (!t || !d.line || d.line > t.doc.lineCount()) continue;
    const cls = d.severity === "error" ? "cm-line-error" : "cm-line-warning";
    diagMarks.push([t.doc, t.doc.addLineClass(d.line - 1, "background", cls), cls]);
  }
}

/* ------------------------------------------------------------------ bottom panel & diff */
function openPanel(name) {
  $("#panel").classList.remove("collapsed"); $("#panel-toggle").classList.add("flip");
  document.querySelectorAll("#panel-tabs [data-panel]").forEach((b) => b.classList.toggle("active", b.dataset.panel === name));
  document.querySelectorAll(".panel-view").forEach((v) => { v.hidden = v.id !== name; });
  cm.refresh();
}
$("#panel-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-panel]"); if (b) { openPanel(b.dataset.panel); if (b.dataset.panel === "diff") showDiff(); }
});
$("#panel-toggle").onclick = () => {
  const p = $("#panel"); p.classList.toggle("collapsed");
  $("#panel-toggle").classList.toggle("flip", !p.classList.contains("collapsed")); cm.refresh();
};
// The diff view shows the answer to the latest request only (also GitHub's version, above).
let diffSeq = 0;
async function showDiff(path) {
  const seq = ++diffSeq;
  const r = await Q.diff(path || undefined);
  if (seq !== diffSeq) return;
  const what = path ? path : "this project";
  let text;
  if (r._status !== 200) text = `Could not get the changes in ${what}: ${r.error || "HTTP " + r._status}`;
  else if (r.repo === false) text = `${what === "this project" ? "This project" : what} is not in a git repository, so there is nothing to compare with.\nCreate one from the GitHub button at the top (Not on GitHub).`;
  else {
    const parts = [];
    if (r.untracked && r.untracked.length) parts.push(`New files, not committed yet: ${r.untracked.join(", ")}`);
    if (r.diff) parts.push(r.diff);
    else if (r.last) parts.push(`No uncommitted changes in ${what}: everything is committed${S.sync && S.sync.remote ? " (and pushed to GitHub with sync on)" : ""}.\n`
      + `The last commit that changed it, ${ago(r.last.at)}: ${r.last.message}  [${r.last.hash}]\n\n${r.last.diff}`);
    else if (!parts.length) parts.push(`No uncommitted changes in ${what}.`);
    text = parts.join("\n\n") || r.diff || "";
  }
  $("#diff").innerHTML = diffHtml(text);
  openPanel("diff");
}

/* ------------------------------------------------------------------ PDF */
PV.init({ scaleKey: "scale" });
PV.onInverse = (p) => inverseJump(p.page, p.x, p.y);
// A plain click does nothing: say once how to jump to the source.
let pdfHinted = false, pdfHintTimer = null;
$("#pdf-scroll").addEventListener("click", (e) => {
  if (pdfHinted || e.detail !== 1 || e.ctrlKey || e.metaKey || !e.target.closest(".pdf-page")
      || e.target.closest(".pdf-link") || String(window.getSelection() || "")) return;    // a link, or selecting text
  pdfHintTimer = setTimeout(() => {          // not when this click was the first of a double-click
    pdfHinted = true; toast(`Double-click (or ${IS_MAC ? "⌘" : "Ctrl"}-click) the PDF to jump to the source.`);
  }, 400);
});
$("#pdf-scroll").addEventListener("dblclick", () => clearTimeout(pdfHintTimer));

// Pop-out viewer: while a viewer tab is alive, the inline PDF pane is hidden, and the top
// bar says where the PDF went. "Show here" closes that tab, or, where the browser does not
// let it close, shows the PDF here anyway until you pop it out again.
const POP = { alive: false, last: 0, here: false };
function setPopped(on) {
  if (on && POP.here) on = false;
  $("#btn-pdf-here").hidden = !on;
  if (POP.alive === on) return;
  POP.alive = on;
  $("#pdf-pane").hidden = on; document.querySelector('.gutter[data-resize="pdf"]').hidden = on;
  // The Compile button lives on the PDF toolbar; while that is hidden, show it in the top bar.
  if (on) $("#build-status").before($("#compile-box")); else $("#pdf-toolbar").prepend($("#compile-box"));
  cm.refresh();
  if (!on && (S.pdfMtime !== PV.mtime || S.pdfMain !== PV.main)) PV.load(S.pdfMtime, S.pdfMain);
}
let popWin = null;
$("#btn-pdf-here").onclick = () => {
  if (pdfChannel) pdfChannel.postMessage({ type: "close" });
  POP.here = true; setPopped(false);
  PV.load(S.pdfMtime, S.pdfMain);
};
function popOut() {
  POP.here = false;
  popWin = window.open("/viewer" + Client.query({ project_key: projectKey }), "prism-pdf");
  if (popWin) popWin.focus();
}
$("#pdf-popout").onclick = (e) => { e.preventDefault(); popOut(); };

// Is a viewer tab open? Each viewer holds the Web Lock VIEWER_LOCK for as long as it
// lives, and the browser releases it the moment the tab closes or crashes. Waiting for
// that lock tells us when the last viewer is gone, however much the browser throttles a
// hidden viewer's timers. (Its heartbeat can arrive a minute late, which used to make the
// inline PDF flash back in; the heartbeat now only serves browsers without Web Locks.)
const VIEWER_LOCK = "prism-pdf-viewer";
const LOCKS = navigator.locks && navigator.locks.request ? navigator.locks : null;
let watchingViewer = false;
function watchViewer() {
  if (!LOCKS || watchingViewer) return;
  watchingViewer = true;
  // Granted only when no viewer holds the lock; release it again at once.
  LOCKS.request(VIEWER_LOCK, async () => {
    const another = (await LOCKS.query()).pending.some((l) => l.name === VIEWER_LOCK);
    if (!another) setPopped(false);
    return another;
  }).then((another) => { watchingViewer = false; if (another) setTimeout(watchViewer, 0); });
}
if (pdfChannel) pdfChannel.onmessage = (ev) => {
  const m = ev.data || {};
  if (m.type === "alive") { POP.last = Date.now(); setPopped(true); watchViewer(); }
  else if (m.type === "bye") { if (!LOCKS) setPopped(false); }     // the lock watcher notices
  else if (m.type === "inverse") {
    // Answer the viewer, which says where the jump went and brings this tab to the front.
    inverseJump(m.page, m.x, m.y).catch((e) => ({ msg: String(e) }))
      .then((res) => pdfChannel.postMessage({ type: "jumped", id: m.id, name: window.name, ...res }));
    window.focus();
  }
};
// The viewer finds this tab by its name to bring it to the front (viewer.js). A tab opened
// from the Home page keeps the name Home gave it ("prism-<id>"), by which Home reuses it.
if (!window.name) window.name = "prism-editor-" + location.port;
if (LOCKS) {
  // A viewer that was already open when this editor (re)loaded.
  LOCKS.query().then((s) => { if (s.held.some((l) => l.name === VIEWER_LOCK)) { setPopped(true); watchViewer(); } });
} else {
  setInterval(() => { if (POP.alive && Date.now() - POP.last > 90000) setPopped(false); }, 5000);
}

async function showPdf(mtime, main) {
  S.pdfMtime = mtime; S.pdfMain = main;
  if (pdfChannel) pdfChannel.postMessage({ type: "pdf", mtime, main });
  // Not loaded (a build still writing it, a file cut short): forget it, so a later poll asks
  // again, at most every few seconds.
  if (!POP.alive && !(await PV.load(mtime, main)) && S.pdfMtime === mtime && S.pdfMain === main) {
    const failed = S.pdfMtime;
    setTimeout(() => { if (S.pdfMtime === failed && S.pdfMain === main) S.pdfMtime = null; }, 3000);
  }
}

/* ------------------------------------------------------------------ SyncTeX */
async function forwardSync() {
  const t = activeTab(); if (!t || !t.path.endsWith(".tex")) return;
  if (!S.pdfMtime) return toast("No PDF yet — compile first.");
  const line = cm.getCursor().line + 1;
  const r = await Q.forward(t.path, line);
  if (r._status !== 200) return toast("No PDF location for this line (compile, or the line produces no output).");
  if (POP.alive && pdfChannel) pdfChannel.postMessage({ type: "forward", r });
  else PV.highlight(r);
}
$("#btn-forward").onclick = () => forwardSync();

// Returns what happened, for the pop-out viewer to show: {ok, file, line} or {msg}.
async function inverseJump(page, x, y) {
  const r = await Q.inverse(page, x, y);
  const fail = (msg) => { toast(msg); return { msg }; };
  if (r._status !== 200 || !r.file) return fail("No source location found here.");
  if (!S.files.some((f) => f.path === r.file)) return fail(`Source is ${r.file}:${r.line} (not editable here).`);
  await openFile(r.file, r.line);
  return { ok: true, file: r.file, line: r.line };
}

/* ------------------------------------------------------------------ misc UI */
function sidebarHidden(h) {
  $("#sidebar").classList.toggle("hidden", h); $("#sidebar-gutter").classList.toggle("hidden", h);
  store.set("sidebar.hidden", h); cm.refresh();
}
sidebarHidden(store.get("sidebar.hidden", false));
$("#btn-sidebar").onclick = () => sidebarHidden(!$("#sidebar").classList.contains("hidden"));

// Build status stays in its own line; actionable errors remain until dismissed.
// `error`: it stays until dismissed. Messages from the server pass true; the guess is for the rest.
function toast(msg, error = /failed|error|could not|cannot |not saved|not undone|unreachable|not reachable|resolve .*conflict/i.test(String(msg))) {
  notice(String(msg), error);
}

document.querySelectorAll(".gutter").forEach((g) => {
  g.addEventListener("mousedown", (e) => {
    e.preventDefault(); g.classList.add("drag");
    const target = { sidebar: $("#sidebar"), pdf: $("#pdf-pane"), chat: $("#chat") }[g.dataset.resize];
    const startX = e.clientX, startW = target.getBoundingClientRect().width;
    const resize = Util.frameBatch((_, x) => {
      const dx = x - startX;
      const w = Math.max(160, startW + (g.dataset.resize === "sidebar" ? dx : -dx));
      target.style.width = w + "px"; cm.refresh();
    });
    const move = (ev) => resize.set("width", ev.clientX);
    const up = () => {
      resize.flush();
      g.classList.remove("drag"); document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up);
      store.set("w." + g.dataset.resize, target.style.width);
    };
    document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
  });
});
for (const [k, sel] of [["sidebar", "#sidebar"], ["pdf", "#pdf-pane"], ["chat", "#chat"]]) { const w = store.get("w." + k, null); if (w) $(sel).style.width = w; }

document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented) return;          // CodeMirror already handled it (its own ⌘S, ⌘↵)
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key === "s") { e.preventDefault(); saveActive(); }
  else if (mod && e.key === "Enter") { e.preventDefault(); compile(); }
  else if (mod && e.key === "b" && !e.shiftKey) { e.preventDefault(); sidebarHidden(!$("#sidebar").classList.contains("hidden")); }
  // Find in the open file, from wherever the focus is but the agent's panel (the editor has its own keys).
  else if (mod && e.code === "KeyF" && !e.shiftKey && !e.target.closest("#chat")) { e.preventDefault(); findOpen(e.altKey); }
  else if (e.ctrlKey && e.code === "KeyH" && !e.shiftKey && !e.altKey && !e.target.closest("#chat")) { e.preventDefault(); findOpen(true); }
});
window.addEventListener("beforeunload", (e) => { if (S.tabs.some(isDirty)) { e.preventDefault(); e.returnValue = ""; } });

/* ------------------------------------------------------------------ agent panel */
// The agent runs on one provider at a time (Claude Code, Codex CLI, DeepSeek or another
// OpenAI-compatible API; see backends.py). Conversation, model and effort are kept per
// provider. P holds what /api/agent/info reports about each provider.
const P = { list: [], byId: {}, projectKey: null };
const C = { provider: store.get("chat.provider", null), job: null };
const prov = () => P.byId[C.provider] || { id: C.provider, label: "Agent", models: [], efforts: [] };
const provLabel = () => prov().label || "the agent";
// Settings saved before providers existed belong to Claude Code.
// Where each is kept: per project and provider, or per provider (chatlogic.js).
const conversationKey = (k, provider = C.provider) => ChatLogic.storageKey(k, P.projectKey, provider);
const provGet = (k) => { const key = conversationKey(k); return key ? store.get(key, null) : null; };
const provSet = (k, v) => { const key = conversationKey(k); if (key) store.set(key, v); };
// The account the provider runs under (Claude Code: `claude auth status`), and whether
// it is the one allowed in Home → Settings. A mismatch stops every turn on the server.
const providerSeq = { account: 0, usage: 0, catalog: 0 };
async function loadAccount() {
  const provider = C.provider, seq = ++providerSeq.account;
  const el = $("#agent-account");
  const r = await Q.account(provider);
  if (provider !== C.provider || seq !== providerSeq.account) return;
  const a = r && r._status === 200 && r.account;
  if (!a) { el.hidden = true; return; }
  el.hidden = false;
  const who = a.email ? a.email + (a.org ? " · " + a.org : "") : "not logged in";
  el.className = r.problem ? "bad" : r.allowed ? "locked" : "";
  el.textContent = r.problem ? r.problem : (r.allowed ? "Allowed account: " : "Account: ") + who;
  el.title = r.problem ? "" : r.allowed
    ? "Only this account may be used (Home → Settings). Each message is checked before it is sent."
    : "The account Claude Code is logged in to. Home → Settings can allow only this one.";
}
function loadProvider() {
  C.session = provGet("session"); C.model = provGet("model"); C.effort = provGet("effort");
  const c = provGet("context");                    // how full this conversation's context is
  renderContext(c && c.session === C.session ? c : null);
  C.snips = provGet("snips") || {};
  const saved = provGet("log");
  restoreChatLog(saved || "");
  if (!saved) chatIntro();
}

/* How full the conversation's context window is, as a ring by the Send button (like the
   Claude desktop app); the details open on hover or click. From Claude Code's usage of
   each message: what the model read plus what it wrote. */
function renderContext(c) {
  const box = $("#ctx");
  if (!c || !c.used) { box.hidden = true; return; }
  box.hidden = false;
  const k = (n) => (n >= 1e6 ? +(n / 1e6).toFixed(n % 1e6 ? 1 : 0) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n));
  const used = c.window ? Math.min(1, c.used / c.window) : null;
  const pct = used === null ? null : Math.round(used * 100);
  const ring = $("#ctx-ring .fill"), len = 2 * Math.PI * 7.5;
  ring.style.strokeDasharray = `${len}`;
  ring.style.strokeDashoffset = `${len * (1 - (used ?? 0))}`;
  const level = used === null ? "" : used >= 0.9 ? "err" : used >= 0.75 ? "warn" : "";
  $("#ctx-ring").className = "ghost " + level;
  // The percentage shows by the ring once half the window is used; before, on hover.
  $("#ctx-label").textContent = pct !== null && pct >= 50 ? `${pct}%` : "";
  $("#ctx-ring").title = "";
  $("#ctx-pop").innerHTML = `<div class="ctx-head"><b>Context window</b>${pct !== null ? `<span class="${level}">${pct}% used</span>` : ""}</div>`
    + `<div class="ctx-bar"><i class="${level}" style="width:${Math.max(1, pct ?? 0)}%"></i></div>`
    + `<div class="ctx-num">${k(c.used)}${c.window ? ` / ${k(c.window)}` : ""} tokens${c.window ? ` · ${k(Math.max(0, c.window - c.used))} left` : ""}</div>`
    + `<div class="ctx-note">${used !== null && used >= 0.75
      ? "Nearly full. Claude Code summarizes the conversation automatically when it runs out; type <code>/compact</code> to do it now, or start a <b>New</b> chat."
      : "Everything in this conversation so far: your messages, the files the agent read, its replies. <b>New</b> starts an empty one."}</div>`;
}
$("#ctx-ring").onclick = (e) => {
  e.stopPropagation();
  const pop = $("#ctx-pop"); pop.hidden = !pop.hidden;
  $("#ctx-ring").setAttribute("aria-expanded", String(!pop.hidden));
};
document.addEventListener("click", (e) => { if (!e.target.closest("#ctx")) { $("#ctx-pop").hidden = true; $("#ctx-ring").setAttribute("aria-expanded", "false"); } });
// Load conversations only after the server supplies the project identity.

function chatHidden(h) {
  $("#chat").classList.toggle("hidden", h); $("#chat-gutter").classList.toggle("hidden", h);
  store.set("chat.hidden", h); cm.refresh();
}
chatHidden(store.get("chat.hidden", false));
$("#btn-chat").onclick = () => chatHidden(!$("#chat").classList.contains("hidden"));
$("#chat-mode").value = store.get("chat.mode", "edit");
$("#chat-mode").onchange = (e) => store.set("chat.mode", e.target.value);

// The agent's replies: Markdown (marked: tables, lists, headings, links) with TeX math as
// KaTeX, the project's macros included. The math is found before Markdown reads the reply
// (mathtext.js), so _ * \\ | in it stay TeX. Raw HTML in a reply is shown as text, links
// open only http(s)/mailto in a new tab, and file.tex:12 becomes a link into the editor.
const FILE_REF = /((?:[\w.-]+\/)*[\w.-]+\.(?:tex|bib|md))(?::(\d+))?/g;
const MD = typeof marked === "undefined" || typeof katex === "undefined" ? null
  : new marked.Marked({ gfm: true, breaks: true }).use({
    renderer: {
      // A line that starts with a tag: text, and the rest of it still Markdown.
      html: (html, block) => block ? `<p>${MD.parseInline(html.trim())}</p>` : esc(html),
      link: (href, title, text) => /^(https?:|mailto:)/i.test(href || "")
        ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer"${title ? ` title="${esc(title)}"` : ""}>${text}</a>` : text,
      image: (href, title, text) => esc(text || ""),
    },
  });
function linkFiles(root) {
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => n.parentElement.closest("pre, a, .katex") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  const nodes = [];
  while (walk.nextNode()) nodes.push(walk.currentNode);
  for (const n of nodes) {
    const h = esc(n.data).replace(FILE_REF, (m, f, ln) => {
      const hit = S.files.find((x) => x.path === f || x.path.endsWith("/" + f));
      return hit ? `<a class="src" data-file="${esc(hit.path)}" data-line="${ln || ""}">${m}</a>` : m;
    });
    if (h !== esc(n.data)) { const t = document.createElement("template"); t.innerHTML = h; n.replaceWith(t.content); }
  }
}
function renderMd(text) {
  if (!MD) return renderPlain(text);
  const box = document.createElement("div");
  box.innerHTML = MathText.render(String(text), { markdown: (s) => MD.parse(s), katex, macros: S.symbols.katex_macros });
  linkFiles(box);
  return box.innerHTML;
}
// Without the vendored libraries: escape first, then code fences, inline code, bold, file links.
function renderPlain(text) {
  const parts = String(text).split(/```[a-zA-Z]*\n?/);
  return parts.map((p, i) => {
    if (i % 2) return `<pre>${esc(p.replace(/\n$/, ""))}</pre>`;
    let h = esc(p);
    h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    h = h.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
    h = h.replace(FILE_REF, (m, f, ln) => {
      const hit = S.files.find((x) => x.path === f || x.path.endsWith("/" + f));
      return hit ? `<a class="src" data-file="${esc(hit.path)}" data-line="${ln || ""}">${m}</a>` : m;
    });
    return h;
  }).join("");
}

function chatAppend(html, cls) {
  const log = $("#chat-log"), stick = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  const div = document.createElement("div");
  if (cls) div.className = cls;
  div.innerHTML = html;
  log.appendChild(div);
  if (stick || cls === "msg user") log.scrollTop = log.scrollHeight;     // unless you scrolled up to read
  return div;
}
// The conversation is kept as HTML: its latest messages, whole, up to 400 000 characters.
function saveChatLog() {
  provSet("log", Util.boundedLog([...$("#chat-log").children].map((el) => el.outerHTML)));
}
// Earlier versions cut the saved log anywhere, even inside a tag, where the rest of an
// attribute (a tool's command, say) could read as new HTML. So a restored log keeps only
// whole elements, and loses scripts, event handlers and links other than web and mail.
const CHAT_DROP = "script, style, iframe, object, embed, link, meta, base, form, img, video, audio, "
  + "foreignObject, use, animate, set, animateMotion, animateTransform";
function restoreChatLog(html) {
  const t = document.createElement("template");
  t.innerHTML = html;                                  // inert: nothing in it runs or loads
  for (const n of [...t.content.childNodes]) if (!/^(DIV|DETAILS)$/.test(n.nodeName)) n.remove();   // the log's own boxes
  t.content.querySelectorAll(CHAT_DROP).forEach((el) => el.remove());
  for (const el of t.content.querySelectorAll("*")) {
    for (const a of [...el.attributes]) {
      const url = /^(href|src|action|formaction|xlink:href)$/i.test(a.name);
      if (/^on/i.test(a.name) || (url && !/^(https?:|mailto:)/i.test(a.value.trim()))) el.removeAttribute(a.name);
    }
  }
  // A reply whose text was kept is drawn from it again, as replies are drawn now.
  for (const el of t.content.querySelectorAll(".msg.assistant[data-md]")) el.innerHTML = renderMd(el.dataset.md);
  $("#chat-log").replaceChildren(t.content);
}
function chatIntro() {
  chatAppend(`The agent works in this repository and follows the project's CLAUDE.md / AGENTS.md.
Pick who runs it in the menu above: Claude Code, Codex CLI, or an API model such as DeepSeek.
<b>Edit</b> mode may change files — every turn ends with a diff and an Undo button.
<b>Ask</b> mode is read-only. Type <code>@</code> to point the agent at a file or the selection
(or select text and press <code>${keys("⌘L")}</code>): it may then change only those files.
Without <code>@</code>, it may change any file in the project. Type <code>/</code> for commands.
Commits and pushes are prohibited. Permission enforcement depends on the selected provider;
post-turn restoration is not an access boundary.`, "msg intro");
}

/* Mentions. "@path" points the agent at a file, "@path:12-18" at those lines (made from the
   editor selection). With mentions, the agent may change only the mentioned files: the
   Claude and Codex use native permissions; API tools check paths before writing.
   Deep Code scope uses post-turn restoration. Without any, it may change any
   file in the project and create new ones. */
C.snips = {};      // selection text belongs to the current project and provider

function selectionMention() {
  const t = activeTab(), sel = cm.getSelection();
  if (!t || !sel) return null;
  const from = cm.getCursor("from").line + 1, to = cm.getCursor("to").line + 1;
  return { token: `@${t.path}:${from === to ? from : from + "-" + to}`, file: t.path, text: sel.slice(0, 6000) };
}
const parseMentions = (text) => ChatLogic.parseMentions(text, S.files);
const rangeLabel = (m) => m.from ? `${m.file}:${m.from}${m.to !== m.from ? "–" + m.to : ""}` : m.file;

async function referenceBlock(mentions) {
  if (!mentions.length) return "";
  let b = "[Referenced]\n";
  for (const m of mentions) {
    if (!m.from) { b += `File: ${m.file}\n`; continue; }
    let txt = C.snips[m.token];
    if (txt === undefined) {
      const r = await Q.file(m.file);
      txt = (r.content || "").split("\n").slice(m.from - 1, m.to).join("\n");
    }
    b += `File: ${m.file}, lines ${m.from}–${m.to} (change only these lines unless the request needs more):\n\`\`\`latex\n${txt}\n\`\`\`\n`;
  }
  return b + "[/Referenced]";
}
function describeScope(mentions, mode) {
  if (mode === "ask") return prov().read_only_ask === false
    ? "Ask: writes denied by category; changes restored after the turn (no OS sandbox)" : "Ask mode: read-only, no agent compilation";
  if (!mentions.length) return "Scope: whole workspace (any file, new files allowed)";
  return "Scope: only " + mentions.map(rangeLabel).join(", ")
    + (prov().enforces_scope === false ? " (restored after the turn; not a write boundary)" : "");
}
function updateScope() {
  const ms = parseMentions($("#chat-input").value), mode = $("#chat-mode").value;
  const el = $("#chat-scope");
  el.textContent = describeScope(ms, mode);
  el.className = mode === "ask" ? "ask" : ms.length ? "narrow" : "wide";
  el.title = "Type @ to point the agent at a file or at the editor selection. "
    + "With @-mentions it may change only those files; without, any file in the project.";
}
function mentionHtml(text) {
  return esc(text).replace(/(^|\s)(@[^\s@]+)/g, '$1<span class="mention">$2</span>');
}
$("#chat-input").addEventListener("input", updateScope);
$("#chat-mode").addEventListener("change", updateScope);

// Insert a mention at the caret (⌘L, or picking one from the @ menu).
function insertMention(token, snip, replaceFrom) {
  const inp = $("#chat-input"), v = inp.value, caret = inp.selectionStart ?? v.length;
  const start = replaceFrom ?? caret;
  const before = v.slice(0, start), after = v.slice(caret);
  const pad = before && !/\s$/.test(before) ? " " : "";
  inp.value = before + pad + token + " " + after.replace(/^\s+/, "");
  const pos = (before + pad + token + " ").length;
  inp.setSelectionRange(pos, pos);
  if (snip !== undefined) {
    delete C.snips[token]; C.snips[token] = snip;             // newest last; keep the latest 50
    C.snips = Object.fromEntries(Object.entries(C.snips).slice(-50));
    provSet("snips", C.snips);
  }
  updateScope(); inp.focus();
}

/* Where the agent works, marked in the editor: the lines it reads (blue), the passage it is
   rewriting (amber, pulsing), and the lines it changed this turn (green, until your next
   message or Undo). The file it is on gets a pulsing dot in the tabs and the file list.
   Marks are line classes on the tab's document, kept here by line number so they come back
   when a tab is opened or reloaded from disk. */
const AH = {
  marks: new Map(),     // path -> [{kind, from, to}] (0-based lines)
  handles: new Map(),   // path -> [[doc, line, cls]] applied now
  here: null,           // the file the agent is on

  set(path, kind, from, to) {
    const list = (this.marks.get(path) || []).filter((m) => kind === "changed" || m.kind !== kind);
    list.push({ kind, from: Math.max(0, from), to: Math.max(from, to) });
    this.marks.set(path, list);
    this.apply(path);
  },

  clear(kind = null) {
    for (const [path, list] of this.marks) {
      const next = kind ? list.filter((m) => m.kind !== kind) : [];
      if (next.length === list.length) continue;
      if (next.length) this.marks.set(path, next); else this.marks.delete(path);
      this.apply(path);
    }
  },

  // Put the marks of `path` on its tab's document (after a swap, a reload, a change).
  apply(path) { cm.operation(() => this.applyNow(path)); },
  applyNow(path) {
    for (const [doc, line, cls] of this.handles.get(path) || []) {
      doc.removeLineClass(line, "background", cls); doc.removeLineClass(line, "gutter", cls + "-g");
      doc.removeLineClass(line, "wrap", cls + "-w");
    }
    const t = S.tabs.find((x) => x.path === path), out = [];
    if (t) {
      const last = t.doc.lineCount() - 1;
      for (const m of this.marks.get(path) || []) {
        const cls = "cm-agent-" + m.kind;
        for (let l = m.from; l <= Math.min(m.to, last, m.from + 3000); l++) {
          const h = t.doc.addLineClass(l, "background", cls);
          t.doc.addLineClass(h, "gutter", cls + "-g");
          t.doc.addLineClass(h, "wrap", cls + "-w");      // the line's text: its colours (app.css)
          out.push([t.doc, h, cls]);
        }
      }
    }
    this.handles.set(path, out);
  },

  // The agent is on `path` now (null: on no file).
  on(path) {
    if (this.here === path) return;
    this.here = path;
    document.querySelectorAll(".agent-here").forEach((el) => el.classList.remove("agent-here"));
    if (path) document.querySelectorAll(`#tabs .tab[data-path="${CSS.escape(path)}"], #tree li[data-path="${CSS.escape(path)}"]`)
      .forEach((el) => el.classList.add("agent-here"));
  },

  // Keep the dot through re-renders of the tabs and the file list.
  refresh() { const p = this.here; this.here = null; this.on(p); },
};

/* The file the agent is writing, live, over the editor. A read-only view: your tabs, their
   saving and their undo history are not touched. For an Edit, the text it replaces has a red
   background and the new text appears after it in green; for a Write, the file appears from the top.
   When the step ends the view closes and the editor shows the changed lines. */
const AV = {
  cm: null, id: null, path: null, name: null, hidden: false,
  base: null, old: "", oldDone: false, text: "", shown: 0, at: null, marks: [],

  view() {
    if (!this.cm) {
      this.cm = CodeMirror($("#agent-view-cm"), { readOnly: true, lineNumbers: true, lineWrapping: true });   // begin() sets the mode
      $("#agent-view-hide").onclick = () => { this.hidden = true; $("#agent-view").hidden = true; };
    }
    return this.cm;
  },

  async begin(id, name, path) {
    Object.assign(this, { id, name, path, base: null, old: "", oldDone: false, text: "", shown: 0, at: null, marks: [] });
    const v = this.view();
    v.setOption("mode", modeFor(path));
    $("#agent-view-title").textContent = `${name === "Write" ? "Writing" : "Editing"} ${path}`;
    $("#agent-view").hidden = !Turn.mayFollow({ started: C.followOpened, current: openAsked, touched: !C.follow, hidden: this.hidden });
    v.setValue(""); v.refresh();
    if (name === "Write") { this.base = ""; return; }
    // An edit changes the file as it is on disk.
    const r = await Q.file(path);
    if (this.id !== id) return;
    v.setValue(r.content || ""); this.base = v.getValue();
    this.render();
  },

  feed(e) {
    if (e.id !== this.id) return;
    if (e.old) this.old += e.old;
    if (e.old_done) this.oldDone = true;
    if (e.text) this.text += e.text;
  },

  // Called once per batch of events.
  render() {
    const v = this.cm;
    if (!v || this.id === null || this.base === null) return;
    if (this.at === null) {
      if (this.name === "Write") this.at = 0;
      else {
        if (!this.oldDone && !this.text) return;        // the text it replaces is not complete yet
        const i = this.base.indexOf(this.old.replace(/\r\n/g, "\n"));
        if (i < 0 || !this.old) this.at = this.base.length;   // not found: show the new text at the end
        else {
          this.at = i + this.old.length;
          this.marks.push(v.markText(v.posFromIndex(i), v.posFromIndex(this.at), { className: "cm-agent-del" }));
          AH.set(this.path, "edit", v.posFromIndex(i).line, v.posFromIndex(this.at).line);   // in the editor too
        }
      }
    }
    if (this.text.length > this.shown) {
      const from = v.posFromIndex(this.at + this.shown);
      v.replaceRange(this.text.slice(this.shown), from);
      this.shown = this.text.length;
      this.marks.push(v.markText(from, v.posFromIndex(this.at + this.shown), { className: "cm-agent-add" }));
    }
    if (!$("#agent-view").hidden) v.scrollIntoView(v.posFromIndex(this.at + this.shown), v.getScrollInfo().clientHeight / 3);
  },

  // The step `id` ended (null: the turn ended). Show the result in the editor.
  async end(id, ok) {
    if (this.id === null || (id !== null && id !== this.id)) return;
    const turnJob = C.job;
    const { path, name, text, at } = this, line = this.cm.posFromIndex(at || 0).line + 1;
    const start = Math.max(0, (at || 0) - this.old.length);   // where the edit was, in the old file
    this.id = null;
    await new Promise((res) => setTimeout(res, 400));
    AH.clear("edit");
    if (!ok) { if (this.id === null) $("#agent-view").hidden = true; return; }
    await poll();                                       // take in the new file (a new one is listed now)
    const t = S.tabs.find((x) => x.path === path);
    if (t && name === "Edit") {                         // mark the lines it changed
      const doc = t.doc.getValue(), nt = text.replace(/\r\n/g, "\n");
      let i = nt ? doc.indexOf(nt, Math.max(0, start - 2000)) : -1;
      if (nt && i < 0) i = doc.indexOf(nt);
      const from = i >= 0 ? t.doc.posFromIndex(i).line : Math.min(line - 1, t.doc.lineCount() - 1);
      const to = i >= 0 ? t.doc.posFromIndex(i + nt.length).line : from;
      AH.set(path, "changed", from, to);
    }
    if (this.id !== null) return;                       // the next step already took the view
    $("#agent-view").hidden = true;
    // Show the result where it is, unless you hid the agent's view.
    if (!S.files.some((f) => f.path === path)) return;
    if (C.job === turnJob && Turn.mayFollow({ started: C.followOpened, current: openAsked, touched: !C.follow, hidden: this.hidden })) {
      await openFile(path, line, true); C.followOpened = openAsked;
    } else chatAppend(`<a data-file="${esc(path)}" data-line="${line}">Jump to ${esc(path)}:${line}</a>`, "card");
  },
};

// Turning to something else while the agent works ends following it: focus moved out of the
// editor (its own view excepted), or typing in the message box or the editor.
function stopFollowing() { if (C.job) { C.follow = false; $("#agent-view").hidden = true; } }
document.addEventListener("focusin", (e) => {
  if (!cm.getWrapperElement().contains(e.target) && !$("#agent-view").contains(e.target)) stopFollowing();
});
$("#chat-input").addEventListener("input", stopFollowing);

/* Files added to a message: the + button, files dragged onto the panel, an image pasted
   into the box. Each is saved in the project's prism-uploads/ folder (/api/upload),
   committed in the project's repository and pushed to GitHub, and shown as a chip whose
   badge says how that went. The message tells the agent where the files are, and it
   reads them with its file tools (Claude Code reads PDFs and images too). */
const ATT = {
  files: [],             // {id, uploadId, name, size, path|null (uploading), error}
  // Removing a chip stops its upload, and takes back what the server may have saved of it
  // already (uploaded, or the stop came too late): the file goes unless a message sent it.
  remove(id) {
    const f = this.files.find((x) => x.id === id);
    f?.controller?.abort();
    if (f) Q.uploadCancel(f.uploadId);
    this.files = this.files.filter((x) => x.id !== id); this.render();
  },
  seq: 0,
  page: Math.random().toString(36).slice(2),     // with seq, an upload's id on the server

  render() {
    $("#chat-files").innerHTML = this.files.map((f) => `<span class="att ${f.error ? "err" : f.path ? "" : "busy"}" ${f.path ? `data-path="${esc(f.path)}"` : ""} title="${esc(f.error || f.path || "uploading…")}">`
      + `<span class="att-name">${esc(f.name)}</span><span class="att-size">${f.error ? "failed" : f.path ? this.size(f.size) : "…"}</span>`
      + `<span class="att-sync"></span><button class="att-x" data-att="${f.id}" aria-label="Remove ${esc(f.name)}">×</button></span>`).join("");
    this.showSync();
  },

  // The commit-and-push of each upload: a badge on its chips (here and in sent messages).
  sync: new Map(),       // path -> {state, message}
  polling: false,
  showSync() {
    const label = { syncing: "syncing…", synced: "✓ GitHub", local: "local", failed: "! not pushed", unknown: "? sync unknown" };
    document.querySelectorAll(".att[data-path]").forEach((el) => {
      const s = this.sync.get(el.dataset.path); if (!s) return;
      el.dataset.sync = s.state;
      const b = el.querySelector(".att-sync"); if (b) b.textContent = label[s.state] || "";
      el.title = `${el.dataset.path}\n${s.message || ""}`;
    });
  },
  watch(path, s) {
    this.sync.set(path, { ...s, at: Date.now() }); this.showSync();
    if (!this.polling) this.pollSync();
  },
  // Every 1.5 s (15 s while the tab is hidden), one request at a time, while an upload syncs.
  async pollSync() {
    this.polling = true;
    try {
      for (;;) {
        if (foreignProject) return;
        for (const [path, state] of this.sync) if (state.state === "syncing" && Date.now() - state.at >= 120000)
          this.sync.set(path, { state: "unknown", message: "Sync status is unknown. Check the GitHub menu." });
        this.showSync();
        await nap(1500, 15000);
        const open = [...this.sync].filter(([, s]) => s.state === "syncing").map(([p]) => p);
        if (!open.length) return;
        const r = await Q.uploadSync(open);
        if (r._status !== 200) continue;
        for (const p of open) {
          if (!r[p]) continue;
          this.sync.set(p, { ...r[p], at: this.sync.get(p).at });
          if (r[p].state === "failed") toast(`${p.split("/").pop()}: ${r[p].message}`, true);
        }
        this.showSync();
      }
    } finally { this.polling = false; }
  },

  size(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n >= 1024 ? Math.round(n / 1024) + " KB" : n + " B"; },

  async add(fileList) {
    for (const file of fileList) {
      const f = { id: ++this.seq, name: file.name || "pasted.png", size: file.size, path: null, error: null };
      f.uploadId = `${this.page}-${f.id}`;
      if (file.size > MAX_UPLOAD_SIZE) { toast(`${f.name} is larger than 25 MB.`); continue; }
      f.controller = new AbortController();
      this.files.push(f); this.render();
      try {
        const r = await uploadFile(file, "attachment", f.controller.signal, f.uploadId);
        if (!this.files.includes(f)) continue;
        f.path = r.path; f.size = r.size;
        if (r.sync) this.watch(r.path, r.sync);
      } catch (e) { f.error = String(e.message || e); }
      this.render();
    }
    $("#chat-input").focus();
  },

  // For the prompt: where the files are. Null when there are none.
  block() {
    const ok = this.files.filter((f) => f.path);
    if (!ok.length) return null;
    return "[Attached files] The author added these files to this message. They are saved in the project; "
      + "read them with your file tools:\n" + ok.map((f) => `- ${f.path} (${this.size(f.size)})`).join("\n");
  },
};
$("#chat-add").onclick = () => $("#chat-file").click();
$("#chat-file").onchange = (e) => { ATT.add([...e.target.files]); e.target.value = ""; };
$("#chat-files").addEventListener("click", (e) => {
  const b = e.target.closest("[data-att]"); if (!b) return;
  ATT.remove(+b.dataset.att);
});
// A pasted image (a screenshot) becomes a file too; pasted text stays text.
$("#chat-input").addEventListener("paste", (e) => {
  const files = [...(e.clipboardData && e.clipboardData.files) || []];
  if (!files.length) return;
  e.preventDefault();
  const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
  ATT.add(files.map((f) => (/^image\.\w+$/.test(f.name) || !f.name ? new File([f], `pasted-${stamp}.${(f.type.split("/")[1] || "png").replace("jpeg", "jpg")}`, { type: f.type }) : f)));
});
// Drag files onto the agent panel.
bindFileDrop($("#chat"), $("#chat-drop"), (files) => ATT.add(files));

async function chatSend() {
  if (C.job || C.sending) return;
  // Taken at once: a second Enter while this message saves, uploads or is being sent sends
  // nothing (it would only be told "still working on the previous message").
  C.sending = true;
  try { return await sendMessage(); } finally { C.sending = false; }
}
async function sendMessage() {
  const text = $("#chat-input").value.trim();
  if (!text) return;
  $("#slash-menu").hidden = true;
  const slash = /^\/([\w:.-]+)(?:\s+([\s\S]*))?$/.exec(text);
  if (slash && LOCAL[slash[1]]) {          // handled here, without running the agent
    $("#chat-input").value = "";
    chatAppend(esc(text), "msg user");
    return runLocal(slash[1], (slash[2] || "").trim());
  }
  const saved = await TABS.saveAll();
  if (!saved.ok) return toast(Tabs.unsavedWhy(saved));
  if (ATT.files.some((f) => !f.path && !f.error)) return toast("Wait until the files are uploaded.");
  const mode = $("#chat-mode").value;
  const mentions = parseMentions(text);
  const refs = await referenceBlock(mentions);
  if (slash) await loadCatalog();
  const attached = ATT.block(), sentFiles = ATT.files.filter((f) => f.path);
  const unsupported = sentFiles.find((f) => !(prov().input_types || ["text"]).includes(
    /\.(png|jpe?g|gif|webp)$/i.test(f.path) ? "image" : /\.pdf$/i.test(f.path) ? "pdf" : "text"));
  if (unsupported) return toast(`${provLabel()} does not support this attachment: ${unsupported.name}.`);
  const { prompt, scope } = ChatLogic.compose({ text, refs, attached, mentions, mode });
  const provider = C.provider;
  if (!P.projectKey) return toast("Wait for the project identity to load.");
  const r = await Q.agent({ prompt, session_id: C.session, mode, model: C.model, effort: C.effort, scope, provider,
    project_key: P.projectKey, attachments: sentFiles.map((f) => f.path) });
  if (r.error) return chatAppend(`<div class="err">${esc(r.error)}</div>`, "card");
  if ($("#chat-input").value.trim() === text) $("#chat-input").value = ""; updateScope();
  const sentIds = new Set(sentFiles.map((f) => f.id));
  ATT.files = ATT.files.filter((f) => !sentIds.has(f.id)); ATT.render();
  setTimeout(() => ATT.showSync(), 0);           // the badges on the sent message's chips
  const extra = [provLabel(), r.model, C.effort && "effort " + C.effort].filter(Boolean).join(" · ");
  chatAppend(`${mentionHtml(text)}${sentFiles.length ? `<span class="att-sent">${sentFiles.map((f) => `<span class="att" data-path="${esc(f.path)}"><span class="att-name">${esc(f.path.split("/").pop())}</span><span class="att-sync"></span></span>`).join("")}</span>` : ""}<span class="ctx">${esc(describeScope(mentions, mode))}${extra ? " · " + esc(extra) : ""}</span>`, "msg user");
  return followTurn(r.job, provider, mode);
}

// A turn's events into the panel, until it is done: the one just sent, or one already
// running when this page loaded (a reload), which is then shown and can be stopped.
// What each event shows is turn.js's; here it is drawn.
async function followTurn(job, provider, mode) {
  C.job = job; C.follow = true; C.followOpened = openAsked; C.stopping = false; setUndoBusy(true); AV.hidden = false; AH.clear();   // last turn's marks go
  C.editTurn = mode === "edit"; TABS.turnStarted();       // saves wait for the turn (tabs.js)
  $("#chat-send").textContent = "Stop"; $("#chat-send").classList.remove("primary");
  $("#chat-status").className = "status busy"; $("#chat-status").textContent = "working…";
  const t = Turn.create();
  const log = $("#chat-log");
  const status = (s) => { $("#chat-status").textContent = C.stopping ? "Stopping…" : s; };
  const messages = new Map();    // message id -> element
  const replies = Util.frameBatch((el, text) => { const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 60; el.innerHTML = renderMd(text); if (stick) log.scrollTop = log.scrollHeight; });
  const tools = new Map();       // tool id -> its row
  // Thinking and the text of a file being written stream into their own boxes, which close
  // when that step ends (click to open them again). Their text is drawn once per batch of
  // events, and the log follows only while you have not scrolled up to read something.
  let think = null;              // {box, pre, text} of the thinking being streamed
  const live = new Map();        // tool id -> {box, pre, text}
  const dirty = new Set();
  const draw = (stick) => {
    for (const x of dirty) {
      if (x.text.length > 240000) { x.text = x.text.slice(-200000); x.pre.textContent = "…" + x.text; }
      else if (x.text.length > x.drawn) x.pre.append(document.createTextNode(x.text.slice(x.drawn)));
      x.drawn = x.text.length;
      x.pre.scrollTop = x.pre.scrollHeight;
    }
    dirty.clear();
    AV.render();
    if (stick) log.scrollTop = log.scrollHeight;
  };
  const liveBox = (cls, summary) => {
    const box = document.createElement("details");
    box.className = cls; box.open = true;
    box.innerHTML = `<summary>${esc(summary)}</summary><pre></pre>`;
    log.appendChild(box);
    return { box, pre: box.querySelector("pre"), text: "", drawn: 0 };
  };
  const toolLabel = (name, summary) => `<span class="st">▸</span>${esc(name)} ${esc(summary || "")}`;
  // Newer models (Opus) think without sending the text: the box then counts the seconds.
  const tick = () => {
    if (think && t.thinking() !== null) think.box.querySelector("summary").textContent = `Thinking… ${t.thinking()}s`;
    status(t.status);
  };
  const timer = setInterval(tick, 1000);
  const show = (steps, stick) => {
    for (const s of steps) {
      if (s.mark === "clear-read") AH.clear("read");          // a read stays marked until the agent's next step
      else if (s.mark === "on") AH.on(s.path);
      else if (s.mark === "read") AH.set(s.path, "read", s.from, s.to);
      else if (s.view === "begin") AV.begin(s.id, s.name, s.path);
      else if (s.view === "feed") AV.feed(s.event);
      else if (s.view === "end") AV.end(s.id, s.ok);
      else if (s.save === "session") {
        // The session belongs to the provider that ran the turn, even if the menu changed since.
        store.set(conversationKey("session", provider), s.id);
        if (C.provider === provider) C.session = s.id;
      } else if (s.draw === "thinking") { think = liveBox("thinking", "Thinking…"); tick(); }
      else if (s.draw === "thinking-text") { think.text += s.text; dirty.add(think); }
      else if (s.draw === "thinking-end") {
        think.box.querySelector("summary").textContent = s.shown
          ? `Thought for ${s.secs}s` : `Thought for ${s.secs}s (this model does not show its thinking)`;
        if (!s.shown) think.pre.remove();
        think.box.open = false; think = null;
      } else if (s.draw === "message") {
        let el = messages.get(s.id);
        if (!el) messages.set(s.id, el = chatAppend("", "msg assistant"));
        replies.set(el, s.text);
        el.dataset.md = s.text;          // kept with the log: drawn again when it is restored
        if (stick) log.scrollTop = log.scrollHeight;
      } else if (s.draw === "tool") {
        // Claude Code announced this call already (tool_start): fill in its summary.
        const el = tools.get(s.id) || chatAppend("", "tool");
        el.innerHTML = toolLabel(s.name, s.summary);
        el.title = `${s.name} ${s.summary}`;
        tools.set(s.id, el);
      } else if (s.draw === "tool-result") {
        const el = tools.get(s.id);
        if (el) { el.querySelector(".st").textContent = s.ok ? "✓" : "✗"; if (!s.ok) { el.classList.add("err"); el.title += "\n" + s.preview; } }
      } else if (s.draw === "live") live.set(s.id, liveBox("live-file", s.name));
      else if (s.draw === "live-path") {
        live.get(s.id).box.querySelector("summary").textContent = `${s.name} ${s.path}`;
        const el = tools.get(s.id); if (el) el.innerHTML = toolLabel(s.name, s.path);
      } else if (s.draw === "live-text") { const x = live.get(s.id); x.text += s.text; dirty.add(x); }
      else if (s.draw === "live-close") {
        const x = live.get(s.id);
        if (x) { draw(false); x.box.open = false; live.delete(s.id); }
      } else if (s.draw === "build") showBuild(s.event.result);         // the agent compiled
      else if (s.draw === "error") chatAppend(`<div class="err">${esc(s.event.message)}</div>`, "card");
      else if (s.draw === "context") {                                   // grows with each message
        if (C.provider === provider) renderContext({ ...s.event, window: s.event.window || (provGet("context") || {}).window });
      } else if (s.draw === "done") {
        const e = s.event;
        renderTurnCard(e); loadAccount();
        if (e.context && C.provider === provider) {
          const c = { ...e.context, window: e.context.window || (provGet("context") || {}).window, session: e.session_id };
          provSet("context", c); renderContext(c);
        }
      }
    }
  };
  try {
    // The server out of reach (a sleeping laptop, a restart): ask again, less and less often.
    let attempts = 0, after = 0, done = false;
    while (!done) {
      // A hidden page asks every 5 s without the server waiting: a waiting request holds
      // one of the few connections the browser keeps to this server (see common.js).
      if (document.visibilityState === "hidden") await nap(5000, 5000);
      const d = await Q.events(job, after, document.visibilityState === "hidden" ? 0 : undefined);
      if (d._status !== 200) {
        if (!foreignProject && Turn.pollFailed(d._status, attempts) === "retry") {
          const retry = Turn.pollDelay(attempts++);
          status(`Connection interrupted; trying again in ${Math.round(retry / 1000)}s…`);
          await new Promise((res) => setTimeout(res, retry));
          continue;
        }
        chatAppend('<div class="warn">The server lost this turn, possibly during a restart. Its result is unknown. Check your files; Undo is unavailable for this turn.</div>', "card");
        break;
      }
      attempts = 0;
      const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
      show(t.feed(d.events), stick);
      draw(stick);
      status(t.status);
      after += d.events.length; done = d.done;
    }
  } finally {            // whatever happened above, the panel and saving work again
    clearInterval(timer);
    show(t.end(), false); replies.flush(); draw(false); AV.end(null, false); AH.on(null); AH.clear("read");
    C.job = null; C.editTurn = false; C.stopping = false; setUndoBusy(false);
    $("#chat-send").textContent = "Send"; $("#chat-send").classList.add("primary");
    $("#chat-status").className = "status"; $("#chat-status").textContent = "";
    saveChatLog();
    await poll();        // first take in the agent's changes (a file it changed under your edits gets the banner) …
    flushSaves();        // … then save what you typed meanwhile
    // Auto-compile the agent's changes too, unless its own last build already has them.
    if ($("#auto-compile").checked && Turn.needsCompile(t.changed, S.files, S.pdfMtime))
      scheduleCompile(null);     // the document the agent edited, whichever tab is open
  }
}

// Usage limits as reported by Claude Code's rate_limit_event (utilization 0–1 per window).
let lastRate = null;
function renderQuota(rate) {
  const q = $("#quota");
  lastRate = rate;
  const hidden = store.get("chat.quotaHidden", false);
  q.classList.toggle("collapsed", hidden);
  const refresh = `<button class="tiny icon ghost" id="quota-refresh" title="Check usage now (a tiny Haiku call, which counts toward your plan's usage limits)">${icon("refresh")}</button>`;
  const hide = `<button class="tiny icon ghost" id="quota-toggle" title="Hide usage limits">${icon("up")}</button>`;
  if (!rate || !rate.unifiedWindows) {
    q.innerHTML = hidden
      ? `<span class="note q-sum" id="quota-toggle" title="Show usage limits">Usage: not checked yet <span class="q-open">${icon("down")}</span></span>`
      : `<span class="note">Usage limits: not checked yet ${refresh}${hide}</span>`;
    return;
  }
  const prev = store.get("chat.rate", null);
  if (!prev || !prev.at || (rate.at || 0) >= prev.at) store.set("chat.rate", rate);
  const fmtReset = (ts) => {
    if (!ts) return "";
    const d = new Date(ts * 1000), now = new Date();
    const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return d.toDateString() === now.toDateString() ? time : d.toLocaleDateString([], { weekday: "short" }) + " " + time;
  };
  const names = { five_hour: "5h", seven_day: "7d", seven_day_opus: "7d Opus", seven_day_sonnet: "7d Sonnet" };
  if (hidden) {                           // one line: "Usage · 5h 41% left · 7d 90% left ▾"
    const limited = rate.status && rate.status !== "allowed";
    const parts = Object.entries(rate.unifiedWindows).map(([k, w]) => {
      const used = Math.max(0, Math.min(1, w.utilization || 0));
      const cls = used >= 0.9 ? "err" : used >= 0.7 ? "warn" : "";
      return `<span class="${cls}">${esc(names[k] || k)} ${100 - Math.round(used * 100)}% left</span>`;
    });
    q.innerHTML = `<span class="note q-sum" id="quota-toggle" title="Show usage limits">`
      + (limited ? `<span class="err">Rate limited until ${esc(fmtReset(rate.resetsAt))}</span>` : "Usage · " + parts.join(" · "))
      + ` <span class="q-open">${icon("down")}</span></span>`;
    return;
  }
  let h = "";
  for (const [k, w] of Object.entries(rate.unifiedWindows)) {
    const used = Math.max(0, Math.min(1, w.utilization || 0)), pct = Math.round(used * 100);
    const cls = used >= 0.9 ? "err" : used >= 0.7 ? "warn" : "";
    h += `<span>${esc(names[k] || k)}</span><div class="bar" title="${pct}% used"><i class="${cls}" style="width:${Math.max(pct, 1)}%"></i></div>
      <span class="num">${100 - pct}% left · resets ${esc(fmtReset(w.resetsAt))}</span>`;
  }
  const age = rate.at ? new Date(rate.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "?";
  if (rate.status && rate.status !== "allowed")
    h += `<span class="note err">Rate limited (${esc(rate.rateLimitType || "")}) until ${esc(fmtReset(rate.resetsAt))}</span>`;
  else
    h += `<span class="note">as of ${esc(age)} · updated by each message and ${refresh}${hide}</span>`;
  q.innerHTML = h;
  q.title = "Claude usage limits reported by Claude Code. Usage from other sessions (e.g. the terminal) shows up after the next message sent from this panel.";
}

// `auto`: the check on page load, which stays quiet when refused (the account line says why).
async function checkUsage(auto = false) {
  const provider = C.provider, seq = ++providerSeq.usage;
  const b = $("#quota-refresh");
  if (b) { b.disabled = true; b.textContent = "…"; }
  const r = await Q.usage(provider);
  if (provider !== C.provider || seq !== providerSeq.usage) return;
  renderQuota(r.rate || store.get("chat.rate", null));
  if (r.error && !auto) toast(r.error, true);
}
$("#quota").addEventListener("click", (e) => {
  if (e.target.id === "quota-refresh") return checkUsage();
  if (e.target.closest("#quota-toggle")) {
    store.set("chat.quotaHidden", !store.get("chat.quotaHidden", false));
    renderQuota(lastRate);
  }
});


function setUndoBusy(busy) {
  for (const button of document.querySelectorAll("[data-undo]")) {
    if (busy && !button.disabled) { button.dataset.turnBusy = "1"; button.disabled = true; button.title = "Wait until the current turn ends."; }
    else if (!busy && button.dataset.turnBusy) { delete button.dataset.turnBusy; button.disabled = false; button.title = ""; }
  }
}
function renderTurnCard(e) {
  const result = Turn.result(e);
  let h = result.outcome === "stopped" ? '<div class="meta">Stopped</div>' : "";
  if (e.changed && e.changed.length) {
    h += `<div class="row"><b>Changed ${e.changed.length} file${e.changed.length > 1 ? "s" : ""}</b>
      <button class="tiny sp" data-undo="${e.turn}">Undo this turn</button>
      <button class="tiny" data-compile="1">Compile</button></div>`;
    for (const c of e.changed) {
      const add = (c.diff.match(/^\+(?!\+\+)/gm) || []).length, del = (c.diff.match(/^-(?!--)/gm) || []).length;
      const label = S.files.some((f) => f.path === c.path)
        ? `<a data-file="${esc(c.path)}" data-line="${(c.diff.match(/^@@ -\d+(?:,\d+)? \+(\d+)/m) || [])[1] || ""}">${esc(c.path)}</a>`
        : `<span>${esc(c.path)}</span>`;
      h += `<div class="row">${label}
        <span class="add">+${add}</span> <span class="del">−${del}</span>${c.created ? " (new)" : c.deleted ? " (deleted)" : ""}
        <button class="tiny sp" data-toggle="1">diff</button></div><pre hidden>${diffHtml(c.diff)}</pre>`;
    }
  } else if (!e.is_error && e.exit === 0) {
    h += `<div class="meta">No file changes detected.</div>`;
  }
  if (e.snapshot_issues && e.snapshot_issues.length)
    h += `<div class="warn">Change detection is incomplete: ${esc(e.snapshot_issues.join("; "))}.</div>`;
  if (e.undo_unavailable && e.undo_unavailable.length)
    h += `<div class="warn">Undo is unavailable for files exceeding the snapshot limit: ${esc(e.undo_unavailable.join(", "))}.</div>`;
  const writes = ["Edit", "Write", "MultiEdit", "NotebookEdit"];
  const denied = [...new Set(e.denials || [])];
  if (e.scope && denied.some((d) => writes.includes(d)))
    h += `<div class="warn">Blocked edits outside ${esc(e.scope.join(", "))}. Remove the @-mentions to let the agent change other files.</div>`;
  if (e.reverted && e.reverted.length)
    h += `<div class="warn">${e.scope ? "Undid changes outside the @-mentioned files" : "Ask mode is read-only: undid its changes to"}: ${esc(e.reverted.join(", "))}.</div>`;
  // Other refusals, mostly shell commands (which a turn here may run only where the
  // project's .claude/settings.json allows them). They do not undo the changes above.
  const others = (e.denied || denied.map((tool) => ({ tool, what: "" })))
    .filter((d) => !(e.scope && writes.includes(d.tool)));
  if (others.length) {
    const list = [...new Set(others.map((d) => d.what ? `${d.tool}: ${d.what}` : d.tool))];
    h += `<div class="note">Skipped ${others.length === 1 ? "a step" : others.length + " steps"} the panel does not allow `
      + `(the file changes above are not affected):<ul>${list.map((x) => `<li><code>${esc(x.slice(0, 160))}</code></li>`).join("")}</ul></div>`;
  }
  if (e.out_of_scope && e.out_of_scope.length)
    h += `<div class="warn">Changes exceeded this turn's permissions and could not be restored: ${esc(e.out_of_scope.join(", "))}.</div>`;
  if (e.new_metadata && e.new_metadata.length)
    h += `<div class="warn">New repository or agent metadata appeared during this turn: ${esc(e.new_metadata.join(", "))}. Nothing was removed; delete it if you did not create it.</div>`;
  if (result.error) h += `<div class="err">${esc(result.error)}</div>`;
  const k = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? Math.round(n / 1e3) + "k" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n));
  const tokens = e.usage && (e.usage.in || e.usage.out) ? `${k(e.usage.in || 0)} in / ${k(e.usage.out || 0)} out tokens` : "";
  // With the claude.ai login a turn costs no money: it counts against the plan's usage
  // limits (the bars above), so no price is shown. A price is shown only for API billing.
  const billing = e.billing === "subscription" ? "counts toward your plan's usage limits" : "";
  // Claude Code reports a list price even for the claude.ai login: a price is shown only
  // when the server says the turn was billed per token.
  const kind = (P.byId[e.provider] || {}).kind;
  const cost = e.cost && (e.billing === "api" || (kind && kind !== "claude")) ? "$" + e.cost.toFixed(3) : "";
  const meta = [e.duration ? (e.duration / 1000).toFixed(1) + "s" : "", cost, tokens, billing].filter(Boolean).join(" · ");
  if (meta) h += `<div class="meta">${esc(meta)}</div>`;
  chatAppend(h, "card");
  setUndoBusy(!!C.job);
}

$("#chat-log").addEventListener("click", async (ev) => {
  const a = ev.target.closest("a[data-file]");
  if (a) return openFile(a.dataset.file, a.dataset.line ? +a.dataset.line : undefined);
  const tg = ev.target.closest("[data-toggle]");
  if (tg) { const pre = tg.closest(".row").nextElementSibling; pre.hidden = !pre.hidden; return; }
  if (ev.target.closest("[data-compile]")) return compile();
  const u = ev.target.closest("[data-undo]");
  if (u) {
    if (C.job) return toast("Wait until the current turn ends before Undo.");
    const saved = await TABS.saveAll(); if (!saved.ok) return toast(Tabs.unsavedWhy(saved));
    const r = await Q.undo(+u.dataset.undo);
    if (r._status === 0 || r.busy) return toast("Not undone: " + r.error);      // the button stays, to try again
    if (r.error) { u.textContent = r.error === "unknown turn" ? "undo unavailable (an old turn, or the server restarted)" : r.error; u.disabled = true; return; }
    u.textContent = `undone (${(r.restored || []).length})` + ((r.skipped || []).length ? `; kept ${r.skipped.length} unavailable or changed since` : "");
    u.disabled = true; saveChatLog(); AH.clear("changed"); await poll();
  }
});

/* ------------------------------------------------------------------ slash commands */
// Each message runs the provider non-interactively (`claude -p`, `codex exec`, or one API
// conversation), where interactive commands such as /model do not exist. The panel
// implements those itself. With Claude Code every other /command (skills, /compact,
// /context, the project's own commands) goes to Claude Code as the first thing in the
// prompt, which is where Claude Code looks for it; other providers get it as plain text.
const LOCAL = {
  help: { args: "", desc: "List the commands you can use here" },
  provider: { args: "[name]", desc: "Show or switch the AI that runs the agent" },
  model: { args: "[name]", desc: "Show or set the model for the next messages" },
  effort: { args: "[level]", desc: "Show or set the effort level" },
  skills: { args: "", desc: "List the skills Claude Code can use in this project" },
  mode: { args: "edit|ask", desc: "Edit (may change files) or Ask (read-only)" },
  clear: { args: "", desc: "Start a new conversation" },
  new: { args: "", desc: "Start a new conversation" },
};
let catalog = null;          // {skills, commands, terminal_only} from /api/agent/commands

async function loadCatalog(refresh = false) {
  if (catalog && !refresh) return catalog;
  const provider = C.provider, seq = ++providerSeq.catalog;
  const r = await Q.commands(provider, refresh);
  if (provider !== C.provider || seq !== providerSeq.catalog) return null;
  if (r._status === 200) catalog = r;
  return catalog;
}
function sysNote(html) { chatAppend(html, "msg sys"); saveChatLog(); }
function chipList(names) {
  return names.map((n) => `<a class="chip-cmd" data-insert="/${esc(n)} ">/${esc(n)}</a>`).join(" ");
}
function settingsLine() {
  const p = prov();
  return `provider <b>${esc(provLabel())}</b> · model <b>${esc(C.model || p.default_model || "default")}</b>`
    + (p.efforts && p.efforts.length ? ` · effort <b>${esc(C.effort || "default")}</b>` : "");
}

async function runLocal(name, arg) {
  if (name === "help") {
    const rows = Object.entries(LOCAL).filter(([n]) => n !== "new")
      .map(([n, c]) => `<code>/${n}${c.args ? " " + esc(c.args) : ""}</code> — ${esc(c.desc)}`).join("\n");
    return sysNote(`<b>Commands handled by this panel</b>\n${rows}\n\nWith Claude Code, any other <code>/command</code> — a skill, <code>/compact</code>, <code>/context</code>, or the project's own commands — is passed to Claude Code. Type <code>/</code> to see them all.`);
  }
  if (name === "provider") {
    if (!arg) {
      const rows = P.list.map((p) => `${p.available ? "" : '<span class="note">'}<code>${esc(p.id)}</code> ${esc(p.label)}${p.available ? "" : " — " + esc(p.reason || "unavailable") + "</span>"}`).join("\n");
      return sysNote(`Current: ${settingsLine()}\n${rows}\n${chipList(P.list.filter((p) => p.available).map((p) => "provider " + p.id))}`);
    }
    return setProvider(arg);
  }
  if (name === "model") {
    const models = prov().models || [];
    if (!arg) return sysNote(`Current: ${settingsLine()}\nUsage: <code>/model &lt;name&gt;</code> (any model name ${esc(provLabel())} accepts). ${chipList(["default", ...models].map((m) => "model " + m))}`);
    C.model = arg === "default" ? null : arg; provSet("model", C.model);
    return sysNote(`Model for the next messages: <b>${esc(C.model || prov().default_model || "default")}</b>`);
  }
  if (name === "effort") {
    const efforts = prov().efforts || [];
    if (!efforts.length) return sysNote(`${esc(provLabel())} has no effort setting.`);
    if (!arg) return sysNote(`Current: ${settingsLine()}\nUsage: <code>/effort &lt;level&gt;</code>. ${chipList(["default", ...efforts].map((e) => "effort " + e))}`);
    if (arg !== "default" && !efforts.includes(arg)) return sysNote(`<span class="err">Effort must be one of: default, ${efforts.join(", ")}</span>`);
    C.effort = arg === "default" ? null : arg; provSet("effort", C.effort);
    return sysNote(`Effort for the next messages: <b>${esc(C.effort || "default")}</b>`);
  }
  if (name === "skills") {
    if (!prov().skills) return sysNote(`The editor has no Skills catalog integration for ${esc(provLabel())}. Native CLI Skills may still be available.`);
    sysNote("Loading skills…");
    const cat = await loadCatalog(arg === "refresh");
    const last = $("#chat-log").lastElementChild;
    if (last && last.textContent === "Loading skills…") last.remove();
    if (!cat) return sysNote(`<span class="err">Could not ask Claude Code for its skills.</span>`);
    return sysNote(`<b>${cat.skills.length} skills</b> (click one to use it):\n${chipList(cat.skills)}\n\n<small>Refresh with <code>/skills refresh</code>.</small>`);
  }
  if (name === "mode") {
    if (!["edit", "ask"].includes(arg)) return sysNote(`Mode is <b>${$("#chat-mode").value}</b>. Usage: <code>/mode edit</code> or <code>/mode ask</code>.`);
    $("#chat-mode").value = arg; store.set("chat.mode", arg);
    return sysNote(`Mode: <b>${arg === "edit" ? "Edit (may change files)" : "Ask (read-only)"}</b>`);
  }
  if (name === "clear" || name === "new") return $("#chat-new").onclick();
}

// Switch provider. Each provider keeps its own conversation, model and effort.
function setProvider(id, quiet) {
  const p = P.byId[id];
  if (!p) return sysNote(`<span class="err">Unknown provider: ${esc(id)}. Try <code>/provider</code>.</span>`);
  if (C.job) return toast("Wait for the current turn to finish.");
  C.provider = id; store.set("chat.provider", id); loadProvider(); loadAccount();
  catalog = null;
  $("#chat-provider").value = id;
  $("#quota").hidden = !p.usage_limits;
  if (p.usage_limits) renderQuota(p.rate || store.get("chat.rate", null));
  if (!quiet) sysNote(p.available
    ? `Now using <b>${esc(p.label)}</b> (${settingsLine()}). It continues its own conversation; <b>New chat</b> starts over.`
    : `<span class="err">${esc(p.label)} is not available: ${esc(p.reason || "")}</span>`);
}
function renderProviders(info) {
  if (!bindProject(info.project_key)) return;
  P.projectKey = projectKey;
  P.list = info.providers || [];
  P.byId = Object.fromEntries(P.list.map((p) => [p.id, p]));
  // A server started before this page's code was updated sends no provider list. Hide the
  // empty menu and say how to get the new server, instead of showing a blank box.
  $("#chat-provider").hidden = !P.list.length;
  if (!P.list.length) {
    chatAppend(`<div class="warn">The prism-local server is older than this page. Close every
      Prism page for this project, wait about 10 seconds, and open it again to restart it.</div>`, "card");
    return;
  }
  $("#chat-provider").innerHTML = P.list.map((p) =>
    `<option value="${esc(p.id)}"${p.available ? "" : " disabled"} title="${esc(p.reason || "")}">${esc(p.label)}${p.available ? "" : " (not set up)"}</option>`).join("");
  let id = C.provider;
  if (!P.byId[id] || !P.byId[id].available) id = info.default;
  if (!P.byId[id] || !P.byId[id].available) id = (P.list.find((p) => p.available) || P.list[0] || {}).id;
  if (id) setProvider(id, true);
  if (info.config_error) chatAppend(`<div class="err">Agent settings: ${esc(info.config_error)}</div>`, "card");
}
$("#chat-provider").onchange = (e) => setProvider(e.target.value);


/* Completion menu: "/" at the start of the message lists commands and skills;
   "@" anywhere lists the editor selection, the open file and the project files. */
const SM = { items: [], sel: 0, kind: null, at: 0 };
function slashItems(q) {
  const seen = new Set(), out = [];
  const add = (name, desc, kind) => {
    if (!seen.has(name) && name.startsWith(q)) { seen.add(name); out.push({ label: "/" + name, args: (LOCAL[name] || {}).args, desc, kind, insert: "/" + name + " " }); }
  };
  for (const [n, c] of Object.entries(LOCAL)) add(n, c.desc, "panel");
  if (catalog) {
    for (const s of catalog.skills) add(s, "", "skill");
    const hidden = new Set([...(catalog.terminal_only || []), "skills", "model", "effort", "clear"]);
    for (const c of catalog.commands) if (!hidden.has(c) && !c.startsWith("__")) add(c, "", "Claude Code");
  }
  return out.slice(0, 60);
}
function mentionItems(q) {
  const out = [], ql = q.toLowerCase();
  const sel = selectionMention(), t = activeTab();
  if (sel && ("selection".startsWith(ql) || sel.token.slice(1).toLowerCase().includes(ql)))
    out.push({ label: sel.token, desc: "the text selected in the editor", kind: "selection", insert: sel.token, snip: sel.text });
  if (t && t.path.toLowerCase().includes(ql))
    out.push({ label: "@" + t.path, desc: "open in the editor", kind: "file", insert: "@" + t.path });
  for (const f of S.files) {
    if (f.editable === false || (t && f.path === t.path)) continue;
    if (f.path.toLowerCase().includes(ql)) out.push({ label: "@" + f.path, desc: "", kind: "file", insert: "@" + f.path });
  }
  return out.slice(0, 60);
}
function renderPicker() {
  const m = $("#slash-menu"), inp = $("#chat-input");
  const v = inp.value, caret = inp.selectionStart ?? v.length;
  const slash = /^\/([\w:.-]*)$/.exec(v);
  const at = /(^|\s)@([^\s@]*)$/.exec(v.slice(0, caret));
  if (slash) {
    SM.kind = "slash";
    if (!catalog) loadCatalog().then(() => { if (/^\/[\w:.-]*$/.test(inp.value)) renderPicker(); });
    SM.items = slashItems(slash[1]);
  } else if (at) {
    SM.kind = "mention"; SM.at = caret - at[2].length - 1;
    SM.items = mentionItems(at[2]);
  } else { m.hidden = true; return; }
  SM.sel = Math.min(SM.sel, Math.max(0, SM.items.length - 1));
  if (!SM.items.length) { m.hidden = true; return; }
  m.innerHTML = SM.items.map((it, i) => `<div class="sm-item${i === SM.sel ? " sel" : ""}" data-i="${i}">
    <span class="sm-name">${esc(it.label)}${it.args ? ` <i>${esc(it.args)}</i>` : ""}</span>
    <span class="sm-desc">${esc(it.desc)}</span><span class="sm-kind">${esc(it.kind)}</span></div>`).join("")
    + (SM.kind === "slash" && !catalog ? `<div class="sm-more">loading skills…</div>` : "");
  m.hidden = false;
  const sel = m.querySelector(".sel"); if (sel) sel.scrollIntoView({ block: "nearest" });
}
function acceptPick(i) {
  const it = SM.items[i]; if (!it) return;
  $("#slash-menu").hidden = true;
  if (SM.kind === "slash") { $("#chat-input").value = it.insert; $("#chat-input").focus(); updateScope(); }
  else insertMention(it.insert, it.snip, SM.at);
}
$("#chat-input").addEventListener("input", () => { SM.sel = 0; renderPicker(); });
$("#chat-input").addEventListener("keydown", (e) => {
  if ($("#slash-menu").hidden) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault(); e.stopImmediatePropagation();
    SM.sel = (SM.sel + (e.key === "ArrowDown" ? 1 : -1) + SM.items.length) % SM.items.length; renderPicker();
  } else if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && !e.isComposing)) {
    e.preventDefault(); e.stopImmediatePropagation(); acceptPick(SM.sel);
  } else if (e.key === "Escape") { e.stopImmediatePropagation(); $("#slash-menu").hidden = true; }
});
$("#slash-menu").addEventListener("mousedown", (e) => {
  const it = e.target.closest(".sm-item"); if (it) { e.preventDefault(); acceptPick(+it.dataset.i); }
});
$("#chat-input").addEventListener("blur", () => setTimeout(() => { $("#slash-menu").hidden = true; }, 150));
$("#chat-log").addEventListener("click", (e) => {
  const a = e.target.closest("[data-insert]"); if (!a) return;
  const v = a.dataset.insert;
  // "/model opus" style chips run at once; skill chips are filled in for you to finish.
  const local = /^\/(model|effort|provider)\s+([\s\S]+)$/.exec(v);
  if (local) runLocal(local[1], local[2].trim());
  else { $("#chat-input").value = v; $("#chat-input").focus(); updateScope(); }
});

async function stopTurn() {
  if (!C.job || C.stopping) return;
  C.stopping = true; $("#chat-send").textContent = "Stopping…"; $("#chat-status").textContent = "Stopping…";
  const job = C.job, r = await Q.stop(job);
  if (C.job === job && r._status !== 200) {
    C.stopping = false; $("#chat-send").textContent = "Stop";
    toast("Could not stop the agent: " + (r.error || r._status));
  }
}
$("#chat-send").onclick = () => { if (C.job) stopTurn(); else chatSend(); };
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); chatSend(); }
});
$("#chat-new").onclick = () => {
  if (C.job) return;
  C.session = null; provSet("session", null);
  const c = provGet("context");                                    // a new chat starts empty (keep the window size)
  if (c) provSet("context", { window: c.window });
  renderContext(null);
  $("#chat-log").innerHTML = ""; chatIntro(); saveChatLog();
};
function askAboutSelection() {
  chatHidden(false);
  const sel = selectionMention();
  if (sel) insertMention(sel.token, sel.text);
  else { const t = activeTab(); if (t) insertMention("@" + t.path); else $("#chat-input").focus(); }
}
cm.setOption("extraKeys", { ...cm.getOption("extraKeys"), "Cmd-L": askAboutSelection, "Ctrl-L": askAboutSelection });
// The providers and the project's identity, without which nothing can be sent: asked for
// again, less and less often, until the server answers.
async function loadAgentInfo() {
  for (let wait = 1000; ; wait = Math.min(wait * 2, 30000)) {
    const r = await Q.info();
    if (r._status !== 0) {
      if (wait > 1000) { $("#chat-status").className = "status"; $("#chat-status").textContent = ""; }
      return r;
    }
    $("#chat-status").className = "status err";
    $("#chat-status").textContent = "server not reachable; the agent is not ready yet";
    await nap(wait, wait);
  }
}
{
  chatIntro();
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  renderQuota(store.get("chat.rate", null));
  loadAgentInfo().then((r) => {
    renderProviders(r);
    if (r.active && !C.job && r.project_key === P.projectKey) {
      sysNote("The agent was already working when this page loaded: its turn follows, and <b>Stop</b> ends it.");
      followTurn(r.active.job, r.active.provider, r.active.mode);
    }
    const p = prov();
    if (!p.available) return chatAppend(`<div class="err">${esc(p.reason || "No AI provider is set up.")}</div>`, "card");
    const known = p.rate || store.get("chat.rate", null);
    if (p.usage_limits && (!known || !known.at || Date.now() / 1000 - known.at > 600)) checkUsage(true);
  });
}

/* ------------------------------------------------------------------ start */
(async function init() {
  try {
    loadSnippets();
    while (!S.ready && !foreignProject) {
      await poll();
      if (!S.ready && !foreignProject) await nap(1000, 1000);
    }
    if (foreignProject) return;
    const sess = store.get("session", null);
    const exists = (p) => S.files.some((f) => f.path === p);
    const paths = sess && sess.tabs ? sess.tabs.filter((p) => exists(p) && p !== sess.active) : [];
    if (sess && sess.active && exists(sess.active)) paths.push(sess.active);
    paths.forEach(fileRequest);                         // downloaded together, opened in order
    let restoring = openAsked;
    for (const p of paths) {
      // A file you opened meanwhile stays in front: the rest only come back as tabs.
      if (openAsked !== restoring) { await loadTab(p); continue; }
      const mine = openAsked + 1;
      await openFile(p);
      if (openAsked === mine) restoring = mine;
    }
    S.restoring = false; renderTabs(); persistSession();
    // No saved session: open the file the server builds (prism.json's "main", or the guess).
    if (!S.active && exists(BUILD.main)) await openFile(BUILD.main);
    renderProblems();
  } finally {
    poll.start();          // whatever failed above, the editor keeps following the project
  }
})();
updateScope();
