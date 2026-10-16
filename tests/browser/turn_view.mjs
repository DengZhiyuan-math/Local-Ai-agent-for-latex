import { closeSocket } from "./close.mjs";
// What a turn's events draw in the agent panel (turn.js decides, app.js draws): a recorded
// Claude Code turn is fed to followTurn through a stand-in for Q.events. Its provider
// ("recorded") is not the menu's: the session it brings is kept apart from the real ones.
const url = process.argv[2];
const tab = await (await fetch("http://127.0.0.1:9333/json/new?" + encodeURIComponent(url), { method: "PUT" })).json();
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let id = 0;
const pending = new Map(), errors = [];
ws.addEventListener("message", (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
  if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || JSON.stringify(r.result.exceptionDetails));
  return r.result.result.value;
};
const results = [];
const check = (name, ok, detail = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);
await send("Runtime.enable"); await send("Page.enable");
await ev(`for (let i = 0; i < 150 && (typeof P === 'undefined' || !(P.projectKey && P.list.length)); i++) await new Promise((r) => setTimeout(r, 100)); return true`);

// Three batches, as the server hands them out; the turn is done after the last.
const batches = [
  [{ t: "init", session_id: "rec-1" }, { t: "message_start" }, { t: "thinking_start" }, { t: "thinking", text: "Let me look." }],
  [{ t: "delta", text: "I will " }, { t: "delta", text: "**fix** it." }, { t: "tool_start", id: "u1", name: "Edit" },
    { t: "tool_live", id: "u1", path: "main.tex" }, { t: "tool_live", id: "u1", old: "Hi", old_done: true },
    { t: "tool_live", id: "u1", text: "Hello\nworld" }],
  [{ t: "tool", id: "u1", name: "Edit", summary: "main.tex", path: "main.tex" }, { t: "tool_result", id: "u1", error: false, preview: "" },
    { t: "tool", id: "r1", name: "Bash", summary: "ls" }, { t: "tool_result", id: "r1", error: true, preview: "denied" },
    { t: "message_start" }, { t: "delta", text: "Done." }, { t: "text", text: "Done." },
    { t: "done", turn: 1, changed: [], exit: 0 }],
];
const drawn = await ev(`
  const batches = ${JSON.stringify(batches)};
  const orig = Q.events, statuses = [];
  Q.events = async (job, after) => {
    const i = batches.findIndex((b, k) => batches.slice(0, k).flat().length === after);
    await new Promise((r) => setTimeout(r, 300));
    statuses.push($("#chat-status").textContent);
    return { _status: 200, events: batches[i], done: i === batches.length - 1 };
  };
  $("#chat-log").innerHTML = "";
  try { await followTurn("recorded", "recorded", "ask"); } finally { Q.events = orig; }
  const log = $("#chat-log");
  return {
    statuses,
    thinking: [...log.querySelectorAll("details.thinking")].map((d) => [d.querySelector("summary").textContent, d.open, d.querySelector("pre")?.textContent]),
    messages: [...log.querySelectorAll(".msg.assistant")].map((m) => m.innerHTML.trim()),
    tools: [...log.querySelectorAll(".tool")].map((t) => [t.textContent.trim(), t.classList.contains("err")]),
    live: [...log.querySelectorAll("details.live-file")].map((d) => [d.querySelector("summary").textContent, d.open, d.querySelector("pre").textContent]),
    card: log.querySelector(".card")?.textContent || "",
    session: store.get(conversationKey("session", "recorded")), current: C.session,
    job: C.job, button: $("#chat-send").textContent, status: $("#chat-status").textContent,
  };`);
check("thinking box: its text, closed with the time", drawn.thinking.length === 1 && /^Thought for \d+s$/.test(drawn.thinking[0][0])
  && drawn.thinking[0][1] === false && drawn.thinking[0][2] === "Let me look.", JSON.stringify(drawn.thinking));
check("replies: streamed pieces joined, the whole text not drawn twice",
  drawn.messages.length === 2 && drawn.messages[0].includes("<strong>fix</strong>") && drawn.messages[1].includes("Done."), JSON.stringify(drawn.messages));
check("tool rows: summary, ✓ and ✗", JSON.stringify(drawn.tools) === JSON.stringify([["✓Edit main.tex", false], ["✗Bash ls", true]]), JSON.stringify(drawn.tools));
check("file written live: its box named, its text, closed", drawn.live.length === 1 && drawn.live[0][0] === "Edit main.tex"
  && drawn.live[0][1] === false && drawn.live[0][2] === "Hello\nworld", JSON.stringify(drawn.live));
check("status line followed the turn", drawn.statuses[1] === "thinking… 0s" && drawn.statuses[2] === "writing main.tex… (2 lines)", JSON.stringify(drawn.statuses));
check("turn card and session", drawn.card.includes("No file changes detected") && drawn.session === "rec-1" && drawn.current !== "rec-1",
  JSON.stringify([drawn.card, drawn.session, drawn.current]));
check("the panel is free again", drawn.job === null && drawn.button === "Send" && drawn.status === "", JSON.stringify(drawn));
check("no page errors", errors.length === 0, errors.join(" | "));
console.log(results.join("\n"));
await closeSocket(ws); await fetch("http://127.0.0.1:9333/json/close/" + tab.id);   // its page goes too: it holds connections
process.exitCode = results.some(line => line.startsWith("FAIL")) ? 1 : 0;
