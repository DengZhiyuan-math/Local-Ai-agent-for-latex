/* prism-local Home — project list. Talks only to prism_local/hub.py.
   Loaded after common.js (helpers, theme, presence) and pdf.js. */
"use strict";

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdf.worker.js";
window.name = "prism-home";        // lets the editor's ⌂ button find and reuse this tab

const H = { projects: [], folders: [], tagColors: {}, defaultParent: "", git: {}, busy: new Set(), loaded: false,
            view: store.get("home.view", { kind: "all" }), collapsed: new Set(store.get("home.collapsed", [])),
            folderSort: store.get("home.folderSort", "name"), tagSort: store.get("home.tagSort", "name") };
const thumbs = new Map();          // `${id}:${pdf_mtime}` -> canvas (or null while rendering)

/* ------------------------------------------------------------------ helpers */
function ago(t) {
  if (!t) return "";
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  const units = [[60, "min"], [3600, "h"], [86400, "d"], [86400 * 30, "mo"], [86400 * 365, "y"]];
  let out = "";
  for (let i = units.length - 1; i >= 0; i--) {
    if (s >= units[i][0]) { out = Math.floor(s / units[i][0]) + " " + units[i][1]; break; }
  }
  return out + " ago";
}
let toastTimer = null;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = err ? "err" : ""; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, err ? 7000 : 2600);
}
const byId = (id) => H.projects.find((p) => p.id === id);

$("#btn-theme").onclick = () => {
  const cur = store.get("theme", null);
  const next = cur === null ? "dark" : cur === "dark" ? "light" : null;
  store.set("theme", next); applyTheme(next);
};

/* ------------------------------------------------------------------ data */
async function load() {
  const r = await api("/api/projects").catch(() => null);
  if (!r || r._status !== 200) return;
  delete r._status;
  const sig = JSON.stringify(r);
  if (sig === H.sig || H.dragging) return;   // unchanged: keep the DOM (hover, focus) as it is
  H.sig = sig;
  H.projects = r.projects; H.folders = r.folders || []; H.tagColors = r.tags || {};
  H.defaultParent = r.default_parent; H.loaded = true;
  render();
}
async function loadGit(p) {
  if (!p.exists) return;
  const r = await api("/api/git?id=" + encodeURIComponent(p.id)).catch(() => null);
  if (r && r._status === 200) { H.git[p.id] = r.git; document.querySelectorAll(`[data-id="${p.id}"] .gitchip`).forEach((el) => { el.outerHTML = gitChip(p.id); }); }
}
function loadAllGit() { return Promise.all(H.projects.map(loadGit)); }

/* ------------------------------------------------------------------ folders and tags
   Folders (a research topic, its sub-projects, …) and tags only organize this list;
   hub.py keeps them in projects.json, and no file moves on disk. */
