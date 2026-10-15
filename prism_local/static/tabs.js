/* The editor's open files and how each is saved (app.js draws them). Plain: the page gives
   what it needs, so the Node checks drive it with a fake document, server and clock.

     const TABS = Tabs.createTabStore({saveFile, isHeld, sleep, onChange, notify});
                                             // saveFile: client.js's (POST /api/file)
     const t = TABS.add(path, doc, mtime);   // doc: a CodeMirror Doc (getValue, setValue,
                                             // changeGeneration, isClean)
     await TABS.save(t)                       // {ok} or {ok: false, reason, message}
     await TABS.saveAll()                     // the same, for every edited tab
     TABS.diskSeen(t, mtime)                  // the tree poll saw the file on disk

   One save per tab at a time: a second waits for the first, so two writes never race on
   the server (the second would look like a change made on disk). While isHeld() (an
   agent's Edit turn), nothing is written: your edits would end up in its turn's diff and
   its Undo; they are saved when the turn ends, and a file the agent changed meanwhile
   gets the conflict banner instead. git busy (503 "busy") is waited out.

   A tab's conflict is "disk" when the file changed on disk while it has unsaved edits: the
   page shows the banner; autosave waits until it is resolved. */
"use strict";

const Tabs = (() => {
  // Why saving did not happen, most needing you first (saveAll reports that one).
  const REASONS = ["conflict", "failed", "held"];

  function createTabStore({ saveFile, isHeld = () => false, sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
                            onChange = () => {}, notify = () => {}, busyWait = 1000 }) {
    const tabs = [];
    let heldNoted = false;

    const dirty = (t) => !t.doc.isClean(t.gen);

    function add(path, doc, mtime) {
      const t = { path, doc, mtime, gen: doc.changeGeneration(true), conflict: null, saving: null, inFlight: false };
      tabs.push(t);
      return t;
    }
    const get = (path) => tabs.find((t) => t.path === path) || null;
    // Close the tabs `pred` picks (unsaved edits are lost: the page asked first).
    function remove(pred) {
      const gone = tabs.filter(pred);
      for (const t of gone) tabs.splice(tabs.indexOf(t), 1);
      return gone;
    }

    function save(t, force = false) {
      if (isHeld()) {
        if (!heldNoted) { heldNoted = true; notify("Your edits are saved when the agent's turn ends."); }
        return Promise.resolve({ ok: false, reason: "held" });
      }
      const run = () => saveNow(t, force);
      t.saving = (t.saving || Promise.resolve()).then(run, run);
      return t.saving;
    }

    async function saveNow(t, force) {
      if (!dirty(t) && !force) return { ok: true };
      let gen, r;
      for (let waited = false; ; waited = true) {
        gen = t.doc.changeGeneration();
        // In flight: the file on disk is this save's until its answer brings the new mtime.
        t.inFlight = true;
        try { r = await saveFile({ path: t.path, content: t.doc.getValue(), base_mtime: t.mtime, force }); }
        finally { t.inFlight = false; }
        if (!(r._status === 503 && r.busy)) break;
        if (!waited) notify(`${t.path}: ${r.error || "busy"}.`);
        await sleep(busyWait);
        if (isHeld()) return { ok: false, reason: "held" };   // an Edit turn began meanwhile
      }
      if (r._status === 409 && r.conflict) {
        t.conflict = "disk";
        onChange(t, "conflict");
        notify(`${t.path} changed on disk — not saved. Resolve in the banner.`);
        return { ok: false, reason: "conflict" };
      }
      if (r._status !== 200) {
        const message = `Save failed: ${r.error || r._status}`;
        notify(message);
        return { ok: false, reason: "failed", message };
      }
      t.mtime = r.mtime; t.gen = gen; t.conflict = null;
      onChange(t, "saved");
      return { ok: true };
    }

    // Every edited tab saved: {ok: true}, or {ok: false, reason (the one most needing you),
    // paths (those not saved), message}.
    async function saveAll() {
      const failed = [];
      for (const t of [...tabs]) {
        if (!dirty(t)) continue;
        const r = await save(t);
        if (!r.ok) failed.push({ path: t.path, ...r });
      }
      if (!failed.length) return { ok: true };
      const reason = REASONS.find((x) => failed.some((f) => f.reason === x));
      return { ok: false, reason, paths: failed.map((f) => f.path), message: (failed.find((f) => f.message) || {}).message };
    }

    // The tree poll saw the file on disk with `mtime`: "same" (nothing to do, or a save of
    // ours still in flight: the file on disk is its own), "reload" (no unsaved edits), or
    // "conflict" (unsaved edits: the banner).
    function diskSeen(t, mtime) {
      if (Math.abs(mtime - t.mtime) < 1e-6 || t.inFlight) return "same";
      if (!dirty(t)) return "reload";
      if (t.conflict !== "disk") { t.conflict = "disk"; onChange(t, "conflict"); }
      return "conflict";
    }

    // The disk's text arrived (a reload; `discard`: the author chose it over unsaved edits):
    // "kept" (typed into while it was fetched: the banner), "same" (no change to the text),
    // or "replace" (the page puts `content` in, then calls synced).
    function loaded(t, content, discard = false) {
      if (!discard && dirty(t)) {
        if (t.conflict !== "disk") { t.conflict = "disk"; onChange(t, "conflict"); }
        return "kept";
      }
      return content === t.doc.getValue() ? "same" : "replace";
    }
    // The tab holds the disk's text of `mtime` now.
    function synced(t, mtime) {
      t.mtime = mtime; t.gen = t.doc.changeGeneration(true); t.conflict = null;
      onChange(t, "synced");
    }

    // An agent's Edit turn starts: its "saved when the turn ends" note is shown once again.
    const turnStarted = () => { heldNoted = false; };

    return { tabs, add, get, remove, dirty, save, saveAll, diskSeen, loaded, synced, turnStarted };
  }

  // What to tell the author when saveAll did not save everything (before a build, a sync,
  // a message to the agent …).
  function unsavedWhy(r) {
    if (r.reason === "conflict") return "Resolve the save conflict first (the banner above the editor).";
    if (r.reason === "held") return "Your edits are saved when the agent's turn ends.";
    return (r.message || "Saving failed") + ". Try again.";
  }

  return { createTabStore, unsavedWhy };
})();

if (typeof module !== "undefined") module.exports = Tabs;
