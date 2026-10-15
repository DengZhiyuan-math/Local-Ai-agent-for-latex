// A page reloaded mid-turn follows the turn and can stop it; a double send sends once.
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
const check = (name, ok, detail = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);
await send("Runtime.enable"); await send("Page.enable");
const ready = () => ev(`for (let i = 0; i < 150 && (typeof P === 'undefined' || !(P.projectKey && P.list.length)); i++) await new Promise((r) => setTimeout(r, 100)); return typeof P !== 'undefined' && !!P.projectKey`);
await ready();

// Double send: two Enter presses at once start one turn and show no error card.
await ev(`
  window.posts = 0;
  const orig = window.fetch;
  window.fetch = (u, o) => { if (String(u) === "/api/agent") posts++; return orig(u, o); };
  $("#chat-mode").value = "ask";
  $("#chat-input").value = "hello";
  chatSend(); chatSend();
  for (let i = 0; i < 50 && !C.job; i++) await new Promise((r) => setTimeout(r, 100));
  return true;`);
const first = await ev(`return { job: C.job, posts, errs: document.querySelectorAll("#chat-log .err").length }`);
check("double send starts one turn", first.job && first.posts === 1 && first.errs === 0, JSON.stringify(first));
await sleep(2000);

// Reload mid-turn.
await send("Page.reload");
await sleep(1500);
await ready();
const after = await ev(`
  for (let i = 0; i < 50 && !C.job; i++) await new Promise((r) => setTimeout(r, 100));
  return { job: C.job, button: $("#chat-send").textContent, note: [...document.querySelectorAll("#chat-log .sys")].some((n) => n.textContent.includes("already working")),
           streamed: $("#chat-log").textContent.includes("word1") };`);
check("reloaded page follows the running turn", after.job === first.job && after.button === "Stop" && after.note, JSON.stringify(after));
check("its events so far are shown", after.streamed);

await ev(`$("#chat-send").click(); return true`);
const stopped = await ev(`for (let i = 0; i < 100 && C.job; i++) await new Promise((r) => setTimeout(r, 100)); return { job: C.job, button: $("#chat-send").textContent }`);
check("Stop from the reloaded page ends the turn", stopped.job === null && stopped.button === "Send", JSON.stringify(stopped));
const info = await ev(`return (await api("/api/agent/info")).active`);
check("server has no running turn", info === null, JSON.stringify(info));
check("no page errors", errors.length === 0, errors.join(" | "));
console.log(results.join("\n"));
ws.close(); process.exit(0);