const folderById = (id) => H.folders.find((f) => f.id === id);
const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
// When a project in folder f (or its subfolders) was last edited or opened; 0 if none.
function folderTime(f, mode) {
  const t = (p) => (mode === "edited" ? p.edited || 0 : p.opened || p.added || 0);
  return H.projects.reduce((m, p) => (inFolder(p, f.id) ? Math.max(m, t(p)) : m), 0);
}
// Folders by name, or most recently edited/opened first (empty ones last, by name).
function sortFolders(list, mode = H.folderSort) {
  if (mode !== "edited" && mode !== "opened") return list.sort(byName);
  const t = new Map(list.map((f) => [f.id, folderTime(f, mode)]));
  return list.sort((a, b) => t.get(b.id) - t.get(a.id) || byName(a, b));
}
const childFolders = (id, mode) => sortFolders(H.folders.filter((f) => (f.parent || null) === (id || null)), mode);
// Every folder below `id` (null: all of them), depth first, with its depth.
function folderTree(id = null, depth = 0, out = [], mode = undefined) {
  for (const f of childFolders(id, mode)) { out.push({ f, depth }); folderTree(f.id, depth + 1, out, mode); }
  return out;
}
// A project's folder; one whose folder was deleted elsewhere counts as not in a folder.
const folderOf = (p) => (p.folder_id && folderById(p.folder_id) ? p.folder_id : null);
function folderPath(id) {
  const out = [];
  for (let f = folderById(id); f && out.length < 50; f = f.parent && folderById(f.parent)) out.unshift(f);
  return out;
}
const isInside = (fid, ancestor) => folderPath(fid).some((f) => f.id === ancestor);
const inFolder = (p, fid) => { const f = folderOf(p); return !!f && isInside(f, fid); };
function allTags() {
  const set = new Map();
  for (const t of Object.keys(H.tagColors)) set.set(t, 0);
  for (const p of H.projects) for (const t of p.tags || []) set.set(t, (set.get(t) || 0) + 1);
  const list = [...set].map(([name, n]) => ({ name, n }));
  return H.tagSort === "count" ? list.sort((a, b) => b.n - a.n || byName(a, b)) : list.sort(byName);
}
const TAG_PALETTE = ["#2f6db3", "#2e8540", "#b07800", "#c0392b", "#7d4fb3", "#00897b", "#c2185b", "#6b7280"];
function tagColor(name) {
  const c = H.tagColors[name] && H.tagColors[name].color;
  if (c) return c;
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return TAG_PALETTE[h % TAG_PALETTE.length];
}
const tagChip = (t) => `<button class="tagchip" data-act="tag" data-tag="${esc(t)}" style="--tag:${tagColor(t)}" title="Show projects tagged ${esc(t)}">${esc(t)}</button>`;
function folderOptions(selected, { none = "— Not in a folder —", exclude = null } = {}) {
  const ex = [].concat(exclude || []);
  return `<option value="">${esc(none)}</option>` + folderTree(null, 0, [], "name")
    .filter(({ f }) => !ex.some((x) => isInside(f.id, x)))
    .map(({ f, depth }) => `<option value="${f.id}" ${f.id === selected ? "selected" : ""}>${"   ".repeat(depth)}${esc(f.name)}</option>`)
    .join("");
}
const currentFolder = () => (H.view.kind === "folder" ? H.view.id : "");
// Like hub.disk_name: a folder name usable on disk.
const diskName = (name) => name.replace(/[<>:"\/\\|?*\x00-\x1f]/g, "-").replace(/^[ .]+|[ .]+$/g, "") || "folder";
// The shared repository folder `fid` is in ({owner, repo, dir}: its own subdirectory), if any.
function sharedRepo(fid) {
  const path = folderPath(fid);
  for (let i = path.length - 1; i >= 0; i--) {
    const s = path[i].sync;
    if (s && s.mode === "shared" && s.repo) {
      const sep = s.repo.includes("\\") ? "\\" : "/";
      return { owner: path[i], repo: s.repo, dir: [s.repo, ...path.slice(i + 1).map((f) => diskName(f.name))].join(sep) };
    }
  }
  return null;
}
function syncChip(fid) {
  const sr = sharedRepo(fid);
  if (sr) return `<button class="chip sync-chip" data-act="sync" data-fid="${sr.owner.id}" title="${esc(sr.repo)}">⎇ One repository${sr.owner.id === fid ? "" : ` (${esc(sr.owner.name)})`}</button>`;
  const s = (folderById(fid) || {}).sync;
  return s && s.mode === "separate" ? `<button class="chip sync-chip" data-act="sync" data-fid="${fid}">⎇ A repository per project</button>` : "";
}

/* ------------------------------------------------------------------ location, history, selection
   The main pane works like a file explorer. A location (the top level, or a folder) shows
   what it holds: its subfolders, then its projects. Pinned, a tag, "Not in a folder" and a
   search (through the location and everything below it) show a flat list instead. */
const sameView = (a, b) => a.kind === b.kind && (a.id || null) === (b.id || null);
H.hist = [H.view]; H.histPos = 0;
H.sel = new Set(); H.anchor = null; H.cursor = null;
H.mode = store.get("home.mode", "details");          // details | icons
H.preview = store.get("home.preview", true);
function setView(view, { push = true } = {}) {
  if (push && sameView(view, H.view)) { if ($("#search").value) { $("#search").value = ""; render(); } return; }
  if (push) { H.hist = H.hist.slice(0, H.histPos + 1).concat([view]); H.histPos = H.hist.length - 1; }
  H.view = view; store.set("home.view", view);
  H.sel.clear(); H.anchor = H.cursor = null;
  $("#search").value = "";
  // The folder tree shows where we are.
  if (view.kind === "folder") for (const f of folderPath(view.id).slice(0, -1)) H.collapsed.delete(f.id);
  store.set("home.collapsed", [...H.collapsed]);
  render(); $("#view-wrap").scrollTop = 0;
}
function goHistory(step) {
  const i = H.histPos + step;
  if (i < 0 || i >= H.hist.length) return;
  H.histPos = i; setView(H.hist[i], { push: false });
}
function goUp() {
  const v = H.view;
  if (v.kind === "folder") { const f = folderById(v.id); setView(f && f.parent ? { kind: "folder", id: f.parent } : { kind: "all" }); }
  else if (v.kind !== "all") setView({ kind: "all" });
}
const locationName = () => ({ all: "Projects", pinned: "Pinned", unfiled: "Not in a folder" }[H.view.kind]
  || (H.view.kind === "tag" ? `Tag “${H.view.id}”` : (folderById(H.view.id) || {}).name || "Projects"));

/* ------------------------------------------------------------------ items: what the view lists */
const fItem = (f) => ({ k: "f", id: f.id, f, key: "f:" + f.id });
const pItem = (p) => ({ k: "p", id: p.id, p, key: "p:" + p.id });
function itemByKey(key) {
  const id = key.slice(2);
  if (key[0] === "f") { const f = folderById(id); return f && fItem(f); }
  const p = byId(id); return p && pItem(p);
}
function matches(p, q) {
  if (!q) return true;
  const hay = [p.name, p.folder, p.title || "", p.path, ...(p.tags || []),
               ...folderPath(folderOf(p)).map((f) => f.name)].join("\n").toLowerCase();
  return q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}
const folderMatches = (f, q) => q.toLowerCase().split(/\s+/).every((w) => (f.name + "\n" + (f.note || "")).toLowerCase().includes(w));
// {flat, items}: flat lists say where each item is (a Location column).
function viewItems(q) {
  const v = H.view, list = (pred) => H.projects.filter((p) => pred(p) && matches(p, q)).map(pItem);
  if (v.kind === "pinned") return { flat: true, items: list((p) => p.pinned) };
  if (v.kind === "unfiled") return { flat: true, items: list((p) => !folderOf(p)) };
  if (v.kind === "tag") return { flat: true, items: list((p) => (p.tags || []).includes(v.id)) };
  const fid = v.kind === "folder" ? v.id : null;
  if (q) return { flat: true, items: [
    ...folderTree(fid).map(({ f }) => f).filter((f) => folderMatches(f, q)).map(fItem),
    ...list((p) => !fid || inFolder(p, fid)),
  ] };
  return { flat: false, items: [...childFolders(fid, "name").map(fItem), ...H.projects.filter((p) => folderOf(p) === fid).map(pItem)] };
}
/* Sorting: folders first (as in Explorer), then by the chosen column. */
const SORTS = {
  name: { label: "Name", dir: 1, f: (f) => f.name, p: (p) => p.name },
  edited: { label: "Date modified", dir: -1, f: (f) => folderTime(f, "edited"), p: (p) => p.edited || 0 },
  opened: { label: "Last opened", dir: -1, f: (f) => folderTime(f, "opened"), p: (p) => p.opened || p.added || 0 },
  where: { label: "Location", dir: 1, f: (f) => folderPath(f.parent).map((x) => x.name).join("/"),
           p: (p) => folderPath(folderOf(p)).map((x) => x.name).join("/") },
};
H.sortKey = SORTS[store.get("home.sort", "opened")] ? store.get("home.sort", "opened") : "opened";
H.sortDir = store.get("home.sortDir", SORTS[H.sortKey].dir);
function setSort(key, dir = null) {
  if (dir === null) dir = key === H.sortKey ? -H.sortDir : SORTS[key].dir;
  H.sortKey = key; H.sortDir = dir;
  store.set("home.sort", key); store.set("home.sortDir", dir); render();
}
const cmpVal = (x, y) => (typeof x === "string" ? x.localeCompare(y, undefined, { numeric: true, sensitivity: "base" }) : x - y);
function sortItems(items, flat) {
  const s = SORTS[!flat && H.sortKey === "where" ? "name" : H.sortKey];
  const val = (it) => (it.k === "f" ? s.f(it.f) : s.p(it.p)), name = (it) => (it.k === "f" ? it.f.name : it.p.name);
  const v = new Map(items.map((it) => [it.key, val(it)]));
  return items.sort((a, b) => (a.k === b.k ? 0 : a.k === "f" ? -1 : 1)
    || H.sortDir * cmpVal(v.get(a.key), v.get(b.key)) || cmpVal(name(a), name(b)));
}

/* ------------------------------------------------------------------ render helpers */
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleString(undefined, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "");
const dateCell = (t, cls) => `<span class="${cls}" title="${t ? esc(ago(t)) : ""}">${fmtDate(t)}</span>`;
function gitChip(id) {
  const g = H.git[id];
  // null: looked, and no repository; undefined: not looked yet.
  if (g === null) return `<span class="gitchip"><button class="chip gh add" data-act="publish" title="Create a private GitHub repository for this project">+ GitHub</button></span>`;
  if (!g) return `<span class="gitchip"></span>`;
  if (g.nested) return `<span class="gitchip chip muted" title="No repository of its own: this folder is inside ${esc(g.toplevel_path)}">inside ${esc(g.toplevel)} repo</span>`;
  const extra = [g.shared ? `in ${g.shared}` : "", g.changes ? `${g.changes} changed` : "clean", g.ahead ? `↑${g.ahead}` : ""].filter(Boolean).join(" · ");
  // New commits on GitHub (fetched when the Home page started): opening the project pulls them.
  const news = g.behind ? ` <span class="chip news" title="${g.behind} new commit${g.behind === 1 ? "" : "s"} on GitHub (another computer, GitHub's editor). Opening the project brings them in.">↓ ${g.behind} new on GitHub</span>`
    : g.fetch_error ? ` <span class="chip err" title="${esc(g.fetch_error)}">GitHub not reached</span>` : "";
  const gh = g.github ? ` <a class="chip gh" href="${esc(g.github)}" target="_blank" rel="noopener" title="${esc(g.github)}">GitHub ↗</a>`
    : ` <button class="chip gh add" data-act="publish" title="Create a private GitHub repository for ${g.shared ? "the repository it is in" : "this project"}">+ GitHub</button>`;
  const where = g.shared ? `, in the repository shared by its folder (${g.toplevel_path})` : "";
  return `<span class="gitchip"><span class="chip ${g.changes ? "dirty" : ""}" title="git: branch ${esc(g.branch)}${esc(where)}">⎇ ${esc(g.branch || "—")} · ${esc(extra)}</span>${news}${gh}</span>`;
}
function initials(name) {
  const w = name.replace(/[-_.]+/g, " ").trim().split(/\s+/);
  return esc(((w[0] || "?")[0] + (w[1] ? w[1][0] : "")).toUpperCase());
}
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
function folderCounts(fid) {
  const n = H.projects.filter((p) => folderOf(p) === fid).length, subs = H.folders.filter((f) => f.parent === fid).length;
  return [subs && plural(subs, "folder"), n && plural(n, "project")].filter(Boolean).join(", ") || "Empty";
}
const whereText = (fid) => (fid ? folderPath(fid).map((f) => f.name).join(" › ") : "Projects");
const whereCell = (fid) => `<span class="c-where" title="${esc(whereText(fid))}">${esc(whereText(fid))}</span>`;
// The project's status: missing, starting, and its git state.
function statusHTML(p) {
  if (!p.exists) return `<span class="chip err">Folder not found</span>`;
  if (H.busy.has(p.id)) return `<span class="chip">Starting…</span>`;
  return gitChip(p.id);
}
const runDot = (p) => (p.running ? `<span class="run-dot" title="Open in an editor (${p.running.pages} page${p.running.pages === 1 ? "" : "s"})"></span>` : "");
const pinMark = (p) => (p.pinned ? `<span class="pin-mark" title="Pinned">${icon("star")}</span>` : "");
// A PDF thumbnail, or a placeholder until it is drawn (see thumbnails below).
function thumbHTML(p) {
  const url = p.pdf_mtime && thumbs.get(`${p.id}:${p.pdf_mtime}`);
  return `<div class="thumb" data-thumb="${p.id}">${url ? `<img src="${url}" alt="">`
    : `<div class="ph"><div class="ini">${p.exists ? initials(p.name) : "!"}</div><small>${!p.exists ? "Folder not found" : p.pdf_mtime ? "" : "No PDF yet"}</small></div>`}</div>`;
}
function itemAttrs(it) {
  const sel = H.sel.has(it.key);
  return `class="it ${it.k === "f" ? "is-folder" : "is-project"} ${sel ? "sel" : ""} ${H.cursor === it.key ? "cur" : ""} ${it.k === "p" && !it.p.exists ? "missing" : ""}"
    data-key="${it.key}" ${it.k === "f" ? `data-fid="${it.id}" data-drop="${it.id}"` : `data-id="${it.id}"`}
    role="option" aria-selected="${sel}" draggable="true"`;
}
function rowHTML(it, flat) {
  if (it.k === "f") {
    const f = it.f;
    return `<div ${itemAttrs(it)} title="${esc(f.note || f.name)}">
      <span class="c-name">${icon("folder", "ico-folder")}<span class="nm">${esc(f.name)}</span></span>
      ${dateCell(folderTime(f, "edited"), "c-edited")}${dateCell(folderTime(f, "opened"), "c-opened")}
      ${flat ? whereCell(f.parent) : ""}
      <span class="c-status"><span class="muted">${folderCounts(f.id)}</span>${syncChip(f.id)}</span><span class="c-tags"></span></div>`;
  }
  const p = it.p;
  return `<div ${itemAttrs(it)} title="${esc(p.title && p.title.toLowerCase() !== p.name.toLowerCase() ? p.title : p.path)}">
    <span class="c-name">${icon("doc", "ico-doc")}<span class="nm">${esc(p.name)}</span>${pinMark(p)}${runDot(p)}</span>
    ${dateCell(p.edited, "c-edited")}${dateCell(p.opened, "c-opened")}
    ${flat ? whereCell(folderOf(p)) : ""}
    <span class="c-status">${statusHTML(p)}</span>
    <span class="c-tags">${(p.tags || []).map(tagChip).join("")}</span></div>`;
}
function tileHTML(it, flat) {
  if (it.k === "f") {
    const f = it.f;
    return `<div ${itemAttrs(it)} title="${esc(f.note || f.name)}">
      <div class="thumb folder-thumb">${icon("folder")}</div>
      <div class="nm">${esc(f.name)}</div><div class="sub">${flat ? esc(whereText(f.parent)) : folderCounts(f.id)}</div></div>`;
  }
  const p = it.p;
  return `<div ${itemAttrs(it)} title="${esc(p.path)}">
    ${thumbHTML(p)}
    <div class="nm">${runDot(p)}${esc(p.name)}${pinMark(p)}</div>
    <div class="sub">${flat ? esc(whereText(folderOf(p))) : !p.exists ? "Folder not found" : H.busy.has(p.id) ? "Starting…" : p.edited ? "Modified " + ago(p.edited) : ""}</div></div>`;
}
function headHTML(flat) {
  const col = (key, cls) => {
    const on = H.sortKey === key;
    return `<button class="${cls} ${on ? "on" : ""}" data-act="sort" data-sort="${key}" aria-sort="${on ? (H.sortDir > 0 ? "ascending" : "descending") : "none"}">
      ${SORTS[key].label}${on ? icon(H.sortDir > 0 ? "up" : "down", "sort-ico") : ""}</button>`;
  };
  return `<div class="it-head" role="presentation">${col("name", "c-name")}${col("edited", "c-edited")}${col("opened", "c-opened")}
    ${flat ? col("where", "c-where") : ""}<span class="c-status">Status</span><span class="c-tags">Tags</span></div>`;
}

/* ------------------------------------------------------------------ sidebar (navigation pane) */
function sideRow({ act, attrs = "", ico, label, n, active, cls = "", depth = 0, twisty = "", more = "", drop = null, drag = "" }) {
  return `<div class="side-row ${cls} ${active ? "active" : ""}" role="button" tabindex="0" data-act="${act}" ${attrs}
      style="--depth:${depth}" ${drop !== null ? `data-drop="${drop}"` : ""} ${drag}>
    ${twisty}${ico}<span class="label">${label}</span>
    <span class="n">${n || ""}</span>${more}</div>`;
}
// A sort order for a sidebar list; H[key] holds it (and the store, as home.<key>).
const sortSelect = (key, title, options) => `<select class="side-sort" data-sort="${key}" title="${title}" aria-label="${title}">` +
  options.map(([v, label]) => `<option value="${v}" ${H[key] === v ? "selected" : ""}>${label}</option>`).join("") + `</select>`;
$("#side").addEventListener("change", (e) => {
  const key = e.target.dataset && e.target.dataset.sort;
  if (!key) return;
  H[key] = e.target.value; store.set("home." + key, H[key]); render();
});
function renderSide() {
  const v = H.view, count = (f) => H.projects.filter(f).length;
  const views = [
    sideRow({ act: "view", attrs: `data-view="all"`, ico: icon("home"), label: "Projects", n: H.projects.length, active: v.kind === "all", drop: "" }),
    sideRow({ act: "view", attrs: `data-view="pinned"`, ico: icon("star"), label: "Pinned", n: count((p) => p.pinned), active: v.kind === "pinned" }),
    sideRow({ act: "view", attrs: `data-view="unfiled"`, ico: icon("inbox"), label: "Not in a folder", n: count((p) => !folderOf(p)), active: v.kind === "unfiled", drop: "" }),
  ].join("");
  const hidden = new Set();
  const folders = folderTree().map(({ f, depth }) => {
    if (f.parent && (hidden.has(f.parent) || H.collapsed.has(f.parent))) { hidden.add(f.id); return ""; }
    const kids = childFolders(f.id).length, open = !H.collapsed.has(f.id);
    const twisty = kids
      ? `<button class="twisty ${open ? "open" : ""}" data-act="twisty" data-fid="${f.id}" tabindex="-1" aria-label="${open ? "Collapse" : "Expand"}">${icon("right")}</button>`
      : `<span class="twisty"></span>`;
    return sideRow({
      act: "folder", attrs: `data-fid="${f.id}" title="${esc(f.note || f.name)}"`, cls: "side-folder", depth, twisty,
      ico: icon("folder"), label: esc(f.name), n: count((p) => inFolder(p, f.id)),
      active: v.kind === "folder" && v.id === f.id, drop: f.id, drag: `draggable="true"`,
      more: `<button class="more" data-act="folder-menu" data-fid="${f.id}" tabindex="-1" aria-label="Folder actions">${icon("more")}</button>`,
    });
  }).join("");
  const tags = allTags().map((t) => sideRow({
    act: "tag", attrs: `data-tag="${esc(t.name)}"`, cls: "side-tag",
    ico: `<span class="dot" style="--tag:${tagColor(t.name)}"></span>`, label: esc(t.name), n: t.n,
    active: v.kind === "tag" && v.id === t.name,
    more: `<button class="more" data-act="tag-menu" data-tag="${esc(t.name)}" tabindex="-1" aria-label="Tag actions">${icon("more")}</button>`,
  })).join("");
  $("#side").innerHTML = `<nav class="side-views">${views}</nav>
    <div class="side-head" data-drop="" title="Drop a folder here to move it to the top level"><span>Folders</span>
      <span class="side-tools">${sortSelect("folderSort", "Sort folders", [["name", "Name"], ["edited", "Recently edited"], ["opened", "Recently opened"]])}
      <button class="tiny icon ghost" data-act="new-folder" title="New folder" aria-label="New folder">${icon("plus")}</button></span></div>
    <div class="side-list">${folders || `<p class="side-empty">Group projects by topic: a folder can hold projects and subfolders.</p>`}</div>
    <div class="side-head"><span>Tags</span>
      <span class="side-tools">${sortSelect("tagSort", "Sort tags", [["name", "Name"], ["count", "Most used"]])}</span></div>
    <div class="side-list">${tags || `<p class="side-empty">Add tags from a project's right-click menu.</p>`}</div>`;
}

/* ------------------------------------------------------------------ address bar, command bar */
function renderBars(n) {
  const v = H.view;
  $("#nav-back").disabled = H.histPos <= 0;
  $("#nav-fwd").disabled = H.histPos >= H.hist.length - 1;
  $("#nav-up").disabled = v.kind === "all";
  const root = `<button class="crumb" data-act="view" data-view="all" data-drop="">${icon("home")}Projects</button>`;
  const sep = `<span class="sep">${icon("right")}</span>`;
  const path = v.kind === "folder" ? folderPath(v.id) : [];
  $("#crumbs").innerHTML = root + (v.kind === "folder"
    ? path.map((f) => sep + `<button class="crumb" data-act="folder" data-fid="${f.id}" data-drop="${f.id}">${esc(f.name)}</button>`).join("")
    : v.kind === "all" ? "" : sep + `<span class="crumb here">${esc(locationName())}</span>`);
  $("#search").placeholder = `Search ${locationName()}`;
  const fid = v.kind === "folder" ? v.id : null, f = fid && folderById(fid);
  $("#loc-sync").innerHTML = fid ? syncChip(fid) : "";
  $("#loc-menu").hidden = !fid;
  const note = $("#folder-note");
  note.textContent = (f && f.note) || ""; note.hidden = !(f && f.note);
  const sel = selected(), one = sel.length === 1 ? sel[0] : null;
  $("#cmd-open").disabled = !one || (one.k === "p" && !one.p.exists);
  $("#cmd-rename").disabled = !one || (one.k === "p" && !one.p.exists);
  $("#cmd-move").disabled = !sel.length;
  $("#cmd-remove").disabled = !sel.length;
  $("#cmd-remove").title = sel.length && sel.every((it) => it.k === "f") ? "Delete the folder (its projects move up; no files are touched)"
    : "Remove from the list (no files are touched)";
  // Location sorts only the flat lists, which show that column.
  const flat = v.kind !== "all" && v.kind !== "folder" || !!$("#search").value.trim();
  $("#sort").querySelector("[value=where]").hidden = !flat;
  $("#sort").value = H.sortKey === "where" && !flat ? "name" : H.sortKey;
  $("#sort-dir").innerHTML = icon(H.sortDir > 0 ? "arrowup" : "arrowdown");
  $("#sort-dir").title = H.sortDir > 0 ? "Ascending" : "Descending";
  $("#mode-details").classList.toggle("on", H.mode === "details");
  $("#mode-icons").classList.toggle("on", H.mode === "icons");
  $("#btn-preview").classList.toggle("on", H.preview);
  const running = H.projects.filter((p) => p.running).length;
  const behind = H.projects.filter((p) => H.git[p.id] && H.git[p.id].behind).length;
  const fetchNote = H.fetch && H.fetch.running ? `Checking GitHub (${H.fetch.done}/${H.fetch.total})…`
    : behind ? `${plural(behind, "project")} with new changes on GitHub` : "";
  $("#status-left").textContent = plural(n, "item") + (sel.length ? `   ·   ${sel.length} selected` : "");
  $("#status-right").textContent = [running && `${running} open in an editor`, fetchNote].filter(Boolean).join("   ·   ");
}

/* ------------------------------------------------------------------ preview pane */
function previewHTML() {
  const sel = selected();
  if (sel.length > 1) {
    const ps = sel.filter((it) => it.k === "p").length, fs = sel.length - ps;
    return `<div class="pv-multi">${icon("list")}<h2>${plural(sel.length, "item")} selected</h2>
      <p class="muted">${[fs && plural(fs, "folder"), ps && plural(ps, "project")].filter(Boolean).join(", ")}</p>
      <div class="pv-actions"><button data-act="move-sel">Move to folder…</button></div></div>`;
  }
  if (sel.length === 1 && sel[0].k === "p") {
    const p = sel[0].p, fid = folderOf(p);
    const title = p.title && p.title.toLowerCase() !== p.name.toLowerCase() ? `<div class="pv-title">${esc(p.title)}</div>` : "";
    return `<div class="pv-project" data-id="${p.id}">${thumbHTML(p)}
      <h2>${runDot(p)}${esc(p.name)}</h2>${title}
      <div class="pv-actions">
        <button class="${p.running ? "" : "primary"}" data-act="open" ${!p.exists || H.busy.has(p.id) ? "disabled" : ""}>${H.busy.has(p.id) ? "Starting…" : p.running ? "Show editor" : "Open"}</button>
        <button class="icon ${p.pinned ? "on" : ""}" data-act="pin" title="${p.pinned ? "Unpin" : "Pin"}" aria-label="${p.pinned ? "Unpin" : "Pin"}">${icon("star")}</button>
        <button class="icon" data-act="menu" title="More actions" aria-label="More actions">${icon("more")}</button></div>
      <dl>
        <dt>Status</dt><dd>${statusHTML(p)}</dd>
        <dt>Modified</dt><dd>${p.edited ? `${fmtDate(p.edited)} <span class="muted">(${ago(p.edited)})</span>` : "—"}</dd>
        <dt>Opened</dt><dd>${p.opened ? `${fmtDate(p.opened)} <span class="muted">(${ago(p.opened)})</span>` : "—"}</dd>
        ${p.exists ? `<dt>Files</dt><dd>${p.files ? plural(p.files, ".tex file") : "No .tex files"}</dd>` : ""}
        <dt>In</dt><dd><button class="linkish" ${fid ? `data-act="folder" data-fid="${fid}"` : `data-act="view" data-view="all"`}>${icon("folder")}${esc(whereText(fid))}</button></dd>
        <dt>Path</dt><dd class="pv-path">${esc(p.path)}</dd>
        <dt>Tags</dt><dd>${(p.tags || []).map(tagChip).join("") || `<button class="linkish" data-act="tags">Add tags…</button>`}</dd>
      </dl></div>`;
  }
  const f = sel.length === 1 ? sel[0].f : H.view.kind === "folder" ? folderById(H.view.id) : null;
  if (f) {
    const all = H.projects.filter((p) => inFolder(p, f.id)).length;
    return `<div class="pv-folder"><div class="thumb folder-thumb">${icon("folder")}</div><h2>${esc(f.name)}</h2>
      ${f.note ? `<p class="pv-note">${esc(f.note)}</p>` : ""}
      <div class="pv-actions">${sel.length ? `<button class="primary" data-act="folder" data-fid="${f.id}">Open</button>` : ""}
        <button class="icon" data-act="folder-menu" data-fid="${f.id}" title="Folder actions" aria-label="Folder actions">${icon("more")}</button></div>
      <dl><dt>Contains</dt><dd>${folderCounts(f.id)}</dd>
        <dt>In total</dt><dd>${plural(all, "project")}</dd>
        <dt>Modified</dt><dd>${folderTime(f, "edited") ? fmtDate(folderTime(f, "edited")) : "—"}</dd>
        <dt>In</dt><dd>${esc(whereText(f.parent))}</dd>
        <dt>GitHub</dt><dd>${syncChip(f.id) || `<button class="linkish" data-act="sync" data-fid="${f.id}">Set up sync…</button>`}</dd></dl></div>`;
  }
  return `<div class="pv-none">${icon("doc")}<p>Select a project to see its first page and details.</p></div>`;
}

/* ------------------------------------------------------------------ render */
let shownKeys = [];                // the items in the order shown, for selection and keys
const selected = () => [...H.sel].map(itemByKey).filter(Boolean);
function render() {
  if ((H.view.kind === "folder" && !folderById(H.view.id)) ||
      (H.view.kind === "tag" && !allTags().some((t) => t.name === H.view.id)) ||
      !["all", "pinned", "unfiled", "folder", "tag"].includes(H.view.kind)) H.view = { kind: "all" };
  const q = $("#search").value.trim(), v = H.view;
  const { flat, items } = viewItems(q);
  sortItems(items, flat);
  shownKeys = items.map((it) => it.key);
  for (const k of [...H.sel]) if (!shownKeys.includes(k)) H.sel.delete(k);
  if (H.cursor && !shownKeys.includes(H.cursor)) H.cursor = null;
  renderSide(); renderBars(items.length);
  const view = $("#view");
  view.className = H.mode === "icons" ? "icons" : "details";
  view.innerHTML = !items.length ? ""
    : H.mode === "icons" ? items.map((it) => tileHTML(it, flat)).join("")
    : headHTML(flat) + items.map((it) => rowHTML(it, flat)).join("");
  view.style.setProperty("--cols", flat
    ? "minmax(200px, 2.2fr) 140px 140px minmax(110px, 1fr) minmax(150px, 1.4fr) minmax(90px, 1fr)"
    : "minmax(220px, 2.6fr) 140px 140px minmax(150px, 1.4fr) minmax(90px, 1fr)");
  $("#home-layout").classList.toggle("no-preview", !H.preview);
  if (H.preview) $("#preview").innerHTML = previewHTML();
  $("#welcome").hidden = !H.loaded || H.projects.length > 0 || H.folders.length > 0;
  $("#no-match").hidden = !q || items.length > 0;
  const empty = $("#empty-view");
  empty.hidden = !H.loaded || !!q || items.length > 0 || !$("#welcome").hidden;
  empty.innerHTML = {
    folder: "This folder is empty. Drag projects or folders here, or create one with <b>+ New project</b>.",
    pinned: "No pinned projects. Pin one from its right-click menu or the ☆ in the preview pane.",
    unfiled: "Every project is in a folder.",
    all: "No projects yet.",
  }[v.kind] || "";
  document.querySelectorAll("[data-thumb]").forEach(attachThumb);
}

// Selecting only repaints: the items stay the same elements, so a double-click still
// lands on the element its first click selected.
function paintSelection() {
  for (const el of document.querySelectorAll("#view .it[data-key]")) {
    const k = el.dataset.key, on = H.sel.has(k);
    el.classList.toggle("sel", on); el.classList.toggle("cur", k === H.cursor);
    el.setAttribute("aria-selected", on);
  }
  renderBars(shownKeys.length);
  if (H.preview) { $("#preview").innerHTML = previewHTML(); $("#preview").querySelectorAll("[data-thumb]").forEach(attachThumb); }
}

/* ------------------------------------------------------------------ PDF thumbnails
   The first page, drawn once per PDF version and kept as an image URL, so the tile and
   the preview pane can both show it. */
const io = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { io.unobserve(e.target); drawThumb(byId(e.target.dataset.thumb)); }
}, { rootMargin: "200px" });
function attachThumb(el) {
  const p = byId(el.dataset.thumb);
  if (p && p.exists && p.pdf_mtime && !thumbs.get(`${p.id}:${p.pdf_mtime}`)) io.observe(el);
}
async function drawThumb(p) {
  const key = p && `${p.id}:${p.pdf_mtime}`;
  if (!p || thumbs.has(key)) return;
  thumbs.set(key, null);
  try {
    const data = await fetch(`/api/pdf?id=${encodeURIComponent(p.id)}&t=${p.pdf_mtime}`).then((r) => r.ok ? r.arrayBuffer() : Promise.reject());
    const doc = await pdfjsLib.getDocument({ data, disableFontFace: true }).promise;   // see pdfview.js
    const page = await doc.getPage(1);
    const base = page.getViewport({ scale: 1 });
    const vp = page.getViewport({ scale: (300 / base.width) * Math.min(2, window.devicePixelRatio || 1) });
    const canvas = document.createElement("canvas");
    canvas.width = vp.width; canvas.height = vp.height;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
    await page.render({ canvasContext: ctx, viewport: vp }).promise;
    doc.destroy();
    const url = canvas.toDataURL("image/jpeg", 0.88);
    for (const k of thumbs.keys()) if (k.startsWith(p.id + ":") && k !== key) thumbs.delete(k);
    thumbs.set(key, url);
    document.querySelectorAll(`[data-thumb="${p.id}"]`).forEach((el) => { el.innerHTML = `<img src="${url}" alt="">`; });
  } catch {
    thumbs.delete(key);
  }
}

