/* The editor's open files and how each is saved (app.js draws them). Plain: the page gives
   what it needs, so the Node checks drive it with a fake document, server and clock.

     const TABS = Tabs.createTabStore({saveFile, isHeld, sleep, onChange, notify});
                                             // saveFile: client.js's (POST /api/file)
     const t = TABS.add(path, doc, mtime);   // doc: a CodeMirror Doc (getValue, setValue,
                                             // changeGeneration, isClean)
     await TABS.save(t)                       // {ok} or {ok: false, reason, message}
     await TABS.saveAll()                     // the same, for every edited tab
     TABS.diskSeen(t, mtime)                  // the tree poll saw the file on disk
     TABS.stamp()                             // before a tree request: tabs whose disk state
                                             // the page learned later have t.seen above it

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
                            onChange = () => {}, notify = () => {}, busyWait = 1000,
                            schedule = (fn, ms) => setTimeout(fn, ms), cancel = clearTimeout,
                            isStopped = () => false, manualInput = null }) {
    const tabs = [];
    let heldNoted = false;
    let learned = 0;                   // counts what the page learned of the disk (t.seen)

    const dirty = (t) => !t.doc.isClean(t.gen);

    function add(path, doc, mtime, capability = null) {
      const t = { path, doc, mtime, seen: ++learned, gen: doc.changeGeneration(true), conflict: null, saving: null,
        inFlight: false, saveError: null, retryTimer: null, retries: 0 };
      tabs.push(t);
      manualInput?.track(doc, capability);
      return t;
    }
    const get = (path) => tabs.find((t) => t.path === path) || null;
    // Close the tabs `pred` picks (unsaved edits are lost: the page asked first).
    function remove(pred) {
      const gone = tabs.filter(pred);
      for (const t of gone) { cancel(t.retryTimer); manualInput?.remove(t.doc); tabs.splice(tabs.indexOf(t), 1); }
      return gone;
    }

    function save(t, force = false) {
      if (isStopped() || !tabs.includes(t)) return Promise.resolve({ ok: false, reason: "stopped" });
      if (t.conflict && !force) return Promise.resolve({ ok: false, reason: t.conflict === "gone" ? "gone" : "conflict" });
      if (isHeld()) {
        if (!heldNoted) { heldNoted = true; notify("Your edits are saved when the agent's turn ends."); }
        return Promise.resolve({ ok: false, reason: "held" });
      }
      const run = () => saveNow(t, force);
      t.saving = (t.saving || Promise.resolve()).then(run, run);
      return t.saving;
    }

    async function saveNow(t, force) {
      if (isStopped() || !tabs.includes(t)) return { ok: false, reason: "stopped" };
      if (isHeld()) return { ok: false, reason: "held" };
      if (!dirty(t) && !force) return { ok: true };
      let gen, r, evidence;
      for (let waited = false; ; waited = true) {
        gen = t.doc.changeGeneration();
        const content = t.doc.getValue();
        evidence = manualInput?.freeze(t.doc, force);
        // In flight: the file on disk is this save's until its answer brings the new mtime.
        t.inFlight = true;
        try { r = await saveFile({ path: t.path, content, base_mtime: t.mtime, force,
          ...(evidence?.token ? { manual_edit_token: evidence.token } : {}) }); }
        catch (e) { r = { _status: 0, error: e.message || String(e) }; }
        finally { t.inFlight = false; }
        if (!(r._status === 503 && r.busy)) break;
        if (!waited) notify(`${t.path}: ${r.error || "busy"}.`);
        await sleep(busyWait);
        if (isHeld()) return { ok: false, reason: "held" };   // an Edit turn began meanwhile
      }
      if (r._status === 409 && r.conflict) {
        t.conflict = r.gone ? "gone" : "disk";
        onChange(t, "conflict");
        const message = `${t.path} ${r.gone ? "was deleted" : "changed"} on disk — not saved. Resolve in the banner.`;
        notify(message);
        return { ok: false, reason: r.gone ? "gone" : "conflict", message };
      }
      if (r._status !== 200) {
        const message = `Save failed: ${r.error || r._status}`;
        t.saveError = message;
        onChange(t, "failed");
        cancel(t.retryTimer);
        if (!r.other_project && !isStopped()) t.retryTimer = schedule(() => {
          t.retryTimer = null;
          if (!dirty(t) || t.conflict || isStopped() || !tabs.includes(t)) return;
          return save(t);
        }, Math.min(30000, 1000 * 2 ** Math.min(t.retries++, 5)));
        t.retryTimer?.unref?.();       // Node checks do not stay alive just for browser retries.
        notify(message);
        return { ok: false, reason: "failed", message };
      }
      cancel(t.retryTimer); t.retryTimer = null; t.retries = 0; t.saveError = null;
      t.mtime = r.mtime; t.seen = ++learned; t.gen = gen; t.conflict = null;
      if (evidence) manualInput?.saved(t.doc, evidence, r.manual_edit);
      if (r.research_error) notify(`${t.path}: saved, but research notation could not be registered: ${r.research_error}`);
      else if (r.research_warning) notify(`${t.path}: saved. ${r.research_warning}`);
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
      const reason = ["gone", "stopped", ...REASONS].find((x) => failed.some((f) => f.reason === x));
      return { ok: false, reason, paths: failed.map((f) => f.path),
               message: (failed.find((f) => f.reason === reason && f.message) || {}).message };
    }

    // The tree poll saw the file on disk with `mtime`: "same" (nothing to do, or a save of
    // ours still in flight: the file on disk is its own), "reload" (no unsaved edits), or
    // "conflict" (unsaved edits: the banner).
    function diskSeen(t, mtime) {
      if (mtime == null) {
        cancel(t.retryTimer);
        if (t.conflict !== "gone") { t.conflict = "gone"; onChange(t, "gone"); }
        return "gone";
      }
      if (t.conflict === "gone" && Math.abs(mtime - t.mtime) < 1e-6) {
        t.conflict = null; onChange(t, "back");          // the same file is back (the Recycle Bin)
        return "same";
      }
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
    function synced(t, mtime, capability = null) {
      cancel(t.retryTimer); t.retryTimer = null; t.saveError = null; t.retries = 0;
      t.mtime = mtime; t.seen = ++learned; t.gen = t.doc.changeGeneration(true); t.conflict = null;
      manualInput?.synced(t.doc, capability);
      onChange(t, "synced");
    }

    // An agent's Edit turn starts: its "saved when the turn ends" note is shown once again.
    const turnStarted = () => { heldNoted = false; };

    const stamp = () => learned;

    return { tabs, add, get, remove, dirty, save, saveAll, diskSeen, loaded, synced, turnStarted, stamp };
  }

  // What to tell the author when saveAll did not save everything (before a build, a sync,
  // a message to the agent …).
  // A save that found the problem just now said so (notify) in these same words, so the
  // page's notice stack shows it once.
  function unsavedWhy(r) {
    if (r.reason === "gone") return r.message || "The file was deleted on disk. Save it under a new name or close its tab.";
    if (r.reason === "stopped") return "This page cannot save. Reopen the project from Home.";
    if (r.reason === "conflict") return r.message || "Resolve the save conflict first (the banner above the editor).";
    if (r.reason === "held") return "Your edits are saved when the agent's turn ends.";
    return r.message || "Saving failed";
  }

  return { createTabStore, unsavedWhy };
})();

if (typeof module !== "undefined") module.exports = Tabs;
