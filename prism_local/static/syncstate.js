/* What the editor's sync button says (app.js renderSync), from /api/git/sync's status:
   the label, its colour class and the menu's explanation. Plain: checked under Node. */
"use strict";

const SyncState = (() => {
  const targetText = (t) => t ? `${t.repository_identity}, branch ${t.branch_ref.replace(/^refs\/heads\//, "")}` : "unavailable";

  // {text, cls, detail, tech}: the label, its colour, what it means in plain words, and
  // for those who want to know, in git's (shown folded, under Details). ago(t): "3 min ago".
  function describeSync(st, ago) {
    const when = st.last_push || (st.last_commit && st.last_commit.at);
    if (!st.own) return { text: "Not on GitHub", cls: "off", detail: "This project is not on GitHub yet. Create a private GitHub repository for it below: from then on every change is saved there by itself." };
    if (!st.enabled) return { text: "Sync off", cls: "off", detail: "Your changes are not saved to GitHub by themselves. Switch it on below." };
    if (st.blocked_reason && st.remote) return { text: "Upload paused", cls: "warn",
      detail: st.blocked_plain || "Uploading to GitHub is paused; your edits are saved on this computer.",
      tech: `${st.blocked_reason}. Bound target: ${targetText(st.bound_target)}. Current target: ${targetText(st.target)}.` };
    if (st.clash && st.clash.length) return { text: "Clash with GitHub", cls: "warn", detail: st.error || "", tech: st.error_details || "" };
    if (st.error) return { text: "Not synced", cls: "warn", detail: st.error, tech: st.error_details || "" };
    if (st.state !== "idle") return { text: { committing: "Saving…", pushing: "Uploading…", pulling: "Getting changes…" }[st.state] || "Syncing…", cls: "busy", detail: "Working with GitHub…" };
    if (st.pending) {
      const n = Math.ceil((st.next_autosave || 0) / 60);
      return { text: "Not on GitHub yet", cls: "pending",
               detail: `Your latest edits are saved on this computer, not yet on GitHub. They go there ${n <= 0 ? "now" : `in about ${n} min`} (or once you stop editing for 2 min).` };
    }
    if (st.ahead && st.remote) return { text: "Not uploaded yet", cls: "pending",
      detail: `${st.ahead} saved change${st.ahead > 1 ? "s are" : " is"} not on GitHub yet; Prism uploads ${st.ahead > 1 ? "them" : "it"} by itself.`,
      tech: `${st.ahead} commit${st.ahead > 1 ? "s" : ""} not pushed to ${targetText(st.target)}.` };
    if (st.remote && st.kept && st.kept.length) return { text: `Both versions kept (${st.kept.length})`, cls: "pending",
      detail: "Saved to GitHub. Where you and a co-author changed the same lines, both versions are in the file: open the menu to go there." };
    if (!st.remote) return { text: "Not on GitHub", cls: "off", detail: "Your changes are saved in this project's history on this computer, not on GitHub. Create a GitHub repository for it below." };
    return { text: "Saved to GitHub", cls: "ok", detail: `Everything is on GitHub${when ? ", " + ago(when) : ""}.`,
             tech: `Synced to ${targetText(st.target)}.` };
  }

  // The sync switch: writes one at a time, the latest wish winning. A status asked for
  // before a write ended (stamp() then, fresh(stamp) after) shows the switch as it was:
  // drawn, it would flip the switch back until the next one.
  function createToggle({ send, onState = () => {}, onError = () => {} }) {
    let wanted = null, running = null, writes = 0;
    function change(value) {
      wanted = !!value;
      if (running) return running;
      running = (async () => {
        while (wanted !== null) {
          const mine = wanted;
          writes++;
          let r;
          try { r = await send(mine); } catch (e) { r = { _status: 0, error: e.message }; }
          const current = wanted === mine;
          if (current) wanted = null;
          if (r._status === 200) onState(r);
          else if (current) { onError(r); onState(null); }
        }
      })().finally(() => { running = null; });
      return running;
    }
    const stamp = () => running ? -1 : writes;
    const fresh = (asked) => asked >= 0 && asked === writes && !running;
    return { change, value: (server) => wanted === null ? !!server : wanted, stamp, fresh };
  }
  return { describeSync, targetText, createToggle };
})();

if (typeof module !== "undefined") module.exports = SyncState;
