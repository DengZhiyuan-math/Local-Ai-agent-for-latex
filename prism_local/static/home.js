/* prism-local Home — project list. Talks only to prism_local/hub.py.
   Loaded after common.js (helpers, theme, presence) and pdf.js. */
"use strict";

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdf.worker.js";
window.name = "prism-home";        // lets the editor's ⌂ button find and reuse this tab

const H = { projects: [], folders: [], tagColors: {}, defaultParent: "", git: {}, busy: new Set(), loaded: false,
            view: store.get("home.view", { kind: "all" }), collapsed: new Set(store.get("home.collapsed", [])) };
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
  if (r && r._status === 200) { H.git[p.id] = r.git; const el = document.querySelector(`.card-p[data-id="${p.id}"] .gitchip`); if (el) el.outerHTML = gitChip(p.id); }
}
function loadAllGit() { return Promise.all(H.projects.map(loadGit)); }

/* ------------------------------------------------------------------ folders and tags
   Folders (a research topic, its sub-projects, …) and tags only organize this list;
   hub.py keeps them in projects.json, and no file moves on disk. */
const folderById = (id) => H.folders.find((f) => f.id === id);
const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
const childFolders = (id) => H.folders.filter((f) => (f.parent || null) === (id || null)).sort(byName);
// Every folder below `id` (null: all of them), depth first, with its depth.
function folderTree(id = null, depth = 0, out = []) {
  for (const f of childFolders(id)) { out.push({ f, depth }); folderTree(f.id, depth + 1, out); }
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
  return [...set].map(([name, n]) => ({ name, n })).sort(byName);
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
  return `<option value="">${esc(none)}</option>` + folderTree()
    .filter(({ f }) => !exclude || !isInside(f.id, exclude))
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
function setView(view) {
  H.view = view; store.set("home.view", view);
  render(); window.scrollTo(0, 0);
}

/* ------------------------------------------------------------------ render */
function sorted(list) {
  const mode = $("#sort").value;
  const key = {
    opened: (p) => -(p.opened || p.added || 0),
    edited: (p) => -(p.edited || 0),
    name: (p) => p.name.toLowerCase(),
  }[mode];
  return [...list].sort((a, b) => { const x = key(a), y = key(b); return x < y ? -1 : x > y ? 1 : 0; });
}
function matches(p, q) {
  if (!q) return true;
  const hay = [p.name, p.folder, p.title || "", p.path, ...(p.tags || []),
               ...folderPath(folderOf(p)).map((f) => f.name)].join("\n").toLowerCase();
  return q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}
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
// The project's folder, when the view does not already say it, and its tags.
function orgHTML(p) {
  const fid = folderOf(p);
  // All projects and a folder view already say where a project is, in their headings.
  const showFolder = fid && H.view.kind !== "all" && !(H.view.kind === "folder" && fid === H.view.id);
  const where = showFolder
    ? `<button class="where" data-act="folder" data-fid="${fid}" title="Show this folder">${icon("folder")}<span>${esc(folderPath(fid).map((f) => f.name).join(" › "))}</span></button>` : "";
  const tags = (p.tags || []).map(tagChip).join("");
  return where || tags ? `<div class="org">${where}${tags}</div>` : "";
}
function cardHTML(p) {
  const busy = H.busy.has(p.id);
  if (!p.exists) {
    return `<div class="card-p missing" data-id="${p.id}" draggable="true">
      <div class="thumb"><div class="ph"><div class="ini">!</div><small>Folder not found</small></div></div>
      <div class="body"><div class="name" title="${esc(p.name)}">${esc(p.name)}</div>
        <div class="path" title="${esc(p.path)}"><bdi>${esc(p.path)}</bdi></div>
        <div class="meta"><span class="chip err">moved or deleted</span></div>${orgHTML(p)}</div>
      <div class="foot"><button class="open" data-act="remove">Remove from list</button>
        <button class="more icon" data-act="menu" title="More" aria-label="More">${icon("more")}</button></div></div>`;
  }
  const title = p.title && p.title.toLowerCase() !== p.name.toLowerCase() ? `<div class="title" title="${esc(p.title)}">${esc(p.title)}</div>` : "";
  const meta = [
    p.edited ? `<span title="Last change to a source file">Edited ${ago(p.edited)}</span>` : "",
    p.files ? `<span>${p.files} file${p.files === 1 ? "" : "s"}</span>` : `<span>no .tex files</span>`,
  ].join("");
  const run = p.running ? `<span class="badge-run" title="${p.running.pages} page(s) open at ${esc(p.running.url)}">Open</span>` : "";
  return `<div class="card-p" data-id="${p.id}" draggable="true">
    <div class="thumb" data-act="open" title="Open ${esc(p.name)}">
      <div class="ph"><div class="ini">${initials(p.name)}</div><small>${p.pdf_mtime ? "" : "No PDF yet"}</small></div>
      ${run}
      <button class="pin ${p.pinned ? "on" : ""}" data-act="pin" title="${p.pinned ? "Unpin" : "Pin to top"}">${icon("star")}</button>
    </div>
    <div class="body">
      <div class="name" data-act="open" title="${esc(p.name)}">${esc(p.name)}</div>
      ${title}
      <div class="path" title="${esc(p.path)}"><bdi>${esc(p.path)}</bdi></div>
      <div class="meta">${meta}${gitChip(p.id)}</div>
      ${orgHTML(p)}
    </div>
    <div class="foot">
      <button class="open ${p.running ? "" : "primary"}" data-act="open" ${busy ? "disabled" : ""}>${busy ? "Starting…" : p.running ? "Show editor" : "Open"}</button>
      <button class="more icon" data-act="menu" title="More actions" aria-label="More actions">${icon("more")}</button>
    </div>
  </div>`;
}
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

function sideRow({ act, attrs = "", ico, label, n, active, cls = "", depth = 0, twisty = "", more = "", drop = null, drag = "" }) {
  return `<div class="side-row ${cls} ${active ? "active" : ""}" role="button" tabindex="0" data-act="${act}" ${attrs}
      style="--depth:${depth}" ${drop !== null ? `data-drop="${drop}"` : ""} ${drag}>
    ${twisty}${ico}<span class="label">${label}</span>
    <span class="n">${n || ""}</span>${more}</div>`;
}
function renderSide() {
  const v = H.view, count = (f) => H.projects.filter(f).length;
  const views = [
    sideRow({ act: "view", attrs: `data-view="all"`, ico: icon("list"), label: "All projects", n: H.projects.length, active: v.kind === "all" }),
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
      <button class="tiny icon ghost" data-act="new-folder" title="New folder" aria-label="New folder">${icon("plus")}</button></div>
    <div class="side-list">${folders || `<p class="side-empty">Group projects by topic: a folder can hold projects and subfolders.</p>`}</div>
    <div class="side-head"><span>Tags</span></div>
    <div class="side-list">${tags || `<p class="side-empty">Add tags from a project's ⋯ menu.</p>`}</div>`;
}

// What the current view shows: [{title, fid?, list}] sections, already searched and sorted.
function viewSections(q) {
  const v = H.view, pick = (f) => sorted(H.projects.filter((p) => f(p) && matches(p, q)));
  if (v.kind === "pinned") return [{ title: "Pinned", list: pick((p) => p.pinned) }];
  if (v.kind === "unfiled") return [{ title: "Not in a folder", list: pick((p) => !folderOf(p)) }];
  if (v.kind === "tag") return [{ title: `Tagged “${v.id}”`, list: pick((p) => (p.tags || []).includes(v.id)) }];
  if (v.kind === "folder") {
    const f = folderById(v.id);
    const out = [{ title: `In ${f.name}`, fid: f.id, list: pick((p) => folderOf(p) === f.id), own: true }];
    for (const { f: sub } of folderTree(f.id)) {
      out.push({ title: folderPath(sub.id).slice(folderPath(f.id).length).map((x) => x.name).join(" › "),
                 fid: sub.id, list: pick((p) => folderOf(p) === sub.id) });
    }
    return out;
  }
  // All projects, grouped like the sidebar: pinned first, then each top-level folder (its
  // subfolders as subsections), then the projects in no folder. A group can be folded.
  const all = pick(() => true), rest = all.filter((p) => !p.pinned);
  const out = [{ title: "Pinned", list: all.filter((p) => p.pinned), group: "pinned", total: all.filter((p) => p.pinned).length }];
  for (const top of childFolders(null)) {
    out.push({ title: top.name, fid: top.id, list: rest.filter((p) => folderOf(p) === top.id), group: top.id,
               total: rest.filter((p) => inFolder(p, top.id)).length });
    for (const { f: sub } of folderTree(top.id)) {
      out.push({ title: folderPath(sub.id).slice(1).map((x) => x.name).join(" › "), fid: sub.id, sub: true,
                 list: rest.filter((p) => folderOf(p) === sub.id), group: top.id });
    }
  }
  out.push({ title: "Not in a folder", list: rest.filter((p) => !folderOf(p)), group: "unfiled", drop: "",
             total: rest.filter((p) => !folderOf(p)).length });
  return out;
}
const closedGroups = new Set(store.get("home.closedGroups", []));
function toggleGroup(g) {
  if (closedGroups.has(g)) closedGroups.delete(g); else closedGroups.add(g);
  store.set("home.closedGroups", [...closedGroups]); render();
}
function sectionHTML(s, noHead, q = "") {
  const link = s.fid ? `<button class="sec-link" data-act="folder" data-fid="${s.fid}" title="Open this folder">${icon("folder")}${esc(s.title)}</button>`
    : s.group === "unfiled" ? `<button class="sec-link" data-act="view" data-view="unfiled">${icon("inbox")}${esc(s.title)}</button>`
    : esc(s.title);
  let head;
  if (noHead) head = "";
  else if (s.group && !s.sub) {          // a group of the All projects view: it folds
    const open = !closedGroups.has(s.group) || !!q;
    head = `<h2 class="group-head"><button class="twisty ${open ? "open" : ""}" data-act="group" data-group="${esc(s.group)}"
              aria-label="${open ? "Fold" : "Unfold"}" aria-expanded="${open}">${icon("right")}</button>${link}<span class="n">${s.total || ""}</span></h2>`;
  } else if (s.sub) head = `<h3 class="sub-head">${link}<span class="n">${s.list.length || ""}</span></h3>`;
  else if (s.fid && !s.own) head = `<h2>${link}<span class="n">${s.list.length || ""}</span></h2>`;
  else head = `<h2>${esc(s.title)}</h2>`;
  const empty = s.fid && !s.list.length && !s.group ? `<div class="drop-hint">No projects yet: drag some here</div>` : "";
  const grid = s.list.length || !s.group ? `<div class="grid">${s.list.map(cardHTML).join("")}</div>` : "";
  const drop = s.fid ? s.fid : s.drop !== undefined ? s.drop : null;
  return `<section class="${s.group && !s.sub ? "group" : ""} ${s.sub ? "sub" : ""}" ${drop !== null ? `data-drop="${drop}"` : ""}>${head}${grid}${empty}</section>`;
}
function renderHead() {
  const v = H.view, crumbs = $("#crumbs"), note = $("#folder-note");
  crumbs.hidden = v.kind !== "folder"; note.hidden = true;
  if (v.kind !== "folder") return;
  const path = folderPath(v.id), f = path[path.length - 1];
  crumbs.innerHTML = `<button class="crumb" data-act="view" data-view="all">All projects</button>` +
    path.map((x, i) => `<span class="sep">›</span>` + (i === path.length - 1
      ? `<h1 class="crumb here">${esc(x.name)}</h1>`
      : `<button class="crumb" data-act="folder" data-fid="${x.id}">${esc(x.name)}</button>`)).join("") +
    syncChip(f.id) +
    `<span class="crumb-actions">
       <button class="tiny" data-act="new-folder" data-parent="${f.id}" title="New folder inside ${esc(f.name)}">${icon("plus")}Subfolder</button>
       <button class="tiny icon" data-act="folder-menu" data-fid="${f.id}" title="Folder actions" aria-label="Folder actions">${icon("more")}</button>
     </span>`;
  note.textContent = f.note || ""; note.hidden = !f.note;
}
function render() {
  if ((H.view.kind === "folder" && !folderById(H.view.id)) ||
      (H.view.kind === "tag" && !allTags().some((t) => t.name === H.view.id)) ||
      !["all", "pinned", "unfiled", "folder", "tag"].includes(H.view.kind)) H.view = { kind: "all" };
  renderSide(); renderHead();
  const q = $("#search").value.trim(), v = H.view;
  const secs = viewSections(q);
  // Empty sections go, except a folder's subfolders: they stay as drop targets. In All
  // projects a group shows while it has projects anywhere; folded, only its heading.
  const visible = v.kind === "all"
    ? secs.filter((s) => (s.sub ? s.list.length && (!closedGroups.has(s.group) || q) : s.total))
    : secs.filter((s) => s.list.length || (s.fid && !s.own && !q));
  const shown = new Set(secs.flatMap((s) => s.list.map((p) => p.id))).size;
  $("#sections").innerHTML = visible.map((s) => {
    const folded = v.kind === "all" && !s.sub && closedGroups.has(s.group) && !q;
    return sectionHTML(folded ? { ...s, list: [] } : s, visible.length === 1 && (s.own || v.kind === "pinned" || v.kind === "unfiled" || v.kind === "tag"), q);
  }).join("");
  // Dropping anywhere else in a folder's view moves the project into that folder.
  if (v.kind === "folder") $("#home-main").dataset.drop = v.id; else delete $("#home-main").dataset.drop;
  $("#welcome").hidden = !H.loaded || H.projects.length > 0;
  $("#no-match").hidden = !H.projects.length || !q || shown > 0;
  const empty = $("#empty-view");
  empty.hidden = !H.projects.length || !!q || visible.length > 0;
  empty.innerHTML = {
    folder: "This folder has no projects yet. Drag projects here from the list on the left or from <b>All projects</b>, or create one with <b>+ New project</b>.",
    pinned: "No pinned projects. Pin one with the ☆ on its card.",
    unfiled: "Every project is in a folder.",
  }[v.kind] || "";
  const running = H.projects.filter((p) => p.running).length;
  const behind = H.projects.filter((p) => H.git[p.id] && H.git[p.id].behind).length;
  const fetchNote = H.fetch && H.fetch.running ? ` · checking GitHub (${H.fetch.done}/${H.fetch.total})…`
    : behind ? ` · ${plural(behind, "project")} with new changes on GitHub` : "";
  $("#summary").textContent = !H.projects.length ? ""
    : v.kind === "all" ? plural(H.projects.length, "project") + (running ? ` · ${running} open` : "") + fetchNote
    : v.kind === "folder" ? plural(shown, "project") + (childFolders(v.id).length ? ` · ${plural(folderTree(v.id).length, "subfolder")}` : "")
    : plural(shown, "project");
  document.querySelectorAll(".card-p[data-id]").forEach(attachThumb);
}

/* ------------------------------------------------------------------ PDF thumbnails */
const io = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { io.unobserve(e.target); drawThumb(e.target); }
}, { rootMargin: "200px" });
function attachThumb(card) {
  const p = byId(card.dataset.id);
  if (!p || !p.pdf_mtime) return;
  const c = thumbs.get(`${p.id}:${p.pdf_mtime}`);
  if (c) { placeThumb(card, c); return; }
  io.observe(card);
}
function placeThumb(card, canvas) {
  const box = card.querySelector(".thumb");
  const ph = box && box.querySelector(".ph");
  if (!box) return;
  if (ph) ph.remove();
  if (canvas.parentNode !== box) box.prepend(canvas);
}
async function drawThumb(card) {
  const p = byId(card.dataset.id);
  const key = p && `${p.id}:${p.pdf_mtime}`;
  if (!p || thumbs.has(key)) return;
  thumbs.set(key, null);
  try {
    const data = await fetch(`/api/pdf?id=${encodeURIComponent(p.id)}&t=${p.pdf_mtime}`).then((r) => r.ok ? r.arrayBuffer() : Promise.reject());
    const doc = await pdfjsLib.getDocument({ data, disableFontFace: true }).promise;   // see pdfview.js
    const page = await doc.getPage(1);
    // clientWidth is 0 while the tab is not laid out; fall back to the grid's minimum.
    const cssW = Math.max(120, Math.min((card.querySelector(".thumb").clientWidth || 236) - 36, 210));
    const base = page.getViewport({ scale: 1 });
    const dpr = window.devicePixelRatio || 1;
    const vp = page.getViewport({ scale: (cssW / base.width) * dpr });
    const canvas = document.createElement("canvas");
    canvas.width = vp.width; canvas.height = vp.height;
    canvas.style.width = cssW + "px"; canvas.style.height = vp.height / dpr + "px";
    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
    doc.destroy();
    for (const k of thumbs.keys()) if (k.startsWith(p.id + ":") && k !== key) thumbs.delete(k);
    thumbs.set(key, canvas);
    const live = document.querySelector(`.card-p[data-id="${p.id}"]`);
    if (live) placeThumb(live, canvas);
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
async function moveProject(id, fid) {
  const p = byId(id);
  if (!p || (folderOf(p) || "") === (fid || "")) return;
  p.folder_id = fid || null; render();
  const r = await post("/api/projects/update", { id, folder_id: fid || "" }, "move the project");
  if (r._status === 200) toast(fid ? `Moved ${p.name} to ${folderById(fid) ? folderById(fid).name : "the folder"}` : `${p.name} is no longer in a folder`);
}
async function moveFolder(fid, parent) {
  const f = folderById(fid);
  if (!f || (f.parent || "") === (parent || "") || fid === parent) return;
  await post("/api/folders/update", { id: fid, parent: parent || "" }, "move the folder");
}
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

/* clicks */
document.addEventListener("click", (e) => {
  const act = e.target.closest("[data-act]");
  if (!e.target.closest("#menu")) closeMenu();
  if (!act) return;
  const card = act.closest(".card-p");
  const id = card && card.dataset.id, d = act.dataset;
  switch (d.act) {
    case "open": return openProject(id);
    case "pin": e.stopPropagation(); return setPinned(id, !byId(id).pinned);
    case "remove": return removeProject(id);
    case "menu": e.stopPropagation(); return showMenu(act, projectMenu(id));
    case "new": return openNew();
    case "add": return openAdd();
    case "view": return setView({ kind: d.view });
    case "folder": return setView({ kind: "folder", id: d.fid });
    case "tag": e.stopPropagation(); return setView({ kind: "tag", id: d.tag });
    case "twisty": e.stopPropagation(); return toggleFolder(d.fid);
    case "new-folder": e.stopPropagation(); return openFolder(null, d.parent || currentFolder());
    case "folder-menu": e.stopPropagation(); return showMenu(act, folderMenu(d.fid));
    case "tag-menu": e.stopPropagation(); return showMenu(act, tagMenu(d.tag));
    case "sync": e.stopPropagation(); return openSync(d.fid);
    case "publish": e.stopPropagation(); return openPublish(id);
    case "group": e.stopPropagation(); return toggleGroup(d.group);
  }
});
// The sidebar's rows are not buttons (they hold buttons): Enter and Space work on them too.
$("#side").addEventListener("keydown", (e) => {
  const row = e.target.closest(".side-row");
  if (row && e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); row.click(); }
});
document.addEventListener("contextmenu", (e) => {
  const card = e.target.closest(".card-p[data-id]"), folder = e.target.closest(".side-folder[data-fid]"),
        tag = e.target.closest(".side-tag[data-tag]");
  const menu = card ? projectMenu(card.dataset.id) : folder ? folderMenu(folder.dataset.fid) : tag ? tagMenu(tag.dataset.tag) : null;
  if (!menu || e.target.closest("a, input")) return;
  e.preventDefault(); showMenu(null, menu, { x: e.clientX, y: e.clientY });
});

/* ------------------------------------------------------------------ menus
   A menu is [[label, fn, cls?], "-", …]. */
let menuItems = [];
function projectMenu(id) {
  const p = byId(id);
  const org = [["Move to folder…", () => openMove(id)], ["Tags…", () => openTags(id)]];
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
   A project card onto a folder (in the sidebar, a subfolder's section, or anywhere in a
   folder's view) moves the project there; onto "Not in a folder" takes it out of its
   folder. A folder onto another folder nests it; onto the "Folders" heading, to the top. */
const DRAG_P = "application/x-prism-project", DRAG_F = "application/x-prism-folder";
let dropOn = null;
function markDrop(el) {
  if (dropOn === el) return;
  if (dropOn) dropOn.classList.remove("drop-on");
  dropOn = el; if (el) el.classList.add("drop-on");
}
// The element a drag over `target` would drop on, if it accepts what is dragged.
function dropTarget(e) {
  const types = e.dataTransfer.types, el = e.target.closest && e.target.closest("[data-drop]");
  if (!el) return null;
  if (types.includes(DRAG_P)) return el;
  if (types.includes(DRAG_F) && (el.classList.contains("side-folder") || el.classList.contains("side-head"))) return el;
  return null;
}
document.addEventListener("dragstart", (e) => {
  const card = e.target.closest && e.target.closest(".card-p[data-id]");
  const folder = e.target.closest && e.target.closest(".side-folder[data-fid]");
  if (card) e.dataTransfer.setData(DRAG_P, card.dataset.id);
  else if (folder) e.dataTransfer.setData(DRAG_F, folder.dataset.fid);
  else return;
  e.dataTransfer.effectAllowed = "move"; H.dragging = true; closeMenu();
});
document.addEventListener("dragend", () => { H.dragging = false; markDrop(null); });
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
  const pid = e.dataTransfer.getData(DRAG_P), fid = e.dataTransfer.getData(DRAG_F);
  if (pid) moveProject(pid, el.dataset.drop);
  else if (fid) {
    if (el.dataset.drop && isInside(el.dataset.drop, fid)) return toast("A folder cannot go inside itself", true);
    moveFolder(fid, el.dataset.drop);
  }
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

function openMove(id) {
  const p = byId(id), f = $("#form-move"), dlg = $("#dlg-move");
  dlgError(dlg, ""); f.dataset.id = id;
  dlg.querySelector("h3").textContent = `Move ${p.name} to folder`;
  f.elements.folder_id.innerHTML = folderOptions(folderOf(p));
  dlg.showModal(); f.elements.folder_id.focus();
}
$("#form-move").addEventListener("submit", (e) => {
  e.preventDefault();
  $("#dlg-move").close();
  moveProject(e.target.dataset.id, e.target.elements.folder_id.value);
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

/* ------------------------------------------------------------------ search, sort, keys */
$("#sort").value = store.get("home.sort", "opened");
$("#sort").onchange = () => { store.set("home.sort", $("#sort").value); render(); };
$("#search").addEventListener("input", render);
$("#search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { const first = document.querySelector(".card-p:not(.missing)"); if (first) openProject(first.dataset.id); }
  if (e.key === "Escape") { e.target.value = ""; render(); e.target.blur(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeMenu();
  if (document.querySelector("dialog[open]") || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
  if (e.key === "/") { e.preventDefault(); $("#search").focus(); }
  if (e.key === "n" && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); openNew(); }
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