/* ------------------------------------------------------------------ actions */
async function openProject(id) {
  const p = byId(id);
  if (!p || !p.exists || H.busy.has(id)) return;
  // Open the tab synchronously (inside the click) so the popup blocker allows it, then
  // point it at the editor once its server answers. The tab is named per project, so a
  // second click focuses the editor that is already open instead of opening another.
  const w = window.open("", "prism-" + id);
  let fresh = true;
  try { fresh = w && w.location.href === "about:blank"; } catch { fresh = false; }
  if (w && !fresh && p.running) { w.focus(); return; }
  if (w && fresh) {
    w.document.title = p.name + " · starting…";
    w.document.body.innerHTML = `<p style="font:15px system-ui;color:#6f6a60;margin:40vh auto;text-align:center">Starting prism-local for <b>${esc(p.name)}</b>…</p>`;
  }
  H.busy.add(id); render();
  const r = await api("/api/projects/open", { id }).catch(() => ({ error: "Home server not reachable" }));
  H.busy.delete(id);
  if (r.url) {
    if (w) { w.location.href = r.url; w.focus(); } else { location.href = r.url; }
    p.running = { url: r.url, pages: 0 }; p.opened = Date.now() / 1000;
  } else {
    if (w && fresh) w.close();
    toast(`Could not open ${p.name}: ${r.error || "unknown error"}` + (r.log ? "\n\n" + r.log : ""), true);
  }
  render();
  setTimeout(load, 1500);
}

