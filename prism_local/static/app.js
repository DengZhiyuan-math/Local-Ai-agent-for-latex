/* prism-local — editor client. Talks only to prism_local/server.py.
   Loaded after common.js (helpers, theme) and pdfview.js (PV). */
"use strict";

/* ------------------------------------------------------------------ state */
const S = {
  files: [], order: [], tabs: [], active: null, symbols: { labels: [], bibkeys: [], outline: [], macros: [] },
  diagnostics: [], pdfMtime: null, building: false,
};

/* ------------------------------------------------------------------ theme */
$("#btn-theme").onclick = () => {
  const cur = store.get("theme", null);
  const next = cur === null ? "dark" : cur === "dark" ? "light" : null;
  store.set("theme", next); applyTheme(next);
};

/* ------------------------------------------------------------------ home */
// The Home page (hub.py) lists all projects. Its tab calls itself "prism-home", so this
// finds and reuses it when it opened us; the server starts the Home page if needed.
$("#btn-home").onclick = async () => {
  const w = window.open("", "prism-home");
  let fresh = true;
  try { fresh = !!w && w.location.href === "about:blank"; } catch { fresh = false; }
  if (w && fresh) w.document.body.innerHTML = '<p style="font:15px system-ui;color:#6f6a60;margin:40vh auto;text-align:center">Opening your projects…</p>';
  const r = await api("/api/home", {}).catch(() => ({ error: "server not reachable" }));
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
    Tab: (ed) => ed.somethingSelected() ? ed.indentSelection("add") : ed.replaceSelection("  "),
  },
});
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
cm.getWrapperElement().style.display = "none";
cm.on("change", () => { const t = activeTab(); if (t) { renderTabs(); scheduleSave(t); } });
cm.on("inputRead", (ed, ch) => { if (/[{,]/.test(ch.text.join("")) || /\\[A-Za-z]*$/.test(lineBefore(ed))) showCompletions(ed, false); });

function modeFor(path) { return path.endsWith(".md") ? "markdown" : "stex"; }
function activeTab() { return S.tabs.find((t) => t.path === S.active) || null; }
function isDirty(t) { return !t.doc.isClean(t.gen); }

async function openFile(path, line) {
  let t = S.tabs.find((x) => x.path === path);
  if (!t) {
    const r = await api("/api/file?path=" + encodeURIComponent(path));
    if (r._status !== 200) return toast(`Cannot open ${path}: ${r.error || r._status}`);
    const doc = CodeMirror.Doc(r.content, modeFor(path));
    t = { path, doc, mtime: r.mtime, gen: doc.changeGeneration(true) };
    S.tabs.push(t);
  }
  S.active = path;
  cm.swapDoc(t.doc);
  AH.apply(path);
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
  if (t && isDirty(t) && !t.conflict) await saveTab(t);      // autosave may still be pending
  if (t && isDirty(t) && !confirm(`${path} has unsaved changes. Close anyway?`)) return;
  S.tabs = S.tabs.filter((x) => x.path !== path);
  if (S.active === path) {
    const next = S.tabs[S.tabs.length - 1];
    if (next) return openFile(next.path);
    S.active = null; cm.getWrapperElement().style.display = "none"; $("#empty-editor").hidden = false;
  }
  renderTabs(); renderTree(); persistSession();
}

function renderTabs() {
  $("#tabs").innerHTML = S.tabs.map((t) => `
    <div class="tab ${t.path === S.active ? "active" : ""} ${isDirty(t) ? "dirty" : ""}" data-path="${esc(t.path)}" title="${esc(t.path)}">
      <span class="name">${esc(t.path.split("/").pop())}</span><span class="close" data-close="${esc(t.path)}">×</span>
    </div>`).join("");
  AH.refresh();
}
$("#tabs").addEventListener("click", (e) => {
  const c = e.target.closest("[data-close]"); if (c) { e.stopPropagation(); return closeTab(c.dataset.close); }
  const t = e.target.closest(".tab"); if (t) openFile(t.dataset.path);
});
$("#tabs").addEventListener("auxclick", (e) => { const t = e.target.closest(".tab"); if (t && e.button === 1) closeTab(t.dataset.path); });

function persistSession() { store.set("session", { tabs: S.tabs.map((t) => t.path), active: S.active }); }

/* ------------------------------------------------------------------ save & external changes */
// One save per tab at a time: a second save waits for the first, so two writes never
// race on the server (the second would otherwise look like a change made on disk).
// While the agent works in Edit mode, nothing is written: your edits would end up in its
// turn's diff (and its Undo). They are saved when the turn ends; a file the agent changed
// meanwhile gets the conflict banner instead.
function saveTab(t, force = false) {
  if (C.editTurn) {
    if (!C.heldNote) { C.heldNote = true; toast("Your edits are saved when the agent's turn ends."); }
    return Promise.resolve(false);
  }
  const run = () => saveTabNow(t, force);
  t.saving = (t.saving || Promise.resolve()).then(run, run);
  return t.saving;
}
async function saveTabNow(t, force) {
  if (!isDirty(t) && !force) return true;
  const gen = t.doc.changeGeneration();
  const r = await api("/api/file", { path: t.path, content: t.doc.getValue(), base_mtime: t.mtime, force });
  if (r._status === 409 && r.conflict) {
    t.conflict = "disk"; showBanner(t);
    toast(`${t.path} changed on disk — not saved. Resolve in the banner.`);
    return false;
  }
  if (r._status !== 200) { toast(`Save failed: ${r.error || r._status}`); return false; }
  t.mtime = r.mtime; t.gen = gen; t.conflict = null;
  renderTabs(); showBanner(t); scheduleSymbols();
  return true;
}
async function saveActive() {
  const t = activeTab(); if (!t) return;
  clearTimeout(t.saveTimer);
  const ok = await saveTab(t);
  if (ok && $("#auto-compile").checked) compile();
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
  if ((await saveTab(t)) && $("#auto-compile").checked) scheduleCompile();
}
function flushSaves() {
  for (const t of S.tabs) { clearTimeout(t.saveTimer); autosave(t); }
}
window.addEventListener("blur", flushSaves);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flushSaves(); });

// Auto-compile: shortly after the last edit is saved; if a build is running, once it ends.
let compileTimer = null;
function scheduleCompile() {
  clearTimeout(compileTimer);
  compileTimer = setTimeout(function go() {
    if (S.building) { compileTimer = setTimeout(go, 1000); return; }
    compile();
  }, AUTOCOMPILE_MS);
}
async function saveAll() {
  let ok = true;
  for (const t of S.tabs) if (isDirty(t)) ok = (await saveTab(t)) && ok;
  return ok;
}

// `discard`: the author chose the disk version over unsaved edits (the conflict banner).
async function reloadFromDisk(t, discard = false) {
  const r = await api("/api/file?path=" + encodeURIComponent(t.path));
  if (r._status !== 200) return;
  if (!discard && isDirty(t)) {             // typed into while the file was fetched
    if (t.conflict !== "disk") { t.conflict = "disk"; showBanner(t); }
    return;
  }
  if (r.content === t.doc.getValue()) {     // same text (a save of ours, a touch): keep cursor and undo
    t.mtime = r.mtime; t.gen = t.doc.changeGeneration(true); t.conflict = null;
    renderTabs(); showBanner(t);
    return;
  }
  const cur = t.doc.getCursor(), scroll = t === activeTab() ? cm.getScrollInfo() : null;
  t.doc.setValue(r.content);
  AH.apply(t.path);                          // setValue drops line classes
  t.doc.setCursor(cur);
  if (scroll) cm.scrollTo(scroll.left, scroll.top);
  t.mtime = r.mtime; t.gen = t.doc.changeGeneration(true); t.conflict = null;
  renderTabs(); showBanner(t);
}

