import { closeSocket } from "./close.mjs";
// Final line highlights must describe the turn's actual diff, including replacements
// repeated across a file and edits made without streamed Edit/Write tool arguments.
const url = process.argv[2];
const tab = await (await fetch("http://127.0.0.1:9333/json/new?" + encodeURIComponent(url), { method: "PUT" })).json();
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r));
let id = 0;
const pending = new Map(), errors = [];
ws.addEventListener("message", m => {
  const d = JSON.parse(m.data);
  if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
  if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
});
const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async expr => {
  const r = await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || JSON.stringify(r.result.exceptionDetails));
  return r.result.result.value;
};
const results = [];
const check = (name, ok, detail = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);
await send("Runtime.enable"); await send("Page.enable");
await ev(`for (let i = 0; i < 150 && (typeof P === 'undefined' || !P.projectKey || !S.ready); i++) await new Promise(r => setTimeout(r, 100));`);

const before = [String.raw`\begin{lemma}`, "For every smooth vector,", String.raw`A=\pi(g_0)v`,
  "Unchanged context", String.raw`B=-\pi(g_0)d\pi(a_1)v`, "Unchanged context",
  String.raw`C=\pi(g_0)d\pi(W_j)v`, String.raw`\end{lemma}`].join("\n") + "\n";
const after = before.replaceAll("g_0", "ha_{-t}");
const shiftedAfter = "Inserted heading\n" + after.split("\n").filter(line => !line.startsWith("B=-")).join("\n");
const diff = ["--- a/__agent-marks__.tex", "+++ b/__agent-marks__.tex", "@@ -1,8 +1,8 @@",
  String.raw` \begin{lemma}`, " For every smooth vector,", String.raw`-A=\pi(g_0)v`, String.raw`+A=\pi(ha_{-t})v`,
  " Unchanged context", String.raw`-B=-\pi(g_0)d\pi(a_1)v`, String.raw`+B=-\pi(ha_{-t})d\pi(a_1)v`,
  " Unchanged context", String.raw`-C=\pi(g_0)d\pi(W_j)v`, String.raw`+C=\pi(ha_{-t})d\pi(W_j)v`,
  String.raw` \end{lemma}`, ""].join("\n");

const replay = await ev(`
  const path = "__agent-marks__.tex", before = ${JSON.stringify(before)}, after = ${JSON.stringify(after)}, diff = ${JSON.stringify(diff)};
  const original = { file: Q.file, tree: Q.tree, events: Q.events, active: S.active, auto: $("#auto-compile").checked };
  const tree = await Q.tree();
  let content = before, mtime = Date.now(), batch = 0;
  Q.file = async p => p === path ? { _status: 200, content, mtime } : original.file(p);
  Q.tree = async () => ({ ...tree, files: [...tree.files, { path, mtime, size: content.length }] });
  const marked = () => Array.from({ length: cm.lineCount() }, (_, l) => l).filter(l => (cm.lineInfo(l).bgClass || "").split(" ").includes("cm-agent-changed"));
  try {
    $("#auto-compile").checked = false;
    await poll(); await openFile(path);
    Q.events = async () => {
      await new Promise(r => setTimeout(r, batch === 3 ? 600 : 100));
      const events = [
        [{ t: "tool_start", id: "replace-all", name: "Edit" }, { t: "tool_live", id: "replace-all", path }],
        [{ t: "tool_live", id: "replace-all", old: "g_0", old_done: true, text: "ha_{-t}" }],
        [{ t: "tool_result", id: "replace-all", error: false }],
        [{ t: "done", turn: 100, changed: [{ path, diff }], exit: 0 }],
      ];
      if (batch === 2) { content = after; mtime++; }
      const i = batch++;
      return { _status: 200, events: events[i], done: i === 3 };
    };
    await followTurn("marks-replay", "recorded", "edit");
    await new Promise(r => setTimeout(r, 500));
    const streamed = { marked: marked(), text: cm.getValue() };
    // Providers can finish an edit through a shell or patch tool, without tool_live.
    Q.events = async () => ({ _status: 200, events: [{ t: "done", turn: 101, changed: [{ path, diff }], exit: 0 }], done: true });
    await followTurn("marks-no-stream", "recorded", "edit");
    const unstreamed = marked();
    // A later step inserts above and deletes the middle occurrence. Final-file
    // ranges must replace the old ones; they also survive a disk reload/tab swap.
    content = ${JSON.stringify(shiftedAfter)}; mtime++;
    Q.events = async () => ({ _status: 200, events: [{ t: "done", turn: 102,
      changed: [{ path, diff: "display diff truncated", lines: [[0, 1], [3, 4], [6, 7]] }], exit: 0 }], done: true });
    await followTurn("marks-final-lines", "recorded", "edit");
    const shifted = marked();
    await openFile(path); await reloadFromDisk(activeTab());
    const reloaded = marked();
    // Green marks belong to the disk result, not to a conflicting unsaved version.
    activeTab().conflict = "disk"; AH.apply(path);
    const conflict = marked();
    await reloadFromDisk(activeTab(), true);
    const resolved = marked();
    // A turn which undoes all its own edits leaves no highlights.
    Q.events = async () => ({ _status: 200, events: [{ t: "done", turn: 103, changed: [], exit: 0 }], done: true });
    await followTurn("marks-reverted", "recorded", "edit");
    return { streamed, unstreamed, shifted, reloaded, conflict, resolved, reverted: marked(), finalText: cm.getValue() };
  } finally {
    Q.file = original.file; Q.tree = original.tree; Q.events = original.events;
    $("#auto-compile").checked = original.auto;
    AH.clear(); TABS.remove(t => t.path === path);
    if (original.active) await openFile(original.active); else showNoFile();
    await poll();
  }
`);
check("all replace-all occurrences are highlighted, without unchanged context", replay.streamed.text === after
  && JSON.stringify(replay.streamed.marked) === "[2,4,6]", JSON.stringify(replay.streamed.marked));
check("final diff highlights edits without live tool arguments", JSON.stringify(replay.unstreamed) === "[2,4,6]", JSON.stringify(replay.unstreamed));
check("later insertions/deletions use final positions, including after reload", JSON.stringify(replay.shifted) === "[0,3,6]"
  && JSON.stringify(replay.reloaded) === "[0,3,6]" && !replay.finalText.includes("B=-"), JSON.stringify([replay.shifted, replay.reloaded]));
check("a conflicting unsaved version does not receive disk-result highlights", replay.conflict.length === 0);
check("accepting the disk result restores its highlights", JSON.stringify(replay.resolved) === "[0,3,6]");
check("reverted edits leave no final highlights", replay.reverted.length === 0);
check("no page errors", errors.length === 0, errors.join(" | "));
console.log(results.join("\n"));
await closeSocket(ws); await fetch("http://127.0.0.1:9333/json/close/" + tab.id);
process.exitCode = results.some(line => line.startsWith("FAIL")) ? 1 : 0;