async function setPinned(id, on) {
  const p = byId(id); if (!p) return;
  p.pinned = on; render();
  const r = await api("/api/projects/update", { id, pinned: on });
  if (r._status !== 200) { toast(r.error || "Could not save", true); load(); }
}
async function removeProject(id) {
  const p = byId(id); if (!p) return;
  if (!confirm(`Remove "${p.name}" from the list?\n\nThe folder and its files are not touched:\n${p.path}`)) return;
  const r = await api("/api/projects/remove", { id });
  if (r._status !== 200) return toast(r.error || "Could not remove", true);
  H.projects = H.projects.filter((x) => x.id !== id); render();
  toast(`Removed ${p.name} from the list`);
}
async function reveal(id) {
  const r = await api("/api/projects/reveal", { id });
  if (r._status !== 200) toast(r.error || "Could not open the folder", true);
}
async function copyPath(id) {
  const p = byId(id); if (!p) return;
  try { await navigator.clipboard.writeText(p.path); toast("Path copied"); } catch { toast(p.path); }
}

/* folders, tags: moving and editing */
async function post(path, body, what) {
  const r = await api(path, body);
  if (r._status !== 200) { toast(r.error || `Could not ${what}`, true); }
  H.sig = null; await load();
  return r;
}
// Move projects and folders (keys "p:id", "f:id") into folder fid ("": the top level).
async function moveItems(keys, fid) {
  fid = fid || "";
  const ps = keys.filter((k) => k[0] === "p").map((k) => byId(k.slice(2))).filter((p) => p && (folderOf(p) || "") !== fid);
  const fs = keys.filter((k) => k[0] === "f").map((k) => folderById(k.slice(2)))
    .filter((f) => f && (f.parent || "") !== fid && f.id !== fid);
  if (fs.some((f) => fid && isInside(fid, f.id))) return toast("A folder cannot go inside itself", true);
  if (!ps.length && !fs.length) return;
  for (const p of ps) p.folder_id = fid || null;
  for (const f of fs) f.parent = fid || null;
  render();
  let err = "";
  for (const p of ps) { const r = await api("/api/projects/update", { id: p.id, folder_id: fid }); if (r._status !== 200) err = r.error || "Could not move the project"; }
  for (const f of fs) { const r = await api("/api/folders/update", { id: f.id, parent: fid }); if (r._status !== 200) err = r.error || "Could not move the folder"; }
  H.sig = null; await load();
  const what = ps.length + fs.length === 1 ? (ps[0] || fs[0]).name : plural(ps.length + fs.length, "item");
  toast(err || (fid ? `Moved ${what} to ${folderById(fid) ? folderById(fid).name : "the folder"}` : `Moved ${what} to the top level`), !!err);
}
const moveFolder = (fid, parent) => moveItems(["f:" + fid], parent);
async function removeFolder(fid) {
  const f = folderById(fid); if (!f) return;
  const n = H.projects.filter((p) => folderOf(p) === fid).length, subs = childFolders(fid).length;
  const up = f.parent ? `“${folderById(f.parent).name}”` : "the top level";
  const what = [n && plural(n, "project"), subs && plural(subs, "subfolder")].filter(Boolean).join(" and ");
  if (!confirm(`Delete the folder “${f.name}”?` + (what ? `\n\nIts ${what} move to ${up}.` : "") +
               "\nNo project files are touched.")) return;
  if (H.view.kind === "folder" && isInside(H.view.id, fid)) setView(f.parent ? { kind: "folder", id: f.parent } : { kind: "all" });
  await post("/api/folders/remove", { id: fid }, "delete the folder");
}
async function removeTag(name) {
  const n = H.projects.filter((p) => (p.tags || []).includes(name)).length;
  if (!confirm(`Remove the tag “${name}”` + (n ? ` from ${plural(n, "project")}?` : "?"))) return;
  await post("/api/tags/remove", { name }, "remove the tag");
}
function toggleFolder(fid) {
  if (H.collapsed.has(fid)) H.collapsed.delete(fid); else H.collapsed.add(fid);
  store.set("home.collapsed", [...H.collapsed]); renderSide();
}

