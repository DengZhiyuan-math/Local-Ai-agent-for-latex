/* What the editor's sync button says (app.js renderSync), from /api/git/sync's status:
   the label, its colour class and the menu's explanation. Plain: checked under Node. */
"use strict";

const SyncState = (() => {
  const targetText = (t) => t ? `${t.repository_identity}, branch ${t.branch_ref.replace(/^refs\/heads\//, "")}` : "unavailable";

  // {text, cls, detail}. ago(t): "3 min ago" (util.js).
  function describeSync(st, ago) {
    const when = st.last_push || (st.last_commit && st.last_commit.at);
    if (!st.own) return { text: "Not on GitHub", cls: "off", detail: "This project has no git repository yet. Create a private GitHub repository for it below: it is then saved and synced automatically." };
    if (!st.enabled) return { text: "Sync off", cls: "off", detail: "Changes are not committed or pushed automatically. Switch it on below." };
    if (st.blocked_reason && st.remote) return { text: "Sync paused", cls: "warn", detail: `Local edits are saved. ${st.blocked_reason}. Bound target: ${targetText(st.bound_target)}. Current target: ${targetText(st.target)}.` };
    if (st.clash && st.clash.length) return { text: "Clash with GitHub", cls: "warn", detail: st.error || "" };
    if (st.error) return { text: "Not synced", cls: "warn", detail: st.error };
    if (st.state !== "idle") return { text: { committing: "Saving…", pushing: "Pushing…", pulling: "Pulling…" }[st.state] || "Syncing…", cls: "busy", detail: "Working with git…" };
    if (st.pending) {
      const n = Math.ceil((st.next_autosave || 0) / 60);
      return { text: "Unsaved changes", cls: "pending",
               detail: `Your latest edits are saved on disk, not yet in GitHub. They are committed ${n <= 0 ? "now" : `in about ${n} min`} (or once you stop editing for 2 min).` };
    }
    if (st.ahead && st.remote) return { text: `${st.ahead} to push`, cls: "pending", detail: `${st.ahead} commit${st.ahead > 1 ? "s" : ""} not on GitHub yet.` };
    if (st.remote && st.kept && st.kept.length) return { text: `Both versions kept (${st.kept.length})`, cls: "pending",
      detail: "Synced with GitHub. Where you and a co-author changed the same lines, both versions are in the file: open the menu to go there." };
    if (!st.remote) return { text: "Not on GitHub", cls: "off", detail: "Committed in the project's repository, which has no GitHub remote yet: nothing is pushed. Create one below." };
    return { text: "Saved to GitHub", cls: "ok", detail: `Synced to ${targetText(st.target)}${when ? ", " + ago(when) : ""}.` };
  }

  return { describeSync, targetText };
})();

if (typeof module !== "undefined") module.exports = SyncState;
