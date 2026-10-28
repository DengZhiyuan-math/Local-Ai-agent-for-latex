/* Reconcile a tree response with the page's last response and open tabs. No I/O. `asked`:
   Tabs.stamp() when the tree was asked for; a tab saved, opened or reloaded since (t.seen
   above it) is newer than the tree and left alone. */
"use strict";
const Watch = (() => {
  function reconcile(prev, tree, tabs, asked = Infinity) {
    const out = { foreign: !!(prev?.project_key && tree.project_key !== prev.project_key),
      treeChanged: false, reload: [], conflicts: [], gone: [], symbols: false, pdf: false };
    if (out.foreign) return out;
    const files = new Map((tree.files || []).map((f) => [f.path, f]));
    const shape = (r) => JSON.stringify([(r?.files || []).map((f) => [f.path, f.git, f.editable]), r?.folders || [], r?.order || []]);
    out.treeChanged = !prev || shape(prev) !== shape(tree);
    out.symbols = out.treeChanged || JSON.stringify((prev?.files || []).map((f) => [f.path, f.mtime]))
      !== JSON.stringify((tree.files || []).map((f) => [f.path, f.mtime]));
    out.pdf = !prev || prev.pdf_mtime !== tree.pdf_mtime || prev.pdf_main !== tree.pdf_main;
    for (const t of tabs) {
      const f = files.get(t.path);
      if (t.inFlight || t.seen > asked) continue;
      if (!f) { if (t.conflict !== "gone") out.gone.push(t.path); continue; }
      if (t.conflict === "gone" || Math.abs(f.mtime - t.mtime) >= 1e-6)
        out[t.dirty ? "conflicts" : "reload"].push(t.path);
    }
    return out;
  }
  return { reconcile };
})();
if (typeof module !== "undefined") module.exports = Watch;
