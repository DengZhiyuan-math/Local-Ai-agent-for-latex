/* What the Home page lists (home.js): folders and their trees, a search, a view's items
   and their order, the tags. Each takes the page's state H ({projects, folders, view,
   folderSort, sortKey, sortDir, tagColors, tagSort}) and changes nothing: home.js draws
   what these decide; checked under Node.

   Folders (a research topic, its sub-projects, …) and tags only organize this list;
   hub.py keeps them in projects.json, and no file moves on disk. */
"use strict";

const HomeView = (() => {
  const folderById = (H, id) => H.folders.find((f) => f.id === id);
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
  // A project's folder; one whose folder was deleted elsewhere counts as not in a folder.
  const folderOf = (H, p) => (p.folder_id && folderById(H, p.folder_id) ? p.folder_id : null);
  function folderPath(H, id) {
    const out = [];
    for (let f = folderById(H, id); f && out.length < 50; f = f.parent && folderById(H, f.parent)) out.unshift(f);
    return out;
  }
  const isInside = (H, fid, ancestor) => folderPath(H, fid).some((f) => f.id === ancestor);
  const inFolder = (H, p, fid) => { const f = folderOf(H, p); return !!f && isInside(H, f, fid); };
  // When a project in folder f (or its subfolders) was last edited or opened; 0 if none.
  function folderTime(H, f, mode) {
    const t = (p) => (mode === "edited" ? p.edited || 0 : p.opened || p.added || 0);
    return H.projects.reduce((m, p) => (inFolder(H, p, f.id) ? Math.max(m, t(p)) : m), 0);
  }
  // Folders by name, or most recently edited/opened first (empty ones last, by name).
  function sortFolders(H, list, mode = H.folderSort) {
    if (mode !== "edited" && mode !== "opened") return list.sort(byName);
    const t = new Map(list.map((f) => [f.id, folderTime(H, f, mode)]));
    return list.sort((a, b) => t.get(b.id) - t.get(a.id) || byName(a, b));
  }
  const childFolders = (H, id, mode) => sortFolders(H, H.folders.filter((f) => (f.parent || null) === (id || null)), mode);
  // Every folder below `id` (null: all of them), depth first, with its depth.
  function folderTree(H, id = null, depth = 0, out = [], mode = undefined) {
    for (const f of childFolders(H, id, mode)) { out.push({ f, depth }); folderTree(H, f.id, depth + 1, out, mode); }
    return out;
  }
  function allTags(H) {
    const set = new Map();
    for (const t of Object.keys(H.tagColors)) set.set(t, 0);
    for (const p of H.projects) for (const t of p.tags || []) set.set(t, (set.get(t) || 0) + 1);
    const list = [...set].map(([name, n]) => ({ name, n }));
    return H.tagSort === "count" ? list.sort((a, b) => b.n - a.n || byName(a, b)) : list.sort(byName);
  }

  /* items: what the view lists */
  const fItem = (f) => ({ k: "f", id: f.id, f, key: "f:" + f.id });
  const pItem = (p) => ({ k: "p", id: p.id, p, key: "p:" + p.id });
  function matches(H, p, q) {
    if (!q) return true;
    const hay = [p.name, p.folder, p.title || "", p.path, ...(p.tags || []),
                 ...folderPath(H, folderOf(H, p)).map((f) => f.name)].join("\n").toLowerCase();
    return q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
  }
  const folderMatches = (f, q) => q.toLowerCase().split(/\s+/).every((w) => (f.name + "\n" + (f.note || "")).toLowerCase().includes(w));
  // {flat, items}: flat lists say where each item is (a Location column).
  function viewItems(H, q) {
    const v = H.view, list = (pred) => H.projects.filter((p) => pred(p) && matches(H, p, q)).map(pItem);
    if (v.kind === "pinned") return { flat: true, items: list((p) => p.pinned) };
    if (v.kind === "unfiled") return { flat: true, items: list((p) => !folderOf(H, p)) };
    if (v.kind === "tag") return { flat: true, items: list((p) => (p.tags || []).includes(v.id)) };
    const fid = v.kind === "folder" ? v.id : null;
    if (q) return { flat: true, items: [
      ...folderTree(H, fid).map(({ f }) => f).filter((f) => folderMatches(f, q)).map(fItem),
      ...list((p) => !fid || inFolder(H, p, fid)),
    ] };
    return { flat: false, items: [...childFolders(H, fid, "name").map(fItem), ...H.projects.filter((p) => folderOf(H, p) === fid).map(pItem)] };
  }

  /* Sorting: folders first (as in Explorer), then by the chosen column. */
  const SORTS = {
    name: { label: "Name", dir: 1, f: (H, f) => f.name, p: (H, p) => p.name },
    edited: { label: "Date modified", dir: -1, f: (H, f) => folderTime(H, f, "edited"), p: (H, p) => p.edited || 0 },
    opened: { label: "Last opened", dir: -1, f: (H, f) => folderTime(H, f, "opened"), p: (H, p) => p.opened || p.added || 0 },
    where: { label: "Location", dir: 1, f: (H, f) => folderPath(H, f.parent).map((x) => x.name).join("/"),
             p: (H, p) => folderPath(H, folderOf(H, p)).map((x) => x.name).join("/") },
  };
  const cmpVal = (x, y) => (typeof x === "string" ? x.localeCompare(y, undefined, { numeric: true, sensitivity: "base" }) : x - y);
  function sortItems(H, items, flat) {
    const s = SORTS[!flat && H.sortKey === "where" ? "name" : H.sortKey];
    const val = (it) => (it.k === "f" ? s.f(H, it.f) : s.p(H, it.p)), name = (it) => (it.k === "f" ? it.f.name : it.p.name);
    const v = new Map(items.map((it) => [it.key, val(it)]));
    return items.sort((a, b) => (a.k === b.k ? 0 : a.k === "f" ? -1 : 1)
      || H.sortDir * cmpVal(v.get(a.key), v.get(b.key)) || cmpVal(name(a), name(b)));
  }

  return { folderById, byName, folderOf, folderPath, isInside, inFolder, folderTime, sortFolders, childFolders,
           folderTree, allTags, fItem, pItem, matches, viewItems, SORTS, sortItems };
})();

if (typeof module !== "undefined") module.exports = HomeView;