/* clicks: buttons anywhere, and selecting items like Explorer (Ctrl, Shift) */
document.addEventListener("click", (e) => {
  const act = e.target.closest("[data-act]");
  if (!e.target.closest("#menu")) closeMenu();
  if (!act) return;
  const holder = act.closest("[data-id]");
  const id = holder && holder.dataset.id, d = act.dataset;
  switch (d.act) {
    case "open": return openProject(id);
    case "pin": e.stopPropagation(); return setPinned(id, !byId(id).pinned);
    case "remove": return removeProject(id);
    case "menu": e.stopPropagation(); return showMenu(act, projectMenu(id));
    case "tags": return openTags(id);
    case "new": return openNew();
    case "add": return openAdd();
    case "view": return setView({ kind: d.view });
    case "folder": e.stopPropagation(); return setView({ kind: "folder", id: d.fid });
    case "tag": e.stopPropagation(); return setView({ kind: "tag", id: d.tag });
    case "twisty": e.stopPropagation(); return toggleFolder(d.fid);
    case "new-folder": e.stopPropagation(); return openFolder(null, d.parent || currentFolder());
    case "folder-menu": e.stopPropagation(); return showMenu(act, folderMenu(d.fid));
    case "tag-menu": e.stopPropagation(); return showMenu(act, tagMenu(d.tag));
    case "sync": e.stopPropagation(); return openSync(d.fid);
    case "publish": e.stopPropagation(); return openPublish(id);
    case "sort": return setSort(d.sort);
    case "move-sel": return openMove([...H.sel]);
  }
});
// The sidebar's rows are not buttons (they hold buttons): Enter and Space work on them too.
$("#side").addEventListener("keydown", (e) => {
  const row = e.target.closest(".side-row");
  if (row && e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); row.click(); }
});
function select(keys, { cursor = keys[keys.length - 1], anchor = true } = {}) {
  H.sel = new Set(keys); H.cursor = cursor || null;
  if (anchor) H.anchor = H.cursor;
  paintSelection();
  const el = H.cursor && document.querySelector(`#view [data-key="${H.cursor}"]`);
  if (el) el.scrollIntoView({ block: "nearest" });
}
function rangeTo(key) {
  const a = shownKeys.indexOf(H.anchor), b = shownKeys.indexOf(key);
  return a < 0 ? [key] : shownKeys.slice(Math.min(a, b), Math.max(a, b) + 1);
}
$("#view").addEventListener("click", (e) => {
  if (e.target.closest("[data-act], a")) return;
  const el = e.target.closest(".it[data-key]");
  if (!el) { if (H.sel.size) select([]); return; }
  const k = el.dataset.key;
  if (e.shiftKey) select(rangeTo(k), { cursor: k, anchor: false });
  else if (e.ctrlKey || e.metaKey) { const s = new Set(H.sel); if (s.has(k)) s.delete(k); else s.add(k); select([...s], { cursor: k }); }
  else select([k]);
  $("#view").focus({ preventScroll: true });
});
$("#view").addEventListener("dblclick", (e) => {
  const el = e.target.closest(".it[data-key]");
  if (el && !e.target.closest("[data-act], a")) activate(el.dataset.key);
});
// Double-click, Enter: a folder opens in the view, a project in its editor.
function activate(key) {
  const it = itemByKey(key); if (!it) return;
  if (it.k === "f") setView({ kind: "folder", id: it.id });
  else if (it.p.exists) openProject(it.id);
  else toast(`${it.p.name}: its folder was moved or deleted`, true);
}
function renameSel() {
  const sel = selected(); if (sel.length !== 1) return;
  if (sel[0].k === "f") openFolder(sel[0].id); else if (sel[0].p.exists) openRename(sel[0].id);
}
async function removeSel() {
  const sel = selected(); if (!sel.length) return;
  if (sel.length === 1) return sel[0].k === "f" ? removeFolder(sel[0].id) : removeProject(sel[0].id);
  const ps = sel.filter((it) => it.k === "p"), fs = sel.filter((it) => it.k === "f");
  const what = [ps.length && `remove ${plural(ps.length, "project")} from the list`, fs.length && `delete ${plural(fs.length, "folder")} (what is in them moves up)`].filter(Boolean).join(" and ");
  if (!confirm(`${what[0].toUpperCase() + what.slice(1)}?\n\nNo files on disk are touched.`)) return;
  let err = "";
  for (const it of ps) { const r = await api("/api/projects/remove", { id: it.id }); if (r._status !== 200) err = r.error || "Could not remove"; }
  for (const it of fs) { const r = await api("/api/folders/remove", { id: it.id }); if (r._status !== 200) err = r.error || "Could not delete the folder"; }
  H.sel.clear(); H.sig = null; await load();
  if (err) toast(err, true);
}
$("#nav-back").onclick = () => goHistory(-1);
$("#nav-fwd").onclick = () => goHistory(1);
$("#nav-up").onclick = goUp;
$("#cmd-new-folder").onclick = () => openFolder(null, currentFolder());
$("#cmd-open").onclick = () => { if (H.sel.size === 1) activate([...H.sel][0]); };
$("#cmd-rename").onclick = renameSel;
$("#cmd-move").onclick = () => openMove([...H.sel]);
$("#cmd-remove").onclick = removeSel;
$("#loc-menu").onclick = (e) => { e.stopPropagation(); if (currentFolder()) showMenu(e.currentTarget, folderMenu(currentFolder())); };
$("#sort").onchange = () => setSort($("#sort").value, SORTS[$("#sort").value].dir);
$("#sort-dir").onclick = () => setSort(H.sortKey, -H.sortDir);
const setMode = (m) => { H.mode = m; store.set("home.mode", m); render(); };
$("#mode-details").onclick = () => setMode("details");
$("#mode-icons").onclick = () => setMode("icons");
$("#btn-preview").onclick = () => { H.preview = !H.preview; store.set("home.preview", H.preview); render(); };
// The mouse's back and forward buttons.
window.addEventListener("mouseup", (e) => {
  if (e.button === 3) { e.preventDefault(); goHistory(-1); }
  if (e.button === 4) { e.preventDefault(); goHistory(1); }
});
document.addEventListener("contextmenu", (e) => {
  if (e.target.closest("a, input, textarea, select, dialog")) return;
  const it = e.target.closest("#view .it[data-key]"), side = e.target.closest(".side-folder[data-fid]"),
        tag = e.target.closest(".side-tag[data-tag]"), pv = e.target.closest(".pv-project[data-id]");
  let menu = null;
  if (it) {
    if (!H.sel.has(it.dataset.key)) select([it.dataset.key]);
    menu = H.sel.size > 1 ? selectionMenu() : it.dataset.fid ? folderMenu(it.dataset.fid) : projectMenu(it.dataset.id);
  } else if (pv) menu = projectMenu(pv.dataset.id);
  else if (side) menu = folderMenu(side.dataset.fid);
  else if (tag) menu = tagMenu(tag.dataset.tag);
  else if (e.target.closest("#view-wrap")) { if (H.sel.size) select([]); menu = backgroundMenu(); }
  if (!menu) return;
  e.preventDefault(); showMenu(null, menu, { x: e.clientX, y: e.clientY });
});

/* ------------------------------------------------------------------ menus
   A menu is [[label, fn, cls?], "-", …]. */
let menuItems = [];
function projectMenu(id) {
  const p = byId(id);
  const org = [["Move to folder…", () => openMove(["p:" + id])], ["Tags…", () => openTags(id)]];
  return p.exists ? [
    [p.running ? "Show editor" : "Open", () => openProject(id)],
    [p.pinned ? "Unpin" : "Pin to top", () => setPinned(id, !p.pinned)],
    ["Rename…", () => openRename(id)],
    "-", ...org,
    ...(H.git[id] && H.git[id].github ? [] : H.git[id] && H.git[id].nested ? [] : [["Create GitHub repository…", () => openPublish(id)]]),
    "-",
    ["Show in folder", () => reveal(id)],
    ...(H.git[id] && H.git[id].github ? [["Open on GitHub", () => window.open(H.git[id].github, "_blank", "noopener")]] : []),
    ["Copy path", () => copyPath(id)],
    "-",
    ["Remove from list…", () => removeProject(id), "danger"],
  ] : [...org, "-", ["Copy path", () => copyPath(id)], "-", ["Remove from list…", () => removeProject(id), "danger"]];
}
function folderMenu(fid) {
  const f = folderById(fid);
  return [
    ["Open", () => setView({ kind: "folder", id: fid })],
    ["New project here…", () => { setView({ kind: "folder", id: fid }); openNew(); }],
    ["New subfolder…", () => openFolder(null, fid)],
    "-",
    ["Rename, move, notes…", () => openFolder(fid)],
    ["GitHub sync…", () => openSync(fid)],
    ...(f.parent ? [["Move to top level", () => moveFolder(fid, "")]] : []),
    "-",
    ["Delete folder…", () => removeFolder(fid), "danger"],
  ];
}
function selectionMenu() {
  const sel = selected(), ps = sel.filter((it) => it.k === "p").map((it) => it.p);
  const pin = ps.length && ps.some((p) => !p.pinned);
  return [
    ["Move to folder…", () => openMove([...H.sel])],
    ...(ps.length ? [[pin ? "Pin" : "Unpin", () => ps.forEach((p) => setPinned(p.id, pin))]] : []),
    "-",
    ["Remove…", removeSel, "danger"],
  ];
}
// Right-click on the view's empty space.
function backgroundMenu() {
  const fid = currentFolder(), check = (on) => (on ? "✓ " : "    ");
  return [
    ["New project…", openNew], ["New folder…", () => openFolder(null, fid)],
    "-",
    [check(H.mode === "details") + "Details", () => setMode("details")],
    [check(H.mode === "icons") + "Large icons", () => setMode("icons")],
    "-",
    ...["name", "edited", "opened"].map((k) => [check(H.sortKey === k) + "Sort by " + SORTS[k].label.toLowerCase(), () => setSort(k, SORTS[k].dir)]),
    ...(fid ? ["-", ["Folder: rename, move, notes…", () => openFolder(fid)], ["Folder: GitHub sync…", () => openSync(fid)]] : []),
  ];
}
function tagMenu(name) {
  return [
    ["Show projects", () => setView({ kind: "tag", id: name })],
    ["Rename, color…", () => openTag(name)],
    "-",
    ["Remove tag…", () => removeTag(name), "danger"],
  ];
}
function showMenu(anchor, items, at = null) {
  const m = $("#menu");
  menuItems = items;
  m.innerHTML = items.map((it, i) => it === "-" ? "<hr>" : `<button data-m="${i}" class="${it[2] || ""}" role="menuitem">${esc(it[0])}</button>`).join("");
  m.hidden = false;
  const mw = m.offsetWidth, mh = m.offsetHeight;
  const r = anchor ? anchor.getBoundingClientRect() : { left: at.x, right: at.x + mw, top: at.y, bottom: at.y };
  m.style.left = Math.max(8, Math.min(anchor ? r.right - mw : r.left, innerWidth - mw - 8)) + "px";
  m.style.top = Math.max(8, r.bottom + mh + 6 > innerHeight ? r.top - mh - 4 : r.bottom + 4) + "px";
  m.querySelector("button").focus();
}
function closeMenu() { $("#menu").hidden = true; }
$("#menu").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-m]"); if (!b) return;
  closeMenu(); menuItems[+b.dataset.m][1]();
});
window.addEventListener("scroll", closeMenu, { passive: true });
window.addEventListener("resize", closeMenu);

/* ------------------------------------------------------------------ drag and drop
   Projects and folders (the selection, when the dragged item is in it) move onto a folder:
   in the view, the sidebar or the address bar. Onto "Projects", "Not in a folder" or the
   "Folders" heading, they go to the top level. */
const DRAG = "application/x-prism-items";
let dropOn = null, dragKeys = [];
function markDrop(el) {
  if (dropOn === el) return;
  if (dropOn) dropOn.classList.remove("drop-on");
  dropOn = el; if (el) el.classList.add("drop-on");
}
// The element a drag over e.target would drop on, if it accepts what is dragged.
function dropTarget(e) {
  if (!e.dataTransfer.types.includes(DRAG)) return null;
  const el = e.target.closest && e.target.closest("[data-drop]");
  if (!el) return null;
  const fid = el.dataset.drop;
  // Not onto itself, nor a folder into its own subfolder.
  if (dragKeys.some((k) => k === "f:" + fid || (k[0] === "f" && fid && isInside(fid, k.slice(2))))) return null;
  return el;
}
document.addEventListener("dragstart", (e) => {
  const it = e.target.closest && e.target.closest("#view .it[data-key]");
  const side = e.target.closest && e.target.closest(".side-folder[data-fid]");
  if (it) {
    if (!H.sel.has(it.dataset.key)) select([it.dataset.key]);
    dragKeys = [...H.sel];
  } else if (side) dragKeys = ["f:" + side.dataset.fid];
  else return;
  e.dataTransfer.setData(DRAG, JSON.stringify(dragKeys));
  e.dataTransfer.effectAllowed = "move"; H.dragging = true; closeMenu();
});
document.addEventListener("dragend", () => { H.dragging = false; dragKeys = []; markDrop(null); });
document.addEventListener("dragover", (e) => {
  const el = dropTarget(e);
  markDrop(el);
  if (el) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }
});
document.addEventListener("drop", (e) => {
  const el = dropTarget(e);
  markDrop(null); H.dragging = false;
  if (!el) return;
  e.preventDefault();
  const keys = JSON.parse(e.dataTransfer.getData(DRAG) || "[]");
  dragKeys = [];
  moveItems(keys, el.dataset.drop);
});

