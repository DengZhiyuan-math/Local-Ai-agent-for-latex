// Frontend audit regressions against the runner's isolated editor and Chrome profile.
const url = process.argv[2];
let failures = 0;
function check(name, ok) { console.log(`${ok ? "PASS" : "FAIL"} ${name}`); if (!ok) failures++; }
async function page(inject = "") {
  const tab = await (await fetch("http://127.0.0.1:9333/json/new?about:blank", { method: "PUT" })).json();
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
  const ev = async (code) => {
    const r = await send("Runtime.evaluate", { expression: `(async () => { ${code} })()`, awaitPromise: true, returnByValue: true });
    if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || "evaluate failed");
    return r.result.result.value;
  };
  await send("Runtime.enable"); await send("Page.enable");
  if (inject) await send("Page.addScriptToEvaluateOnNewDocument", { source: inject });
  await send("Page.navigate", { url });
  const wait = async (condition) => {
    for (let i = 0; i < 160; i++) {
      if (await ev(`try { return !!(${condition}); } catch { return false; }`)) return;
      await new Promise((r) => setTimeout(r, 50));
    }
    throw new Error("Timed out: " + condition + " " + errors.join(" | "));
  };
  return { ev, wait, errors, close: async () => { ws.close(); await fetch("http://127.0.0.1:9333/json/close/" + tab.id); } };
}

let p = await page(`
  localStorage.setItem("prism.session", JSON.stringify({ tabs: ["sec/a.tex", "main.tex"], active: "main.tex" }));
  const originalFetch = window.fetch; let trees = 0;
  window.fetch = async (...args) => {
    if (String(args[0]).startsWith("/api/tree")) {
      if (++trees === 1) return new Response('{}', { status: 503 });
      if (trees === 2) await new Promise(r => setTimeout(r, 900));
    }
    return originalFetch(...args);
  };`);
try {
  await p.wait("typeof store !== 'undefined' && typeof S !== 'undefined'");
  check("a failed first tree request preserves the saved session", await p.ev(`return store.get("session").tabs.length === 2;`));
  await p.wait("S.ready && !S.restoring && S.tabs.length === 2");
  check("session tabs restore after the first successful response", await p.ev(`return S.active === "main.tex" && !!TABS.get("sec/a.tex");`));
  check("deleted files keep their text and cannot be autosaved", await p.ev(`
    await openFile("sec/a.tex"); const t = activeTab(); t.doc.setValue("keep these edits");
    const realTree = Q.tree, realSave = Q.saveFile;
    Q.tree = async () => ({ ...S.watch, _status: 200, files: S.files.filter(f => f.path !== t.path) });
    await poll(); let writes = 0; Q.saveFile = async () => { writes++; return { _status: 200 }; };
    const saved = await TABS.save(t);
    Q.tree = realTree; Q.saveFile = realSave;
    return saved.reason === "gone" && writes === 0 && t.doc.getValue() === "keep these edits" && $("#banner").textContent.includes("deleted");`));
  check("a reused port stops polling and saving without replacing edits", await p.ev(`
    let polls = 0; Q.tree = async () => { polls++; return { ...S.watch, _status: 200, project_key: "another-paper" }; };
    await poll(); await poll(); const t = activeTab(), text = t.doc.getValue();
    const saved = await TABS.save(t);
    return foreignProject && polls === 1 && !saved.ok && text === "keep these edits" && !!$("#other-project");`));
  check("startup and reconciliation raise no page errors", p.errors.length === 0);
} finally { await p.close(); }
p = await page();
try {
  await p.wait("S.ready && !S.restoring && S.active");
  check("a temporary event failure keeps the turn and saves held", await p.ev(`
    const real = Q.events; let calls = 0;
    Q.events = async () => ++calls === 1 ? { _status: 502 } : { _status: 200, done: true,
      events: [{ t: "done", turn: 9001, provider: "slow", changed: [], outcome: "stopped", exit: 1, stderr: "terminated" }] };
    const following = followTurn(9001, "slow", "edit");
    await new Promise(r => setTimeout(r, 30));
    const held = C.job === 9001 && C.editTurn && (await TABS.save(activeTab())).reason === "held";
    await following; Q.events = real;
    return held && calls === 2 && !C.job && !$("#chat-log").lastElementChild.querySelector(".err");`));
  check("stopped turns have one neutral outcome", await p.ev(`return $("#chat-log").textContent.includes("Stopped") && !$("#chat-log").textContent.includes("terminated");`));
  check("Undo is disabled during a turn and Stop failures remain retryable", await p.ev(`
    renderTurnCard({ turn: 8000, outcome: "ok", exit: 0, changed: [{ path: "main.tex", diff: "+x" }] });
    const realEvents = Q.events, realStop = Q.stop; let release;
    Q.events = () => new Promise(r => { release = r; });
    Q.stop = async () => ({ _status: 503, error: "temporarily busy" });
    const following = followTurn(9002, "slow", "edit");
    const disabled = document.querySelector('[data-undo="8000"]').disabled;
    await stopTurn(); const retryable = !C.stopping && $("#chat-send").textContent === "Stop";
    release({ _status: 200, done: true, events: [{ t: "done", turn: 9002, outcome: "ok", exit: 0, changed: [] }] });
    await following; Q.events = realEvents; Q.stop = realStop;
    return disabled && retryable && !document.querySelector('[data-undo="8000"]').disabled;`));
  check("a missing job reports an unknown result without offering Undo", await p.ev(`
    const real = Q.events; Q.events = async () => ({ _status: 404, error: "unknown job" });
    await followTurn(9003, "slow", "edit"); Q.events = real;
    return $("#chat-log").textContent.includes("result is unknown") && !document.querySelector('[data-undo="9003"]');`));
  check("turn lifecycle raises no page errors", p.errors.length === 0);
  check("attachments and a draft added while sending remain for the next message", await p.ev(`
    const realAgent = Q.agent, realEvents = Q.events; let release;
    ATT.files = [{ id: 100, name: "first.txt", path: "first.txt", size: 1 }];
    $("#chat-input").value = "first message";
    Q.agent = () => new Promise(r => { release = r; });
    Q.events = async () => ({ _status: 200, done: true, events: [{ t: "done", outcome: "ok", exit: 0, changed: [] }] });
    const sending = sendMessage();
    for (let i = 0; i < 100 && !release; i++) await new Promise(r => setTimeout(r, 10));
    if (!release) throw Error("message was not sent");
    ATT.files.push({ id: 101, name: "next.txt", path: "next.txt", size: 2 });
    $("#chat-input").value = "next draft";
    release({ _status: 200, job: 9004 }); await sending;
    const kept = ATT.files.length === 1 && ATT.files[0].id === 101 && $("#chat-input").value === "next draft";
    ATT.files = []; ATT.render(); $("#chat-input").value = ""; Q.agent = realAgent; Q.events = realEvents;
    return kept;`));
} finally { await p.close(); }
process.exitCode = failures ? 1 : 0;