function showBanner(t) {
  const b = $("#banner");
  if (!t || t !== activeTab() || !t.conflict) { b.hidden = true; return; }
  b.hidden = false;
  b.innerHTML = `<b>${esc(t.path)}</b> was changed on disk (e.g. by Claude Code) while you have unsaved edits.
    <button id="bn-reload">Load disk version (discard mine)</button>
    <button id="bn-keep">Keep mine (overwrite disk)</button>
    <button id="bn-diff">Show git diff</button>`;
  $("#bn-reload").onclick = () => reloadFromDisk(t, true);
  $("#bn-keep").onclick = () => saveTab(t, true);
  $("#bn-diff").onclick = () => showDiff(t.path);
}

/* ------------------------------------------------------------------ file tree & outline */
function renderTree() {
  const groups = new Map();
  const rank = (p) => { const i = S.order.indexOf(p); return i < 0 ? 999 : i; };
  const files = [...S.files].sort((a, b) => rank(a.path) - rank(b.path) || a.path.localeCompare(b.path));
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
    if (d) html += `<li class="folder">${esc(d)}/</li>`;
    for (const f of groups.get(d)) {
      html += `<li data-path="${esc(f.path)}" class="${f.path === S.active ? "active" : ""}" title="${esc(f.path)}">
        ${esc(f.path.split("/").pop())}${f.git ? `<span class="git" title="git status">${esc(f.git)}</span>` : ""}</li>`;
    }
  }
  $("#tree").innerHTML = html;
  AH.refresh();
}
$("#tree").addEventListener("click", (e) => { const li = e.target.closest("li[data-path]"); if (li) openFile(li.dataset.path); });

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
async function loadSymbols() {
  const r = await api("/api/symbols").catch(() => null);
  if (r && r._status === 200) { S.symbols = r; renderOutline(); }
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
    if (!(await saveAll())) { showProjectName(old, S.projectFolder); return toast("Resolve the save conflict first."); }
    let r = await api("/api/project/rename", { name }).catch(() => ({ error: "server not reachable" }));
    if (r._status === 404) {
      // This project's server started before renaming existed: restart it with the new
      // code, rename, and load the page again.
      toast("Updating this project's server to rename it…");
      const ok = await restartServer();
      if (ok !== true) { showProjectName(old, S.projectFolder); return toast("Could not rename: " + ok); }
      r = await api("/api/project/rename", { name }).catch(() => ({ error: "server not reachable" }));
      if (r._status === 200) {
        if (r.moving) { toast(`Renaming the folder to “${r.folder}”…`); await waitForNewServer(); }
        return reloadPage();
      }
    }
    if (r._status !== 200) { showProjectName(old, S.projectFolder); return toast("Could not rename: " + (r.error || "unknown error")); }
    if (r.moving) {
      // The folder is renamed too: the server stops, and starts again in it on this port.
      toast(`Renaming the folder to “${r.folder}”…`);
      const ok = await waitForNewServer();
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

async function poll() {
  const r = await api("/api/tree").catch(() => null);
  if (!r || r._status !== 200) { $("#build-status").textContent = "server not reachable"; $("#build-status").className = "status err"; return; }
  showProjectName(r.name || r.root, r.root);
  if (r.move_error && !S.moveErrorShown) { S.moveErrorShown = true; toast(r.move_error); }
  const changed = JSON.stringify(r.files.map((f) => [f.path, f.git])) !== JSON.stringify(S.files.map((f) => [f.path, f.git]));
  S.files = r.files; S.order = r.order || [];
  if (changed) renderTree();
  let anyChange = false;
  for (const t of S.tabs) {
    const f = r.files.find((x) => x.path === t.path);
    if (!f || Math.abs(f.mtime - t.mtime) < 1e-6) continue;
    anyChange = true;
    if (!isDirty(t)) await reloadFromDisk(t);
    else if (t.conflict !== "disk") { t.conflict = "disk"; showBanner(t); }
  }
  if (anyChange || changed) loadSymbols();
  if (r.pdf_mtime && r.pdf_mtime !== S.pdfMtime && !S.building) showPdf(r.pdf_mtime);
  $("#btn-restart").hidden = !r.updated;
  S.server = r.server;
  if (r.sync) renderSync(r.sync);
}

/* ------------------------------------------------------------------ GitHub sync */
// The project's changes are committed to its repository and pushed (gitsync.py): after you
// stop editing, after each agent turn, and from this menu. The button says how far that is.
function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago`
    : new Date(t * 1000).toLocaleDateString();
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
  const targetText = (t) => t ? `${t.repository_identity}, branch ${t.branch_ref.replace(/^refs\/heads\//, "")}` : "unavailable";
  let text, cls = "", detail;
  const when = st.last_push || (st.last_commit && st.last_commit.at);
  if (!st.own) { text = "Not on GitHub"; cls = "off"; detail = "This project has no git repository yet. Create a private GitHub repository for it below: it is then saved and synced automatically."; }
  else if (!st.enabled) { text = "Sync off"; cls = "off"; detail = "Changes are not committed or pushed automatically. Switch it on below."; }
  else if (st.blocked_reason && st.remote) { text = "Sync paused"; cls = "warn"; detail = `Local edits are saved. ${st.blocked_reason}. Bound target: ${targetText(st.bound_target)}. Current target: ${targetText(st.target)}.`; }
  else if (st.clash && st.clash.length) { text = "Clash with GitHub"; cls = "warn"; detail = st.error || ""; }
  else if (st.error) { text = "Not synced"; cls = "warn"; detail = st.error; }
  else if (st.state !== "idle") { text = { committing: "Saving…", pushing: "Pushing…", pulling: "Pulling…" }[st.state] || "Syncing…"; cls = "busy"; detail = "Working with git…"; }
  else if (st.pending) {
    const n = Math.ceil((st.next_autosave || 0) / 60);
    text = "Unsaved changes"; cls = "pending";
    detail = `Your latest edits are saved on disk, not yet in GitHub. They are committed ${n <= 0 ? "now" : `in about ${n} min`} (or once you stop editing for 2 min).`;
  }
  else if (st.ahead && st.remote) { text = `${st.ahead} to push`; cls = "pending"; detail = `${st.ahead} commit${st.ahead > 1 ? "s" : ""} not on GitHub yet.`; }
  else if (st.remote && st.kept && st.kept.length) {
    text = `Both versions kept (${st.kept.length})`; cls = "pending";
    detail = "Synced with GitHub. Where you and a co-author changed the same lines, both versions are in the file: open the menu to go there.";
  }
  else if (!st.remote) { text = "Not on GitHub"; cls = "off"; detail = "Committed in the project's repository, which has no GitHub remote yet: nothing is pushed. Create one below."; }
  else { text = "Saved to GitHub"; cls = "ok"; detail = `Synced to ${targetText(st.target)}${when ? ", " + ago(when) : ""}.`; }
  label.textContent = text;
  b.className = "ghost sync-" + cls;
  b.title = detail.replace(/<[^>]+>/g, "");
  const last = st.last_commit ? `<br>Last commit ${ago(st.last_commit.at)}: <i>${esc(st.last_commit.message)}</i>` : "";
  $("#sync-detail").innerHTML = esc(detail) + last;
  $("#sync-auto").checked = !!st.enabled;
  // Where you and a co-author changed the same lines, both versions were kept: list them.
  const kept = (st.own && st.kept) || [];
  $("#sync-kept").hidden = !kept.length;
  $("#sync-kept").innerHTML = kept.length ? `<div class="dd-note">Both versions kept where you and a co-author changed the same lines.
      Keep what you want and delete the <code>% [prism-local]</code> lines:</div>` + kept.map((k) =>
    `<button type="button" class="dd-item" data-kept="${esc(k.path)}" data-line="${k.line || 1}">${esc(k.path)}${k.line ? ":" + k.line : ""}
       <small>${k.side ? "GitHub's version saved as " + esc(k.side) : "with " + esc(k.who)}</small></button>`).join("") + "<hr>" : "";
  // The automatic merge could not be done: compare, combine, then keep the combination.
  const clash = (st.own && st.clash) || [];
  $("#sync-clash").hidden = !clash.length;
  $("#sync-clash").innerHTML = clash.map((f) =>
    `<button type="button" class="dd-item" data-theirs="${esc(f)}">Compare with GitHub's version: ${esc(f)}</button>`).join("")
    + (clash.length ? `<button type="button" class="dd-item" data-sync="resolve">I've combined them: keep my version of the clashing lines</button><hr>` : "");
  if (st.notice) toast(st.notice + (/[.)]$/.test(st.notice) ? "" : ".") + " The editor shows the new versions.");
}
function syncMenu(open) { $("#sync-menu").hidden = !open; $("#btn-sync").setAttribute("aria-expanded", String(open)); }
$("#btn-sync").onclick = async (e) => {
  e.stopPropagation(); syncMenu($("#sync-menu").hidden);
  if (!$("#sync-menu").hidden && !$("#sync-publish").hidden) {
    // Created in a terminal since the editor started? Look once, now that you ask.
    const st = await api("/api/git/recheck", {}).catch(() => null);
    if (st && st._status === 200) { delete st._status; renderSync(st); }
    if (!$("#sync-publish").hidden) loadPublish();
  }
  if (!$("#sync-menu").hidden && !S.githubUrl) {
    const r = await api("/api/git/github").catch(() => ({}));
    S.githubUrl = r.github || "";
    if (S.githubUrl) { $("#sync-github").href = S.githubUrl; $("#sync-github").hidden = false; }
  }
};
$("#sync-menu").addEventListener("click", async (e) => {
  e.stopPropagation();
  const b = e.target.closest("[data-sync]"); if (!b) return;
  syncMenu(false);
  if (b.dataset.sync === "history") return showHistory();
  if (b.dataset.sync === "bind" && !confirm(`Sync this project to ${S.sync.target.repository_identity}\nBranch: ${S.sync.target.branch_ref}\nFetch: ${S.sync.target.fetch_url}\nPush: ${S.sync.target.push_url}?`)) return;
  if (b.dataset.sync === "resolve" && !confirm("Keep your version of the lines you and GitHub both changed?\n\n"
      + "Do this after editing the file so it holds what both of you want: where both changed the same lines, "
      + "yours is kept; everything else of theirs comes in. Their version stays in their commits (History).")) return;
  if (b.dataset.sync === "resolve" && !(await saveAll())) return;
  // Your open files go to disk first: committed by Save now, and out of the way of what Get
  // changes brings in (no "changed on disk" clash with text not saved yet).
  if ((b.dataset.sync === "commit" || b.dataset.sync === "pull") && !(await saveAll())) return;
  renderSync({ ...S.sync, state: b.dataset.sync === "pull" ? "pulling" : "committing", error: null });
  const r = await api("/api/git/sync", { action: b.dataset.sync, target: b.dataset.sync === "bind" ? S.sync.target : undefined }).catch(() => null);
  if (r && r._status === 200) renderSync(r);
  else if (r && r.error) toast(r.error);
  await poll();
});
// Create a private GitHub repository for the project (hub.publish_project, through this server).
async function loadPublish() {
  const f = $("#sync-publish"), go = $("#pub-go"), note = $("#pub-note");
  if (f.dataset.loaded) return;
  note.textContent = "Checking…"; go.disabled = true;
  const r = await api("/api/git/publish").catch(() => null);
  if (!r || r._status !== 200) { note.textContent = "Could not check: " + ((r && r.error) || "server not reachable"); return; }
  f.dataset.loaded = "1";
  if (!f.elements.name.value) f.elements.name.value = r.name;
  const gh = r.gh || {};
  $("#pub-owner").textContent = `github.com/${gh.account || "…"}/ · private`;
  $("#pub-private").innerHTML = (r.private_dirs || []).filter((d) => !d.ignored).map((d) =>
    `<label class="dd-check"><input type="checkbox" name="leave_out" value="${esc(d.dir)}" checked> Keep <code>${esc(d.dir)}/</code> (${esc(d.what)}) off GitHub</label>`).join("");
  const why = r.kind === "nested" ? `This folder is inside another repository (${r.top}), so it cannot have one of its own. Move the project out of it first.`
    : !gh.logged_in ? (gh.error || "The GitHub CLI is not logged in.") + " Then reopen this menu."
    : r.kind === "shared" ? `This project is part of the repository in ${r.top}: that whole repository goes to GitHub.`
    : "Build output and LaTeX's auxiliary files stay out (.gitignore). From then on, changes are saved and pushed automatically.";
  note.textContent = why;
  go.disabled = r.kind === "nested" || !gh.logged_in;
}
$("#sync-publish").addEventListener("click", (e) => e.stopPropagation());
$("#sync-publish").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, go = $("#pub-go");
  if (C.job || S.building) return toast("Wait until the agent's turn or the build has finished.");
  if (!(await saveAll())) return toast("Resolve the save conflict first.");
  go.disabled = true; go.textContent = "Creating…";
  const r = await api("/api/git/publish", {
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
  const r = await api("/api/git/theirs?path=" + encodeURIComponent(b.dataset.theirs));
  if (r._status !== 200) return toast(r.error || "Could not get GitHub's version");
  const head = r.content === null ? `GitHub has deleted ${r.path}.` : `What GitHub's version of ${r.path} has that yours does not (+), and what yours has instead (-):`;
  $("#diff").innerHTML = [head, "", ...(r.diff || "(the same)").split("\n")].map((l) => {
    const cls = l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : "";
    return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l);
  }).join("\n");
  openPanel("diff");
});
$("#sync-auto").onchange = async (e) => { const r = await api("/api/git/sync", { action: e.target.checked ? "on" : "off" }); renderSync(r); };
document.addEventListener("click", (e) => { if (!e.target.closest("#sync-box")) syncMenu(false); });

// History: the commits that changed the open file; one of them shows what it changed and
// can be put back (as an edit of yours, which is saved and committed like any other).
async function showHistory(path = S.active) {
  openPanel("history");
  const list = $("#hist-list"), view = $("#hist-view");
  view.innerHTML = "";
  if (!path) { list.innerHTML = `<li class="none">Open a file to see its history.</li>`; return; }
  list.innerHTML = `<li class="none">Loading the history of ${esc(path)}…</li>`;
  const r = await api("/api/git/log?path=" + encodeURIComponent(path)).catch(() => ({}));
  const commits = r.commits || [];
  if (!commits.length) { list.innerHTML = `<li class="none">${esc(r.error || `No commits of ${path} yet.`)}</li>`; return; }
  list.innerHTML = `<li class="hist-file">${esc(path)} · ${commits.length} version${commits.length > 1 ? "s" : ""}</li>` + commits.map((c, i) =>
    `<li data-rev="${c.hash}" data-path="${esc(path)}"><span class="hist-when" title="${esc(new Date(c.at * 1000).toLocaleString())}">${esc(ago(c.at))}</span>`
    + `<span class="hist-msg">${esc(c.message)}</span><span class="hist-hash">${c.hash.slice(0, 7)}${i === 0 ? " · latest" : ""}</span></li>`).join("");
}
$("#hist-list").addEventListener("click", async (e) => {
  const li = e.target.closest("li[data-rev]"); if (!li) return;
  document.querySelectorAll("#hist-list li.sel").forEach((x) => x.classList.remove("sel")); li.classList.add("sel");
  const r = await api(`/api/git/show?rev=${li.dataset.rev}&path=${encodeURIComponent(li.dataset.path)}`);
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
  if (!(await saveAll())) return "Resolve the save conflict first.";
  const r = await api("/api/restart", {}).catch(() => ({ error: "server not reachable" }));
  if (r.error) return r.error;
  return waitForNewServer();
}
// The server stopped to start again (restart, folder rename): wait until the new one answers.
async function waitForNewServer() {
  for (let i = 0; i < 80; i++) {           // the new server listens within a few seconds
    await new Promise((res) => setTimeout(res, 500));
    const t = await fetch("/api/tree", { cache: "no-store" }).then((x) => x.ok && x.json()).catch(() => null);
    if (t && t.server !== S.server) return true;
  }
  return "The server did not come back. Reopen the project from the Home page.";
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
const REF_RE = /\\(?:[cC]ref|[cC]pageref|ref|eqref|autoref|pageref|labelcref|namecref|nameref)\*?\{([^}]*)$/;
const CITE_RE = /\\(?:cite[tp]?|nocite|citeauthor|citeyear)\*?(?:\[[^\]]*\]){0,2}\{([^}]*)$/;
const CMD_RE = /\\([A-Za-z]*)$/;
const COMMON_CMDS = ["begin", "end", "label", "cref", "Cref", "eqref", "cite", "section", "subsection", "emph", "textbf",
  "mathbb", "mathcal", "mathrm", "operatorname", "frac", "sum", "int", "lim", "sup", "inf", "left", "right", "langle", "rangle",
  "varepsilon", "alpha", "beta", "gamma", "delta", "lambda", "mu", "sigma", "omega", "Omega", "partial", "nabla", "infty",
  "subseteq", "coloneqq", "quad", "qquad", "text", "item", "input", "footnote", "ref", "includegraphics"];

function computeHints(ed, explicit) {
  const before = lineBefore(ed), cur = ed.getCursor();
  let m, items = [], word = "";
  if ((m = before.match(REF_RE))) {
    word = m[1].split(",").pop().trimStart();
    const titles = S.symbols.env_titles || {};
    items = S.symbols.labels.map((l) => ({ text: l.label, kind: `${titles[l.kind] || l.kind} · ${l.file.split("/").pop()}:${l.line}` }));
  } else if ((m = before.match(CITE_RE))) {
    word = m[1].split(",").pop().trimStart();
    items = S.symbols.bibkeys.map((k) => ({ text: k.key, kind: k.type }));
  } else if ((m = before.match(CMD_RE)) && (explicit || m[1].length >= 2)) {
    word = m[1];
    const mine = new Set((S.symbols.macros || []).map((x) => x.name));
    items = [...new Set([...mine, ...COMMON_CMDS])].map((n) => ({ text: n, kind: mine.has(n) ? "macros.tex" : "" }));
  } else return null;
  const w = word.toLowerCase();
  const list = items.filter((i) => i.text.toLowerCase().includes(w) && i.text !== word)
    .sort((a, b) => (a.text.toLowerCase().startsWith(w) ? 0 : 1) - (b.text.toLowerCase().startsWith(w) ? 0 : 1) || a.text.localeCompare(b.text))
    .slice(0, 60)
    .map((i) => ({ text: i.text, render: (el) => { el.innerHTML = `${esc(i.text)}<span class="hint-kind">${esc(i.kind)}</span>`; } }));
  if (!list.length) return null;
  return { list, from: { line: cur.line, ch: cur.ch - word.length }, to: cur };
}

function showCompletions(ed, explicit) {
  if (ed.state.completionActive) return;          // the open widget re-queries by itself
  if (!computeHints(ed, explicit)) {
    if (explicit && CITE_RE.test(lineBefore(ed)) && !S.symbols.bibkeys.length) toast("No BibTeX entries found in the project's .bib files.");
    return;
  }
  ed.showHint({ hint: (e) => computeHints(e, explicit), completeSingle: false });
}

async function loadConfig() {
  const r = await api("/api/config");
  if (r._status !== 200) return;
  BUILD.modes = r.modes || [];
  BUILD.cmds = r.build || {};
  BUILD.main = String(r.main || "main.tex").replace(/\\/g, "/").replace(/^(\.\/)+/, "");
  const want = store.get("buildmode", "draft");
  BUILD.mode = BUILD.modes.includes(want) ? want : BUILD.modes[0] || "";
  renderCompileMenu();
  if (r.error) { $("#build-info").textContent = r.error; toast(r.error); }
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
const plural = (n, w, ws = w + "s") => `${n} ${n === 1 ? w : ws}`;

// The result of a build, yours or the agent's (its compile tool): status, problems, PDF.
async function showBuild(r) {
  const st = $("#build-status");
  if (!r.cancelled) S.diagnostics = r.diagnostics || [];      // a stopped build keeps the last list
  $("#output").textContent = r.output || "";
  const errs = S.diagnostics.filter((d) => d.severity === "error").length;
  const warns = S.diagnostics.length - errs;
  let text, cls = "err";
  if (r.cancelled) [text, cls] = ["build stopped", "warn"];
  else if (r.timed_out) text = "stopped: the build took too long";
  else if (r.exit === 0 || r.pdf_updated) {
    text = errs ? `PDF built with ${plural(errs, "error")}` : r.exit === 0 ? "OK" : "PDF built with errors";
    cls = errs || r.exit !== 0 ? "err" : warns ? "warn" : "ok";
  } else text = `FAILED (exit ${r.exit})`;
  st.className = "status " + cls;
  st.textContent = text + (warns && !r.cancelled ? ` · ${plural(warns, "warning")}` : "") + ` · ${r.seconds}s`;
  // What ran, e.g. "pdflatex (default) · 3 passes · bibtex main".
  const how = [r.builder === "builtin" ? `${r.engine} (${r.engine_reason === "default" ? "default" : "because of " + r.engine_reason})` : r.builder,
    r.builder === "builtin" && r.passes ? plural(r.passes, "pass", "passes") : "",
    ...(r.steps || []).filter((s) => !/\(pass \d+\)$/.test(s))].filter(Boolean).join(" · ");
  st.title = how; $("#build-info").textContent = how ? "Last build: " + how : "";
  renderProblems();
  applyDiagnostics();
  if ((r.exit !== 0 && !r.cancelled) || errs) openPanel(errs || !r.output ? "problems" : "output");
  if (r.pdf_mtime) await showPdf(r.pdf_mtime);
}

async function compile(clean = false) {
  if (S.building) return;
  if (C.editTurn) toast("Compiling the files on disk; your edits are saved when the agent's turn ends.");
  else if (!(await saveAll())) return;
  const mode = BUILD.mode;
  S.building = true;
  const st = $("#build-status");
  st.className = "status busy"; st.textContent = clean ? `recompiling from scratch (${mode})…` : `compiling (${mode})…`;
  setCompileButton(true);
  try {
    const r = await api("/api/build", { mode, clean });
    if (r.busy) { st.className = "status warn"; st.textContent = "a build is already running"; return; }
    if (r._status !== 200) { st.className = "status err"; st.textContent = "build failed: " + (r.error || "HTTP " + r._status); return; }
    await showBuild(r);
  } catch (e) {
    st.className = "status err"; st.textContent = "build request failed: " + e;
  } finally {
    S.building = false; setCompileButton(false);
  }
}
$("#btn-compile").onclick = () => S.building ? api("/api/build/stop", {}) : compile();
$("#btn-clean-build").onclick = () => { compileMenu(false); compile(true); };

/* Compile menu: build mode and auto-compile, on the ▾ half of the Compile button. */
const BUILD = { modes: [], cmds: {}, main: "main.tex", mode: store.get("buildmode", "draft") };
const MODE_INFO = {
  draft: ["Draft", "continue on errors"], strict: ["Strict", "stop at first error"], check: ["Check", ""],
};
function renderCompileMenu() {
  $("#build-modes").innerHTML = BUILD.modes.map((m) => {
    const [name, note] = MODE_INFO[m] || [m, ""];
    return `<label class="dd-item" title="${esc(BUILD.cmds[m] || "")}"><input type="radio" name="build-mode" value="${m}" ${m === BUILD.mode ? "checked" : ""}> ${esc(name)}${note ? `<small>${esc(note)}</small>` : ""}</label>`;
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
    <li data-i="${i}"><span class="sev ${x.severity}">${x.severity}</span><span class="loc">${esc(x.file || "?")}${x.line ? ":" + x.line : ""}</span>${esc(x.message)}</li>`).join("")
    : `<li class="none">No errors or warnings from the build.</li>`;
}
$("#problems").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-i]"); if (!li) return;
  const d = S.diagnostics[+li.dataset.i];
  if (d.file) openFile(d.file, d.line);
});

let diagMarks = [];
function applyDiagnostics() {
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
async function showDiff(path) {
  const r = await api("/api/diff" + (path ? "?path=" + encodeURIComponent(path) : ""));
  const what = path ? path : "this project";
  let text;
  if (r.repo === false) text = `${what === "this project" ? "This project" : what} is not in a git repository, so there is nothing to compare with.\nCreate one from the GitHub button at the top (Not on GitHub).`;
  else {
    const parts = [];
    if (r.untracked && r.untracked.length) parts.push(`New files, not committed yet: ${r.untracked.join(", ")}`);
    if (r.diff) parts.push(r.diff);
    else if (r.last) parts.push(`No uncommitted changes in ${what}: everything is committed${S.sync && S.sync.remote ? " (and pushed to GitHub with sync on)" : ""}.\n`
      + `The last commit that changed it, ${ago(r.last.at)}: ${r.last.message}  [${r.last.hash}]\n\n${r.last.diff}`);
    else if (!parts.length) parts.push(`No uncommitted changes in ${what}.`);
    text = parts.join("\n\n") || r.diff || "";
  }
  $("#diff").innerHTML = text.split("\n").map((l) => {
    const cls = l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : "";
    return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l);
  }).join("\n");
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
  if (!on && S.pdfMtime && S.pdfMtime !== PV.mtime) PV.load(S.pdfMtime);
}
let popWin = null;
$("#btn-pdf-here").onclick = () => {
  if (pdfChannel) pdfChannel.postMessage({ type: "close" });
  POP.here = true; setPopped(false);
  if (S.pdfMtime) PV.load(S.pdfMtime);
};
function popOut() {
  POP.here = false;
  popWin = window.open("/viewer", "prism-pdf");
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

function showPdf(mtime) {
  S.pdfMtime = mtime;
  if (pdfChannel) pdfChannel.postMessage({ type: "pdf", mtime });
  if (!POP.alive) return PV.load(mtime);
}

/* ------------------------------------------------------------------ SyncTeX */
async function forwardSync() {
  const t = activeTab(); if (!t || !t.path.endsWith(".tex")) return;
  if (!S.pdfMtime) return toast("No PDF yet — compile first.");
  const line = cm.getCursor().line + 1;
  const r = await api(`/api/synctex/forward?file=${encodeURIComponent(t.path)}&line=${line}`);
  if (r._status !== 200) return toast("No PDF location for this line (compile, or the line produces no output).");
  if (POP.alive && pdfChannel) pdfChannel.postMessage({ type: "forward", r });
  else PV.highlight(r);
}
$("#btn-forward").onclick = () => forwardSync();

// Returns what happened, for the pop-out viewer to show: {ok, file, line} or {msg}.
async function inverseJump(page, x, y) {
  const r = await api(`/api/synctex/inverse?page=${page}&x=${x.toFixed(2)}&y=${y.toFixed(2)}`);
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

let toastTimer = null;
function toast(msg) {
  const st = $("#build-status");
  const prev = [st.textContent, st.className];
  st.textContent = msg; st.className = "status warn";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { if (st.textContent === msg) [st.textContent, st.className] = prev; }, 4000);
}

document.querySelectorAll(".gutter").forEach((g) => {
  g.addEventListener("mousedown", (e) => {
    e.preventDefault(); g.classList.add("drag");
    const target = { sidebar: $("#sidebar"), pdf: $("#pdf-pane"), chat: $("#chat") }[g.dataset.resize];
    const startX = e.clientX, startW = target.getBoundingClientRect().width;
    const move = (ev) => {
      const dx = ev.clientX - startX;
      const w = Math.max(160, startW + (g.dataset.resize === "sidebar" ? dx : -dx));
      target.style.width = w + "px"; cm.refresh();
    };
    const up = () => {
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
});
window.addEventListener("beforeunload", (e) => { if (S.tabs.some(isDirty)) { e.preventDefault(); e.returnValue = ""; } });

/* ------------------------------------------------------------------ agent panel */
// The agent runs on one provider at a time (Claude Code, Codex CLI, DeepSeek or another
// OpenAI-compatible API; see backends.py). Conversation, model and effort are kept per
// provider. P holds what /api/agent/info reports about each provider.
const P = { list: [], byId: {}, projectKey: null };
const C = { provider: store.get("chat.provider", null), job: null, cur: null };
const prov = () => P.byId[C.provider] || { id: C.provider, label: "Agent", models: [], efforts: [] };
const provLabel = () => prov().label || "the agent";
// Settings saved before providers existed belong to Claude Code.
const conversationKey = (k, provider = C.provider) => P.projectKey ? `chat.${P.projectKey}.${provider}.${k}` : null;
const provGet = (k) => ["session", "context", "log", "snips"].includes(k)
  ? (conversationKey(k) ? store.get(conversationKey(k), null) : null)
  : store.get(`chat.${k}.${C.provider}`, null);
const provSet = (k, v) => { const key = ["session", "context", "log", "snips"].includes(k) ? conversationKey(k) : `chat.${k}.${C.provider}`; if (key) store.set(key, v); };
// The account the provider runs under (Claude Code: `claude auth status`), and whether
// it is the one allowed in Home → Settings. A mismatch stops every turn on the server.
async function loadAccount() {
  const el = $("#agent-account");
  const r = await api("/api/agent/account?provider=" + encodeURIComponent(C.provider || "")).catch(() => null);
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
  $("#chat-log").innerHTML = saved || "";
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

// Minimal, safe rendering: escape first, then code fences, inline code, bold, file:line links.
function renderMd(text) {
  const parts = String(text).split(/```[a-zA-Z]*\n?/);
  return parts.map((p, i) => {
    if (i % 2) return `<pre>${esc(p.replace(/\n$/, ""))}</pre>`;
    let h = esc(p);
    h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    h = h.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
    h = h.replace(/((?:[\w.-]+\/)*[\w.-]+\.(?:tex|bib|md))(?::(\d+))?/g, (m, f, ln) => {
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
function saveChatLog() { provSet("log", $("#chat-log").innerHTML.slice(-400000)); }
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
const MENTION_RE = /(^|\s)@([^\s@]+)/g;
C.snips = {};      // selection text belongs to the current project and provider

function selectionMention() {
  const t = activeTab(), sel = cm.getSelection();
  if (!t || !sel) return null;
  const from = cm.getCursor("from").line + 1, to = cm.getCursor("to").line + 1;
  return { token: `@${t.path}:${from === to ? from : from + "-" + to}`, file: t.path, text: sel.slice(0, 6000) };
}
function parseMentions(text) {
  const out = [], seen = new Set();
  for (const m of text.matchAll(MENTION_RE)) {
    const raw = m[2].replace(/[.,;!?)]+$/, "");
    const mm = /^(.+?)(?::(\d+)(?:-(\d+))?)?$/.exec(raw);
    if (!mm || seen.has(raw) || !S.files.some((f) => f.path === mm[1])) continue;
    seen.add(raw);
    const from = mm[2] ? +mm[2] : null;
    out.push({ token: "@" + raw, file: mm[1], from, to: mm[3] ? +mm[3] : from });
  }
  return out;
}
const rangeLabel = (m) => m.from ? `${m.file}:${m.from}${m.to !== m.from ? "–" + m.to : ""}` : m.file;

async function referenceBlock(mentions) {
  if (!mentions.length) return "";
  let b = "[Referenced]\n";
  for (const m of mentions) {
    if (!m.from) { b += `File: ${m.file}\n`; continue; }
    let txt = C.snips[m.token];
    if (txt === undefined) {
      const r = await api("/api/file?path=" + encodeURIComponent(m.file));
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
      this.marks.set(path, kind ? list.filter((m) => m.kind !== kind) : []);
      this.apply(path);
    }
  },

  // Put the marks of `path` on its tab's document (after a swap, a reload, a change).
  apply(path) {
    for (const [doc, line, cls] of this.handles.get(path) || []) {
      doc.removeLineClass(line, "background", cls); doc.removeLineClass(line, "gutter", cls + "-g");
    }
    const t = S.tabs.find((x) => x.path === path), out = [];
    if (t) {
      const last = t.doc.lineCount() - 1;
      for (const m of this.marks.get(path) || []) {
        const cls = "cm-agent-" + m.kind;
        for (let l = m.from; l <= Math.min(m.to, last, m.from + 3000); l++) {
          const h = t.doc.addLineClass(l, "background", cls);
          t.doc.addLineClass(h, "gutter", cls + "-g");
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
   saving and their undo history are not touched. For an Edit, the text it replaces is struck
   through and the new text appears after it; for a Write, the file appears from the top.
   When the step ends the view closes and the editor shows the changed lines. */
const AV = {
  cm: null, id: null, path: null, name: null, hidden: false,
  base: null, old: "", oldDone: false, text: "", shown: 0, at: null, marks: [],

  view() {
    if (!this.cm) {
      this.cm = CodeMirror($("#agent-view-cm"), { readOnly: true, lineNumbers: true, lineWrapping: true, mode: "stex" });
      $("#agent-view-hide").onclick = () => { this.hidden = true; $("#agent-view").hidden = true; };
    }
    return this.cm;
  },

  async begin(id, name, path) {
    Object.assign(this, { id, name, path, base: null, old: "", oldDone: false, text: "", shown: 0, at: null, marks: [] });
    const v = this.view();
    v.setOption("mode", modeFor(path));
    $("#agent-view-title").textContent = `${name === "Write" ? "Writing" : "Editing"} ${path}`;
    $("#agent-view").hidden = this.hidden;           // hidden: it still marks the editor (AH)
    v.setValue(""); v.refresh();
    if (name === "Write") { this.base = ""; return; }
    // An edit changes the file as it is on disk.
    const r = await api("/api/file?path=" + encodeURIComponent(path)).catch(() => ({}));
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
    if (!this.hidden && S.files.some((f) => f.path === path)) openFile(path, line);
  },
};

/* Files added to a message: the + button, files dragged onto the panel, an image pasted
   into the box. Each is saved in the project's prism-uploads/ folder (/api/upload),
   committed in the project's repository and pushed to GitHub, and shown as a chip whose
   badge says how that went. The message tells the agent where the files are, and it
   reads them with its file tools (Claude Code reads PDFs and images too). */
const ATT = {
  files: [],             // {id, name, size, path|null (uploading), error}
  seq: 0,
  MAX: 25 * 1024 * 1024,

  render() {
    $("#chat-files").innerHTML = this.files.map((f) => `<span class="att ${f.error ? "err" : f.path ? "" : "busy"}" ${f.path ? `data-path="${esc(f.path)}"` : ""} title="${esc(f.error || f.path || "uploading…")}">`
      + `<span class="att-name">${esc(f.name)}</span><span class="att-size">${f.error ? "failed" : f.path ? this.size(f.size) : "…"}</span>`
      + `<span class="att-sync"></span><button class="att-x" data-att="${f.id}" aria-label="Remove ${esc(f.name)}">×</button></span>`).join("");
    this.showSync();
  },

  // The commit-and-push of each upload: a badge on its chips (here and in sent messages).
  sync: new Map(),       // path -> {state, message}
  timer: null,
  showSync() {
    const label = { syncing: "syncing…", synced: "✓ GitHub", local: "local", failed: "! not pushed" };
    document.querySelectorAll(".att[data-path]").forEach((el) => {
      const s = this.sync.get(el.dataset.path); if (!s) return;
      el.dataset.sync = s.state;
      const b = el.querySelector(".att-sync"); if (b) b.textContent = label[s.state] || "";
      el.title = `${el.dataset.path}\n${s.message || ""}`;
    });
  },
  watch(path, s) {
    this.sync.set(path, s); this.showSync();
    if (!this.timer) this.timer = setInterval(() => this.pollSync(), 1500);
  },
  async pollSync() {
    const open = [...this.sync].filter(([, s]) => s.state === "syncing").map(([p]) => p);
    if (!open.length) { clearInterval(this.timer); this.timer = null; return; }
    const r = await api("/api/upload/sync?paths=" + encodeURIComponent(open.join("\n"))).catch(() => null);
    if (!r) return;
    for (const p of open) {
      if (!r[p]) continue;
      this.sync.set(p, r[p]);
      if (r[p].state === "failed") toast(`${p.split("/").pop()}: ${r[p].message}`);
    }
    this.showSync();
  },

  size(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n >= 1024 ? Math.round(n / 1024) + " KB" : n + " B"; },

  async add(fileList) {
    for (const file of fileList) {
      const f = { id: ++this.seq, name: file.name || "pasted.png", size: file.size, path: null, error: null };
      if (file.size > this.MAX) { toast(`${f.name} is larger than 25 MB.`); continue; }
      this.files.push(f); this.render();
      try {
        const data = await new Promise((res, rej) => {
          const rd = new FileReader();
          rd.onload = () => res(String(rd.result).split(",", 2)[1] || "");
          rd.onerror = () => rej(rd.error);
          rd.readAsDataURL(file);
        });
        const r = await api("/api/upload", { name: f.name, data });
        if (r.error || !r.path) throw new Error(r.error || "upload failed");
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
  ATT.files = ATT.files.filter((f) => f.id !== +b.dataset.att); ATT.render();
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
{
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer && e.dataTransfer.types) || []].includes("Files");
  const chat = $("#chat");
  chat.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth++; $("#chat-drop").hidden = false; });
  chat.addEventListener("dragover", (e) => { if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
  chat.addEventListener("dragleave", (e) => { if (!hasFiles(e)) return; if (--depth <= 0) { depth = 0; $("#chat-drop").hidden = true; } });
  chat.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; $("#chat-drop").hidden = true;
    ATT.add([...e.dataTransfer.files]);
  });
}

async function chatSend() {
  if (C.job) return;
  const text = $("#chat-input").value.trim();
  if (!text) return;
  $("#slash-menu").hidden = true;
  const slash = /^\/([\w:.-]+)(?:\s+([\s\S]*))?$/.exec(text);
  if (slash && LOCAL[slash[1]]) {          // handled here, without running the agent
    $("#chat-input").value = "";
    chatAppend(esc(text), "msg user");
    return runLocal(slash[1], (slash[2] || "").trim());
  }
  if (!(await saveAll())) return toast("Resolve the save conflict before asking the agent.");
  if (ATT.files.some((f) => !f.path && !f.error)) return toast("Wait until the files are uploaded.");
  const mode = $("#chat-mode").value;
  const mentions = parseMentions(text);
  const refs = await referenceBlock(mentions);
  let prompt;
  if (slash) { await loadCatalog(); prompt = slashPrompt(text, refs); }
  else prompt = refs ? refs + "\n\n" + text : text;
  const attached = ATT.block(), sentFiles = ATT.files.filter((f) => f.path);
  const unsupported = sentFiles.find((f) => !(prov().input_types || ["text"]).includes(
    /\.(png|jpe?g|gif|webp)$/i.test(f.path) ? "image" : /\.pdf$/i.test(f.path) ? "pdf" : "text"));
  if (unsupported) return toast(`${provLabel()} does not support this attachment: ${unsupported.name}.`);
  if (attached) prompt = slash ? `${prompt}\n\n${attached}` : `${attached}\n\n${prompt}`;   // a /command stays first
  // Only the mentioned files may change; without mentions, the whole project.
  const scope = mode === "edit" && mentions.length ? [...new Set(mentions.map((m) => m.file))] : null;
  const provider = C.provider;
  if (!P.projectKey) return toast("Wait for the project identity to load.");
  const r = await api("/api/agent", { prompt, session_id: C.session, mode, model: C.model, effort: C.effort, scope, provider,
    project_key: P.projectKey, attachments: sentFiles.map((f) => f.path) });
  if (r.error) return chatAppend(`<div class="err">${esc(r.error)}</div>`, "card");
  $("#chat-input").value = ""; updateScope();
  ATT.files = []; ATT.render();
  setTimeout(() => ATT.showSync(), 0);           // the badges on the sent message's chips
  const extra = [provLabel(), r.model, C.effort && "effort " + C.effort].filter(Boolean).join(" · ");
  chatAppend(`${mentionHtml(text)}${sentFiles.length ? `<span class="att-sent">${sentFiles.map((f) => `<span class="att" data-path="${esc(f.path)}"><span class="att-name">${esc(f.path.split("/").pop())}</span><span class="att-sync"></span></span>`).join("")}</span>` : ""}<span class="ctx">${esc(describeScope(mentions, mode))}${extra ? " · " + esc(extra) : ""}</span>`, "msg user");
  C.job = r.job; C.cur = null; AV.hidden = false; AH.clear();   // last turn's marks go
  C.editTurn = mode === "edit"; C.heldNote = false;       // saves wait for the turn (see saveTab)
  const tools = new Map();
  $("#chat-send").textContent = "Stop"; $("#chat-send").classList.remove("primary");
  $("#chat-status").className = "status busy"; $("#chat-status").textContent = "working…";
  let after = 0, done = false, buf = "", streamed = false;
  // A streamed message is drawn once per batch of events, not once per fragment, and the
  // log follows it only while you have not scrolled up to read something.
  const log = $("#chat-log");
  const flush = (stick) => { if (C.cur) { C.cur.innerHTML = renderMd(buf); if (stick) log.scrollTop = log.scrollHeight; } };
  // What the agent is doing now, in the status line: thinking, writing a file, running a tool.
  const status = (s) => { $("#chat-status").textContent = s; };
  // Thinking and the text of a file being written stream into their own boxes, which close
  // when that step ends (click to open them again). Drawn once per batch, like messages.
  let think = null;              // {box, text} of the thinking being streamed
  const live = new Map();        // tool id -> {box, pre, text, path, name}
  const dirty = new Set();
  const draw = (stick) => {
    for (const x of dirty) {
      x.pre.textContent = x.text.length > 200000 ? "…" + x.text.slice(-200000) : x.text;
      x.pre.scrollTop = x.pre.scrollHeight;
    }
    dirty.clear();
    AV.render();
    if (stick) log.scrollTop = log.scrollHeight;
  };
  // Newer models (Opus) think without sending the text: the box then counts the seconds.
  const endThinking = () => {
    if (!think) return;
    clearInterval(think.timer);
    const secs = Math.round((Date.now() - think.t0) / 1000);
    think.box.querySelector("summary").textContent = think.text
      ? `Thought for ${secs}s` : `Thought for ${secs}s (this model does not show its thinking)`;
    if (!think.text) think.pre.remove();
    think.box.open = false; think = null;
  };
  const startThinking = () => {
    C.cur = null;
    if (think) return;
    think = liveBox("thinking", "Thinking…"); think.t0 = Date.now();
    const tick = () => {
      const secs = Math.round((Date.now() - think.t0) / 1000);
      think.box.querySelector("summary").textContent = `Thinking… ${secs}s`;
      status(`thinking… ${secs}s`);
    };
    const t = think; think.timer = setInterval(() => { if (think === t) tick(); }, 1000);
    tick();
  };
  const liveBox = (cls, summary) => {
    const box = document.createElement("details");
    box.className = cls; box.open = true;
    box.innerHTML = `<summary>${esc(summary)}</summary><pre></pre>`;
    log.appendChild(box);
    return { box, pre: box.querySelector("pre"), text: "" };
  };
  const toolLabel = (name, summary) => `<span class="st">▸</span>${esc(name)} ${esc(summary || "")}`;
  try {
    while (!done) {
      let d;
      try { d = await api(`/api/agent/events?job=${r.job}&after=${after}`); }
      catch { await new Promise((res) => setTimeout(res, 1000)); continue; }
      if (d._status !== 200) break;
      const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
      let pending = false;
      for (const e of d.events) {
        if (e.t !== "delta" && pending) { flush(stick); pending = false; }
        if (e.t !== "thinking" && e.t !== "thinking_start") endThinking();
        if (e.t === "thinking_start") startThinking();
        else if (e.t === "thinking") { startThinking(); think.text += e.text; dirty.add(think); }
        else if (e.t === "tool_start") {
          C.cur = null;
          AH.clear("read");          // a read stays marked until the agent's next step
          tools.set(e.id, chatAppend(toolLabel(e.name, ""), "tool"));
          status(`${e.name}…`);
        }
        else if (e.t === "tool_live") {
          let x = live.get(e.id);
          if (!x) {
            const name = tools.get(e.id) ? tools.get(e.id).textContent.slice(1).trim() : "Write";
            x = liveBox("live-file", name); x.name = name; live.set(e.id, x);
          }
          if (e.path) {
            x.path = e.path; x.box.querySelector("summary").textContent = `${x.name} ${e.path}`;
            const el = tools.get(e.id); if (el) el.innerHTML = toolLabel(x.name, e.path);
            AV.begin(e.id, x.name, e.path); AH.on(e.path);
          }
          if (e.text) { x.text += e.text; dirty.add(x); }
          AV.feed(e);
          status(`writing ${x.path || "a file"}… (${x.text.split("\n").length} lines)`);
        }
        else if (e.t === "init") {
          // The session belongs to the provider that ran the turn, even if the menu changed since.
          store.set(conversationKey("session", provider), e.session_id);
          if (C.provider === provider) C.session = e.session_id;
        }
        else if (e.t === "message_start") { C.cur = null; buf = ""; streamed = false; }
        else if (e.t === "delta") {
          if (!C.cur) { C.cur = chatAppend("", "msg assistant"); buf = ""; status("writing a reply…"); }
          streamed = true; buf += e.text; pending = true;
        } else if (e.t === "text") {
          if (!streamed) { C.cur = chatAppend("", "msg assistant"); buf = e.text; flush(stick); C.cur = null; }
        } else if (e.t === "tool") {
          C.cur = null;
          // Claude Code announced this call already (tool_start): fill in its summary.
          const el = tools.get(e.id) || chatAppend("", "tool");
          el.innerHTML = toolLabel(e.name, e.summary);
          el.title = `${e.name} ${e.summary || ""}`;
          tools.set(e.id, el);
          AH.clear("read");          // (backends without tool_start)
          if (e.path) AH.on(e.path);
          if (e.path && e.lines) AH.set(e.path, "read", e.lines[0] - 1, e.lines[1] - 1);
          status(`${e.name} ${e.summary || ""}`.trim().slice(0, 80) + "…");
        } else if (e.t === "tool_result") {
          const el = tools.get(e.id);
          if (el) { el.querySelector(".st").textContent = e.error ? "✗" : "✓"; if (e.error) { el.classList.add("err"); el.title += "\n" + e.preview; } }
          const x = live.get(e.id);
          if (x) { draw(false); x.box.open = false; live.delete(e.id); }
          AV.end(e.id, !e.error);
          status("working…");
        } else if (e.t === "build") showBuild(e.result);         // the agent compiled
        else if (e.t === "error") chatAppend(`<div class="err">${esc(e.message)}</div>`, "card");
        else if (e.t === "context") {                          // grows with each message
          if (C.provider === provider) renderContext({ ...e, window: e.window || (provGet("context") || {}).window });
        }
        else if (e.t === "done") {
          renderTurnCard(e); loadAccount();
          if (e.context && C.provider === provider) {
            const c = { ...e.context, window: e.context.window || (provGet("context") || {}).window, session: e.session_id };
            provSet("context", c); renderContext(c);
          }
        }
      }
      if (pending) flush(stick);
      draw(stick);
      after += d.events.length; done = d.done;
    }
  } finally {            // whatever happened above, the panel and saving work again
    endThinking(); draw(false); live.forEach((x) => { x.box.open = false; }); AV.end(null, false); AH.on(null); AH.clear("read");
    C.job = null; C.editTurn = false;
    $("#chat-send").textContent = "Send"; $("#chat-send").classList.add("primary");
    $("#chat-status").className = "status"; $("#chat-status").textContent = "";
    saveChatLog();
    await poll();        // first take in the agent's changes (a file it changed under your edits gets the banner) …
    flushSaves();        // … then save what you typed meanwhile
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
  const b = $("#quota-refresh");
  if (b) { b.disabled = true; b.textContent = "…"; }
  const r = await api("/api/agent/usage", { provider: C.provider }).catch(() => ({}));
  renderQuota(r.rate || store.get("chat.rate", null));
  if (r.error && !auto) toast(r.error);
}
$("#quota").addEventListener("click", (e) => {
  if (e.target.id === "quota-refresh") return checkUsage();
  if (e.target.closest("#quota-toggle")) {
    store.set("chat.quotaHidden", !store.get("chat.quotaHidden", false));
    renderQuota(lastRate);
  }
});

function diffHtml(diff) {
  return diff.split("\n").map((l) => {
    const cls = l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : "";
    return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l);
  }).join("\n");
}

function renderTurnCard(e) {
  let h = "";
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
  if (e.exit !== 0 || e.is_error)
    h += `<div class="err">${esc((P.byId[e.provider] || {}).label || "The agent")}: ${esc(e.subtype || "exit " + e.exit)}.${e.stderr ? "\n" + esc(e.stderr) : ""}</div>`;
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
}

$("#chat-log").addEventListener("click", async (ev) => {
  const a = ev.target.closest("a[data-file]");
  if (a) return openFile(a.dataset.file, a.dataset.line ? +a.dataset.line : undefined);
  const tg = ev.target.closest("[data-toggle]");
  if (tg) { const pre = tg.closest(".row").nextElementSibling; pre.hidden = !pre.hidden; return; }
  if (ev.target.closest("[data-compile]")) return compile();
  const u = ev.target.closest("[data-undo]");
  if (u) {
    if (S.tabs.some(isDirty) && !(await saveAll())) return;
    const r = await api("/api/agent/undo", { turn: +u.dataset.undo });
    if (r.error) { u.textContent = r.error === "unknown turn" ? "undo unavailable (an old turn, or the server restarted)" : r.error; u.disabled = true; return; }
    u.textContent = `undone (${r.restored.length})` + (r.skipped.length ? `; kept ${r.skipped.length} unavailable or changed since` : "");
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
  const q = `?provider=${encodeURIComponent(C.provider || "")}` + (refresh ? "&refresh=1" : "");
  const r = await api("/api/agent/commands" + q).catch(() => ({}));
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
  if (projectKey && projectKey !== info.project_key) {
    toast("This port now serves another project. Reopen the project's editor.");
    return;
  }
  projectKey = info.project_key || null;
  P.projectKey = info.project_key || null;
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

// The prompt for a message starting with "/": the command must come first. Skills take
// free text. Keep referenced text for unknown commands and all providers too.
function slashPrompt(text, refs) {
  return refs ? text + "\n\n" + refs : text;
}

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
    if (t && f.path === t.path) continue;
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
  if (/^\/(model|effort|provider) /.test(v)) { $("#chat-input").value = v.trim(); chatSend(); }
  else { $("#chat-input").value = v; $("#chat-input").focus(); updateScope(); }
});

$("#chat-send").onclick = () => { if (C.job) api("/api/agent/stop", { job: C.job }); else chatSend(); };
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
{
  chatIntro();
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  renderQuota(store.get("chat.rate", null));
  api("/api/agent/info").then((r) => {
    renderProviders(r);
    const p = prov();
    if (!p.available) return chatAppend(`<div class="err">${esc(p.reason || "No AI provider is set up.")}</div>`, "card");
    const known = p.rate || store.get("chat.rate", null);
    if (p.usage_limits && (!known || !known.at || Date.now() / 1000 - known.at > 600)) checkUsage(true);
  });
}

/* ------------------------------------------------------------------ start */
(async function init() {
  await loadConfig();
  await poll();
  await loadSymbols();
  const sess = store.get("session", null);
  const exists = (p) => S.files.some((f) => f.path === p);
  if (sess && sess.tabs) {
    for (const p of sess.tabs.filter(exists)) if (p !== sess.active) await openFile(p);
    if (sess.active && exists(sess.active)) await openFile(sess.active);
  }
  // No saved session: open the file the server builds (prism.json's "main", or the guess).
  if (!S.active && exists(BUILD.main)) await openFile(BUILD.main);
  renderProblems();
  setInterval(poll, 2000);
})();
updateScope();