/* ------------------------------------------------------------------ dialogs */
function dlgError(dlg, msg) { const el = dlg.querySelector(".dlg-error"); el.textContent = msg || ""; el.hidden = !msg; }
document.querySelectorAll("dialog [data-close]").forEach((b) => { b.onclick = () => b.closest("dialog").close(); });
document.querySelectorAll("dialog [data-browse]").forEach((b) => {
  b.onclick = async () => {
    const input = b.closest("form").elements[b.dataset.browse];
    b.disabled = true; b.textContent = "Choosing…";
    const r = await api("/api/pick-folder", { start: input.value || H.defaultParent, title: b.closest("dialog").querySelector("h3").textContent });
    b.disabled = false; b.textContent = "Browse…";
    if (r.path) { input.value = r.path; input.dispatchEvent(new Event("input")); }
    else if (r.error) dlgError(b.closest("dialog"), r.error + ". Type the path instead.");
  };
});

const sep = () => (H.defaultParent.includes("\\") ? "\\" : "/");
function updateTarget() {
  const f = $("#form-new");
  const parent = f.elements.parent.value.replace(/[\\/]+$/, "");
  const name = f.elements.name.value.trim();
  $("#new-target").textContent = name && parent ? "Creates " + parent + sep() + name : "";
}
function openNew() {
  const f = $("#form-new"), dlg = $("#dlg-new");
  f.reset();
  f.elements.parent.value = H.defaultParent;
  f.elements.author.value = store.get("home.author", "");
  f.elements.template.value = store.get("home.template", "amsart");
  const st = H.settings || {}, ready = !!(H.github && H.github.logged_in);
  f.elements.git.checked = st.git_init !== false;
  f.elements.github.checked = !!st.github_repo && ready;
  f.elements.github.disabled = !ready;
  f.elements.github_name.dataset.edited = "";
  f.elements.folder_id.innerHTML = folderOptions(currentFolder());
  dlgError(dlg, ""); updateShared(); updateTarget(); updateGithubRow();
  dlg.showModal(); f.elements.name.focus();
}
// A project created in a folder with one repository goes into that repository's folder,
// and gets no repository of its own.
function updateShared() {
  const f = $("#form-new"), sr = sharedRepo(f.elements.folder_id.value), note = f.querySelector(".shared-note");
  if (sr) {
    f.elements.parent.value = sr.dir;
    f.elements.git.checked = f.elements.github.checked = false;
  } else if (f.dataset.sharedDir && f.elements.parent.value === f.dataset.sharedDir) {
    f.elements.parent.value = H.defaultParent;
    const st = H.settings || {};
    f.elements.git.checked = st.git_init !== false;
  }
  f.dataset.sharedDir = sr ? sr.dir : "";
  f.elements.git.disabled = !!sr;
  f.elements.github.disabled = !!sr || !(H.github && H.github.logged_in);
  note.hidden = !sr;
  note.textContent = sr ? `Part of the repository of “${sr.owner.name}”: the project is created in its folder and synced with it.` : "";
}
$("#form-new").elements.folder_id.addEventListener("change", () => { updateShared(); updateTarget(); updateGithubRow(); });
$("#form-new").addEventListener("input", (e) => {
  if (e.target.name === "github_name") e.target.dataset.edited = "1";
  updateTarget(); updateGithubRow();
});
// GitHub allows letters, digits, ".", "-" and "_" in repository names.
const repoName = (folder) => folder.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "").slice(0, 100) || "latex-project";
function updateGithubRow() {
  const f = $("#form-new"), on = f.elements.github.checked, gh = H.github || {};
  f.querySelector(".gh-name").hidden = !on;
  // A GitHub repository needs a git repository.
  if (on) f.elements.git.checked = true;
  f.elements.git.disabled = on || !!sharedRepo(f.elements.folder_id.value);
  f.elements.github_name.required = on;
  if (on && !f.elements.github_name.dataset.edited) f.elements.github_name.value = repoName(f.elements.name.value.trim());
  $("#gh-owner").textContent = `github.com/${(H.settings && H.settings.github_owner) || gh.account || "…"}/ · private`;
  const note = f.querySelector(".gh-note");
  note.hidden = !!gh.logged_in;
  note.textContent = gh.logged_in ? "" : (gh.error || "GitHub is not connected.") + " See Settings (top right).";
}
$("#form-new").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, dlg = $("#dlg-new"), btn = f.querySelector("button[type=submit]");
  const body = {
    name: f.elements.name.value.trim(), parent: f.elements.parent.value.trim(),
    title: f.elements.title.value.trim(), author: f.elements.author.value.trim(),
    template: f.elements.template.value, git: f.elements.git.checked,
    github: f.elements.github.checked, github_name: f.elements.github_name.value.trim(),
    github_owner: (H.settings && H.settings.github_owner) || "",
    folder_id: f.elements.folder_id.value,
  };
  store.set("home.author", body.author); store.set("home.template", body.template);
  btn.disabled = true; btn.textContent = body.github ? "Creating on GitHub…" : "Creating…"; dlgError(dlg, "");
  const r = await api("/api/projects/create", body);
  btn.disabled = false; btn.textContent = "Create";
  if (r._status !== 200) return dlgError(dlg, r.error || "Could not create the project");
  dlg.close();
  if (r.github && r.github.error) toast(`Created ${r.path}, but the GitHub repository was not created:\n${r.github.error}`, true);
  else toast(`Created ${r.path}` + (r.github ? `\nGitHub: ${r.github.url}` : "") + (r.git_note ? `\n${r.git_note}` : ""));
  await load();
  if (H.view.kind === "tag") await post("/api/projects/update", { id: r.id, tags: [H.view.id] }, "tag the project");
  if (f.elements.open.checked) openProject(r.id);
});

function openAdd() {
  const f = $("#form-add"), dlg = $("#dlg-add");
  f.reset(); dlgError(dlg, "");
  f.elements.folder_id.innerHTML = folderOptions(currentFolder());
  dlg.showModal(); f.elements.path.focus();
}
$("#form-add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const dlg = $("#dlg-add");
  const r = await api("/api/projects/add", { path: e.target.elements.path.value, folder_id: e.target.elements.folder_id.value });
  if (r._status !== 200) return dlgError(dlg, r.error || "Could not add the folder");
  dlg.close();
  toast(r.has_tex ? `Added ${r.path}` : `Added ${r.path}\n(no .tex file at its top level yet)`);
  await load(); loadAllGit();
});

// Like registry.folder_name: the folder a name gives.
const folderFor = (name) => name.replace(/[<>:"\/\\|?*\x00-\x1f]/g, "-").replace(/^[ .]+|[ .]+$/g, "").slice(0, 120);
function renameHint() {
  const f = $("#form-rename"), p = byId(f.dataset.id), name = f.elements.name.value.trim();
  const folder = folderFor(name), hint = f.querySelector(".rename-hint");
  f.elements.folder.disabled = !!p.running;
  if (p.running) f.elements.folder.checked = false;
  hint.textContent = p.running
    ? "Its editor is open, so only the name in the list changes here. To rename the folder too, click the name at the top left of the editor."
    : !name ? `Empty: the list shows the folder's name, ${p.folder}.`
    : f.elements.folder.checked && folder && folder !== p.folder ? `The folder ${p.folder} becomes ${folder}.`
    : f.elements.folder.checked ? "The folder keeps its name."
    : `Only the name in the list changes; the folder stays ${p.folder}.`;
}
function openRename(id) {
  const p = byId(id), f = $("#form-rename"), dlg = $("#dlg-rename");
  f.dataset.id = id;
  f.elements.name.value = p.name;
  f.elements.name.placeholder = p.folder;
  f.elements.folder.checked = true;
  dlgError(dlg, ""); renameHint();
  dlg.showModal(); f.elements.name.select();
}
$("#form-rename").addEventListener("input", renameHint);
$("#form-rename").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, dlg = $("#dlg-rename");
  const r = await api("/api/projects/rename", { id: f.dataset.id, name: f.elements.name.value,
                                                folder: f.elements.folder.checked && !f.elements.folder.disabled });
  if (r._status !== 200) return dlgError(dlg, r.error || "Could not rename");
  dlg.close();
  if (r.moved) toast(`Folder renamed: ${r.path}`);
  H.sig = null; await load(); loadAllGit();
});

/* folders: new, or rename, move and notes of an existing one */
function openFolder(fid, parent = "") {
  const f = $("#form-folder"), dlg = $("#dlg-folder"), cur = fid && folderById(fid);
  f.reset(); dlgError(dlg, "");
  f.dataset.id = fid || "";
  dlg.querySelector("h3").textContent = cur ? "Edit folder" : parent ? `New folder in ${folderById(parent).name}` : "New folder";
  f.elements.parent.innerHTML = folderOptions(cur ? cur.parent : parent, { none: "— Top level —", exclude: fid });
  f.elements.name.value = cur ? cur.name : "";
  f.elements.note.value = cur ? cur.note || "" : "";
  dlg.showModal(); f.elements.name.focus(); f.elements.name.select();
}
$("#form-folder").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, dlg = $("#dlg-folder"), id = f.dataset.id;
  const body = { name: f.elements.name.value.trim(), parent: f.elements.parent.value, note: f.elements.note.value };
  const r = await api(id ? "/api/folders/update" : "/api/folders/create", id ? { id, ...body } : body);
  if (r._status !== 200) return dlgError(dlg, r.error || "Could not save the folder");
  dlg.close();
  if (r.parent) { H.collapsed.delete(r.parent); store.set("home.collapsed", [...H.collapsed]); }
  H.sig = null; await load();
  if (!id) setView({ kind: "folder", id: r.id });
});

function openMove(keys) {
  const items = keys.map(itemByKey).filter(Boolean), f = $("#form-move"), dlg = $("#dlg-move");
  if (!items.length) return;
  dlgError(dlg, ""); f.dataset.keys = JSON.stringify(items.map((it) => it.key));
  const one = items.length === 1 && (items[0].k === "f" ? items[0].f : items[0].p);
  dlg.querySelector("h3").textContent = `Move ${one ? one.name : plural(items.length, "item")} to folder`;
  const here = items.map((it) => (it.k === "f" ? it.f.parent : folderOf(it.p)) || "");
  f.elements.folder_id.innerHTML = folderOptions(here.every((x) => x === here[0]) ? here[0] : "",
    { exclude: items.filter((it) => it.k === "f").map((it) => it.id) });
  dlg.showModal(); f.elements.folder_id.focus();
}
$("#form-move").addEventListener("submit", (e) => {
  e.preventDefault();
  $("#dlg-move").close();
  moveItems(JSON.parse(e.target.dataset.keys || "[]"), e.target.elements.folder_id.value);
});

/* a folder's repositories: one shared, or one per project (hub.py: sync_plan, apply_sync) */
const SYNC_NOW = { own: "own repository", shared: "shared repository", none: "no repository", nested: "inside another repository", missing: "folder not found" };
function syncRow(r, plan) {
  const shared = plan.plan_mode === "shared";
  const rel = (t) => (t && t.startsWith(plan.repo) ? t.slice(plan.repo.length).replace(/^[\\/]/, "") : t);
  const then = {
    moves: `moves to <code>${esc(rel(r.target))}</code>` + (r.history ? ", with its history" : ""),
    stays: r.kind === "shared" ? "already in it" : "already in the folder" + (r.history ? ", with its history" : ""),
    keeps: "keeps its repository",
    splits: "gets its own repository, with its history",
    new: "new repository",
    blocked: `skipped: ${esc(r.why || "")}`,
    skip: "skipped",
  }[r.action];
  const changes = ["moves", "splits", "new"].includes(r.action) || (shared && r.action === "stays" && r.kind !== "shared");
  const open = r.running && changes ? `<span class="chip err" title="Close its editor first">editor open</span>` : "";
  const gh = r.github ? ` <a href="${esc(r.github)}" target="_blank" rel="noopener">GitHub ↗</a>` : "";
  return `<tr class="${r.action}"><td><b>${esc(r.name)}</b>${r.sub ? `<small>${esc(r.sub)}</small>` : ""}</td>
    <td>${SYNC_NOW[r.kind] || r.kind}${gh}</td><td>${then} ${open}</td></tr>`;
}
function renderSync() {
  const f = $("#form-sync"), plan = H.syncPlan, gh = (plan && plan.github) || H.github || {};
  if (!plan) return;
  const shared = plan.plan_mode === "shared";
  f.querySelectorAll(".sync-shared").forEach((el) => { el.hidden = !shared; });
  const missing = plan.projects.filter((r) => r.kind !== "missing" && (shared ? true : !r.github)).length;
  const label = $("#sync-gh-label");
  if (shared && plan.repo_github) {
    label.innerHTML = `Push to <a href="${esc(plan.repo_github)}" target="_blank" rel="noopener">${esc(plan.repo_github)}</a>`;
    f.elements.github.checked = true; f.elements.github.disabled = true;
  } else {
    label.textContent = shared ? "Create a private GitHub repository for it"
      : `Create private GitHub repositories for the projects that have none (${missing})`;
    f.elements.github.disabled = !gh.logged_in || (!shared && !missing);
    if (f.elements.github.disabled) f.elements.github.checked = false;
  }
  f.querySelector(".sync-gh-name").hidden = !shared || !f.elements.github.checked || !!plan.repo_github;
  $("#sync-owner").textContent = `github.com/${(H.settings && H.settings.github_owner) || gh.account || "…"}/ · private`;
  if (!f.elements.github_name.dataset.edited) f.elements.github_name.value = repoName(plan.repo.split(/[\\/]/).pop());
  const note = f.querySelector(".sync-gh-note");
  note.hidden = !!gh.logged_in;
  note.textContent = gh.logged_in ? "" : (gh.error || "GitHub is not connected.") + " Without it, the repositories stay on this computer.";
  const moves = plan.projects.filter((r) => r.action === "moves").length;
  const open = plan.projects.filter((r) => r.running && ["moves", "splits", "new"].includes(r.action)).length;
  const warn = [
    shared && moves ? `${plural(moves, "project folder")} will move into <code>${esc(plan.repo)}</code>. Their files are not changed; tags, folders and notes in this list come along.` : "",
    shared && plan.others.length ? `That folder also holds ${esc(plan.others.join(", "))}: it would be committed too.` : "",
    shared && plan.repo_inside ? `That folder is inside the repository ${esc(plan.repo_inside)}.` : "",
    !shared && plan.projects.some((r) => r.action === "splits") ? "The shared repository's .git is kept, renamed .git-prism-shared." : "",
    open ? `Close the editors marked “editor open” before applying.` : "",
  ].filter(Boolean);
  const current = plan.mode === plan.plan_mode ? " (current)" : "";
  $("#sync-plan").innerHTML = `<div class="sync-head">What happens${current}</div>
    <table><thead><tr><th>Project</th><th>Now</th><th>Then</th></tr></thead>
    <tbody>${plan.projects.map((r) => syncRow(r, plan)).join("") || `<tr><td colspan="3">No projects in this folder yet.</td></tr>`}</tbody></table>
    ${warn.map((w) => `<p class="hint warn">${w}</p>`).join("")}`;
  f.querySelector("button[type=submit]").disabled = !!open || !plan.projects.length;
}
let syncTimer = null;
async function loadSyncPlan() {
  const f = $("#form-sync"), dlg = $("#dlg-sync");
  const q = new URLSearchParams({ id: f.dataset.id, mode: f.elements.mode.value || "" });
  if (f.elements.repo.value.trim()) q.set("repo", f.elements.repo.value.trim());
  const r = await api("/api/folders/sync?" + q);
  if (r._status !== 200) { dlgError(dlg, r.error || "Could not look at the projects"); return; }
  dlgError(dlg, "");
  if (r.inherited) {
    H.syncPlan = null;
    const p = f.querySelector(".sync-inherited");
    p.hidden = false;
    p.innerHTML = `This folder is part of the repository of <b>${esc(r.inherited.name)}</b> (<code>${esc(r.inherited.repo)}</code>). Change it there.`;
    f.querySelectorAll(".sync-modes, .sync-shared, .check, .sync-plan").forEach((el) => { el.hidden = true; });
    f.querySelector("button[type=submit]").hidden = true;
    return;
  }
  H.syncPlan = r;
  if (!f.elements.mode.value) f.elements.mode.value = r.plan_mode;
  if (!f.elements.repo.value) f.elements.repo.value = r.repo;
  renderSync();
}
async function openSync(fid) {
  const f = $("#form-sync"), dlg = $("#dlg-sync"), folder = folderById(fid);
  f.reset(); dlgError(dlg, "");
  f.dataset.id = fid; f.elements.github_name.dataset.edited = "";
  dlg.querySelector("h3").textContent = `GitHub sync: ${folder.name}`;
  f.querySelector(".sync-inherited").hidden = true;
  f.querySelectorAll(".sync-modes, .check, .sync-plan").forEach((el) => { el.hidden = false; });
  f.querySelector("button[type=submit]").hidden = false;
  $("#sync-report").hidden = true;
  $("#sync-plan").innerHTML = `<p class="hint">Looking at the projects…</p>`;
  H.syncPlan = null;
  dlg.showModal();
  if (!H.github) await loadSettings();
  await loadSyncPlan();
}
$("#form-sync").addEventListener("input", (e) => {
  const f = $("#form-sync");
  if (e.target.name === "github_name") { e.target.dataset.edited = "1"; return; }
  if (e.target.name === "github") return renderSync();
  if (e.target.name === "mode" && e.target.value === "shared" && H.syncPlan && H.syncPlan.plan_mode !== "shared") f.elements.repo.value = "";
  clearTimeout(syncTimer);
  syncTimer = setTimeout(loadSyncPlan, e.target.name === "repo" ? 400 : 0);
});
$("#form-sync").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, dlg = $("#dlg-sync"), btn = f.querySelector("button[type=submit]"), plan = H.syncPlan;
  if (!plan) return;
  const shared = plan.plan_mode === "shared";
  const moves = plan.projects.filter((r) => r.action === "moves").length;
  if (shared && moves && !confirm(`Move ${plural(moves, "project folder")} into
${plan.repo}?

Nothing is deleted. Editors of these projects open at the new place afterwards.`)) return;
  btn.disabled = true; btn.textContent = f.elements.github.checked ? "Applying, with GitHub…" : "Applying…";
  dlgError(dlg, "");
  const r = await api("/api/folders/sync", {
    id: f.dataset.id, mode: f.elements.mode.value, repo: f.elements.repo.value.trim(),
    github: f.elements.github.checked && !f.elements.github.disabled, github_name: f.elements.github_name.value.trim(),
    github_owner: (H.settings && H.settings.github_owner) || "",
  });
  btn.textContent = "Apply";
  if (r._status !== 200) { btn.disabled = false; return dlgError(dlg, r.error || "Could not apply"); }
  const rep = $("#sync-report");
  rep.hidden = false;
  rep.className = "sync-report " + (r.ok ? "ok" : "err");
  rep.innerHTML = [r.ok ? "Done." : "Stopped:", ...r.report].map((x) => `<li>${esc(x)}</li>`).join("");
  H.sig = null; await load(); loadAllGit();
  await loadSyncPlan();
});

/* a GitHub repository for one project (hub.publish_project) */
async function openPublish(id) {
  const p = byId(id), f = $("#form-publish"), dlg = $("#dlg-publish"), btn = f.querySelector("button[type=submit]");
  f.reset(); dlgError(dlg, ""); f.dataset.id = id;
  dlg.querySelector("h3").textContent = `GitHub repository for ${p.name}`;
  $("#pub-private").innerHTML = ""; $("#pub-note").textContent = "Checking…"; btn.disabled = true;
  dlg.showModal();
  const r = await api("/api/projects/publish?id=" + encodeURIComponent(id));
  if (r._status !== 200) return dlgError(dlg, r.error || "Could not check the project");
  const gh = r.gh || {};
  f.elements.name.value = r.name;
  $("#pub-owner").textContent = `github.com/${(H.settings && H.settings.github_owner) || gh.account || "…"}/ · private`;
  $("#pub-private").innerHTML = (r.private_dirs || []).filter((d) => !d.ignored).map((d) =>
    `<label class="check"><input type="checkbox" name="leave_out" value="${esc(d.dir)}" checked> Keep <code>${esc(d.dir)}/</code> (${esc(d.what)}) off GitHub</label>`).join("");
  if (r.github) { $("#pub-note").innerHTML = `Already on GitHub: <a href="${esc(r.github)}" target="_blank" rel="noopener">${esc(r.github)}</a>`; return; }
  $("#pub-note").textContent = r.kind === "nested" ? `This folder is inside another repository (${r.top}); move the project out of it first.`
    : !gh.logged_in ? (gh.error || "The GitHub CLI is not logged in.") + " See Settings (top right)."
    : r.kind === "shared" ? `It is part of the repository in ${r.top}: that whole repository goes to GitHub.`
    : (r.kind === "none" ? "A git repository is created first. " : "") + "Build output and LaTeX's auxiliary files stay out (.gitignore). Its editor then saves and pushes changes automatically.";
  btn.disabled = r.kind === "nested" || !gh.logged_in;
}
$("#form-publish").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, dlg = $("#dlg-publish"), btn = f.querySelector("button[type=submit]");
  btn.disabled = true; btn.textContent = "Creating…"; dlgError(dlg, "");
  const r = await api("/api/projects/publish", { id: f.dataset.id, name: f.elements.name.value.trim(),
    owner: (H.settings && H.settings.github_owner) || "",
    leave_out: [...f.querySelectorAll('input[name="leave_out"]:checked')].map((i) => i.value) });
  btn.disabled = false; btn.textContent = "Create";
  if (r._status !== 200) return dlgError(dlg, r.error || "Could not create the repository");
  dlg.close();
  toast(r.created ? `Created ${r.url}` : `Already on GitHub: ${r.url}`);
  const p = byId(f.dataset.id); if (p) loadGit(p);
});

/* a project's tags */
function tagChoices(checked) {
  const names = [...new Set([...allTags().map((t) => t.name), ...checked])];
  $("#tag-choices").innerHTML = names.length
    ? names.map((t) => `<label class="tagpick" style="--tag:${tagColor(t)}"><input type="checkbox" value="${esc(t)}" ${checked.includes(t) ? "checked" : ""}><span>${esc(t)}</span></label>`).join("")
    : `<p class="hint">No tags yet: add the first one below.</p>`;
}
const checkedTags = () => [...document.querySelectorAll("#tag-choices input:checked")].map((i) => i.value);
function openTags(id) {
  const p = byId(id), f = $("#form-tags"), dlg = $("#dlg-tags");
  f.reset(); dlgError(dlg, ""); f.dataset.id = id;
  dlg.querySelector("h3").textContent = `Tags of ${p.name}`;
  tagChoices(p.tags || []);
  dlg.showModal(); f.elements.new_tag.focus();
}
function addTypedTag() {
  const input = $("#form-tags").elements.new_tag, t = input.value.replace(/\s+/g, " ").trim();
  if (!t) return false;
  const have = checkedTags(), same = allTags().find((x) => x.name.toLowerCase() === t.toLowerCase());
  tagChoices([...have, same ? same.name : t]);
  input.value = ""; input.focus();
  return true;
}
$("#tag-add").onclick = addTypedTag;
$("#form-tags").elements.new_tag.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && addTypedTag()) e.preventDefault();      // an empty field submits
});
$("#form-tags").addEventListener("submit", async (e) => {
  e.preventDefault();
  addTypedTag();
  const id = e.target.dataset.id;
  $("#dlg-tags").close();
  await post("/api/projects/update", { id, tags: checkedTags() }, "save the tags");
});

/* a tag: rename (on every project) and color */
function openTag(name) {
  const f = $("#form-tag"), dlg = $("#dlg-tag");
  dlgError(dlg, ""); f.dataset.name = name;
  f.elements.name.value = name;
  const cur = tagColor(name);
  $("#tag-swatches").innerHTML = TAG_PALETTE.map((c) => `<label class="swatch" style="--tag:${c}" title="${c}"><input type="radio" name="color" value="${c}" ${c === cur ? "checked" : ""}><span></span></label>`).join("");
  dlg.showModal(); f.elements.name.select();
}
$("#form-tag").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, dlg = $("#dlg-tag"), name = f.dataset.name;
  const body = { name, color: f.elements.color.value };
  const next = f.elements.name.value.replace(/\s+/g, " ").trim();
  if (next && next !== name) body.new_name = next;
  const r = await api("/api/tags/update", body);
  if (r._status !== 200) return dlgError(dlg, r.error || "Could not save the tag");
  dlg.close();
  if (body.new_name && H.view.kind === "tag" && H.view.id === name) {
    const merged = allTags().find((t) => t.name.toLowerCase() === next.toLowerCase());
    H.view = { kind: "tag", id: merged ? merged.name : next }; store.set("home.view", H.view);
  }
  H.sig = null; load();
});

$("#btn-new").onclick = openNew;

/* ------------------------------------------------------------------ settings */
async function loadSettings(refresh = false) {
  const r = await api("/api/settings" + (refresh ? "?refresh=1" : "")).catch(() => null);
  if (r && r._status === 200) { H.settings = r.settings; H.github = r.github; H.claude = r.claude; }
  return r;
}
function renderGhStatus() {
  const el = $("#gh-status"), gh = H.github || {};
  el.className = "gh-status " + (gh.logged_in ? "ok" : "err");
  el.textContent = gh.logged_in ? `✓ Connected as ${gh.account} (GitHub CLI)` : (gh.error || "Checking…");
  const f = $("#form-settings");
  f.elements.github_repo.disabled = !gh.logged_in;
  f.elements.github_owner.placeholder = gh.account || "";
  renderClaudeStatus();
}
// Which account Claude Code uses now, and whether it matches the one allowed.
function renderClaudeStatus() {
  const el = $("#claude-status"), c = H.claude || {}, f = $("#form-settings");
  const want = f.elements.claude_account.value.trim().toLowerCase();
  const who = c.email ? `${c.email}${c.org ? " · " + c.org : ""}${c.subscription ? " (" + c.subscription + ")" : ""}` : "";
  let ok = !!c.logged_in && !c.error && !(c.overrides || []).length;
  let text;
  if (c.error) text = "Could not check: " + c.error;
  else if (!c.logged_in) text = "Claude Code is not logged in. Run claude in a terminal and log in.";
  else if ((c.overrides || []).length) text = `Logged in as ${who}, but overridden by ${c.overrides.join("; ")}.`;
  else if (want && c.email && want !== c.email.toLowerCase()) { ok = false; text = `Logged in as ${who}, not the allowed account: the agent will refuse to run.`; }
  else text = `✓ Claude Code is logged in as ${who}`;
  el.className = "gh-status " + (ok ? "ok" : "err");
  el.textContent = text;
  $("#claude-use-current").disabled = !c.email;
}
$("#claude-use-current").onclick = () => {
  const f = $("#form-settings");
  if (H.claude && H.claude.email) { f.elements.claude_account.value = H.claude.email; renderClaudeStatus(); }
};
$("#form-settings").addEventListener("input", (e) => { if (e.target.name === "claude_account") renderClaudeStatus(); });
async function openSettings() {
  const f = $("#form-settings"), dlg = $("#dlg-settings");
  dlgError(dlg, "");
  const fill = () => {
    const st = H.settings || {};
    f.elements.default_parent.value = st.default_parent || "";
    f.elements.git_init.checked = st.git_init !== false;
    f.elements.github_repo.checked = !!st.github_repo;
    f.elements.github_owner.value = st.github_owner || "";
    f.elements.claude_account.value = st.claude_account || "";
    f.elements.claude_config_dir.value = st.claude_config_dir || "";
    renderGhStatus();
  };
  fill(); dlg.showModal();
  await loadSettings(true); fill();          // re-check gh: you may have just logged in
}
$("#btn-settings").onclick = openSettings;
$("#form-settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, dlg = $("#dlg-settings");
  const r = await api("/api/settings", {
    default_parent: f.elements.default_parent.value.trim(), git_init: f.elements.git_init.checked,
    github_repo: f.elements.github_repo.checked, github_owner: f.elements.github_owner.value.trim(),
    claude_account: f.elements.claude_account.value.trim(),
    claude_config_dir: f.elements.claude_config_dir.value.trim(),
  });
  if (r._status !== 200) return dlgError(dlg, r.error || "Could not save the settings");
  H.settings = r.settings; H.github = r.github; H.claude = r.claude; H.sig = null;
  dlg.close(); toast("Settings saved"); load();
});
$("#btn-add").onclick = openAdd;

/* ------------------------------------------------------------------ search, keys */
$("#search").addEventListener("input", () => { H.sel.clear(); render(); });
$("#search").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && shownKeys.length) { e.preventDefault(); select([shownKeys[0]]); $("#view").focus(); }
  if (e.key === "ArrowDown" && shownKeys.length) { e.preventDefault(); select([shownKeys[0]]); $("#view").focus(); }
  if (e.key === "Escape") { e.target.value = ""; render(); e.target.blur(); }
});
// How many items one row of the large icons holds.
function perRow() {
  const els = [...document.querySelectorAll("#view .it")];
  if (H.mode !== "icons" || !els.length) return 1;
  const top = els[0].offsetTop;
  return Math.max(1, els.findIndex((el) => el.offsetTop !== top) < 0 ? els.length : els.findIndex((el) => el.offsetTop !== top));
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeMenu();
  if (document.querySelector("dialog[open]") || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)
      || e.target.closest("#menu")) return;
  const mod = e.ctrlKey || e.metaKey;
  if (e.key === "/" || (mod && e.key.toLowerCase() === "f") || (mod && e.key.toLowerCase() === "e")) { e.preventDefault(); $("#search").focus(); return; }
  if (e.key === "n" && !mod && !e.altKey) { e.preventDefault(); openNew(); return; }
  if (mod && e.shiftKey && e.key.toLowerCase() === "n") { e.preventDefault(); openFolder(null, currentFolder()); return; }
  if (e.altKey && e.key === "ArrowLeft") { e.preventDefault(); goHistory(-1); return; }
  if (e.altKey && e.key === "ArrowRight") { e.preventDefault(); goHistory(1); return; }
  if ((e.altKey && e.key === "ArrowUp") || e.key === "Backspace") { e.preventDefault(); e.key === "Backspace" ? goHistory(-1) : goUp(); return; }
  // Moving through the items, like Explorer.
  if (e.target.closest("#side")) return;
  const n = shownKeys.length, i = shownKeys.indexOf(H.cursor);
  const step = { ArrowDown: perRow(), ArrowUp: -perRow(), ArrowRight: H.mode === "icons" ? 1 : 0, ArrowLeft: H.mode === "icons" ? -1 : 0,
                 Home: -Infinity, End: Infinity, PageDown: 10 * perRow(), PageUp: -10 * perRow() }[e.key];
  if (step !== undefined && n) {
    if (!step) return;
    e.preventDefault();
    const j = Math.max(0, Math.min(n - 1, i < 0 ? (step > 0 ? 0 : n - 1) : i + step)), k = shownKeys[j];
    if (e.shiftKey) select(rangeTo(k), { cursor: k, anchor: false }); else select([k]);
    return;
  }
  if (mod && e.key.toLowerCase() === "a" && n) { e.preventDefault(); select(shownKeys, { cursor: H.cursor || shownKeys[0] }); return; }
  if (/^(BUTTON|A)$/.test(document.activeElement.tagName) && (e.key === "Enter" || e.key === " ")) return;
  if (e.key === "Enter" && H.sel.size === 1) { e.preventDefault(); activate([...H.sel][0]); return; }
  if (e.key === "F2") { e.preventDefault(); renameSel(); return; }
  if (e.key === "Delete" && H.sel.size) { e.preventDefault(); removeSel(); return; }
  if (e.key === "Escape" && H.sel.size) select([]);
});

/* ------------------------------------------------------------------ GitHub check */
// The Home server fetches every repository when it starts: follow it, then refresh the
// cards' git state once it is done.
async function watchFetch() {
  for (;;) {
    const r = await api("/api/fetch").catch(() => null);
    if (!r || r._status !== 200) return;
    const was = H.fetch && H.fetch.running;
    H.fetch = r;
    if (!r.running) {
      if (was || !H.fetchSeen) { H.fetchSeen = true; await loadAllGit(); }
      render();
      return;
    }
    render();
    await new Promise((res) => setTimeout(res, 1000));
  }
}

/* ------------------------------------------------------------------ start */
load().then(loadAllGit).then(watchFetch);
loadSettings();
setInterval(() => { if (document.visibilityState === "visible" && !document.querySelector("dialog[open]")) load(); }, 10000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { load(); loadAllGit(); } });
