// Many pages of one editor at once: a browser keeps at most six connections to one server,
// so the pages share one presence stream, and a hidden page follows an agent's turn
// without holding a connection. usage: node many_pages.mjs <editor url> <project folder>
import { readFileSync } from "node:fs";
import { join } from "node:path";

const [url, root] = process.argv.slice(2);
const results = [];
const check = (name, ok, detail = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function open() {
  const tab = await (await fetch("http://127.0.0.1:9333/json/new?" + encodeURIComponent(url), { method: "PUT" })).json();
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  let id = 0;
  const pending = new Map();
  ws.addEventListener("message", (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
  });
  const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (expr) => {
    const r = await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
    if (!r.result) throw new Error("CDP: " + JSON.stringify(r.error) + " for " + expr.slice(0, 60));
    if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || "evaluate failed");
    return r.result.result.value;
  };
  await send("Runtime.enable");
  return { ev, close: () => { ws.close(); return fetch("http://127.0.0.1:9333/json/close/" + tab.id); } };
}
// Loaded: its scripts ran and the files are listed (false after 15 s).
// (A page still navigating loses what runs in it: asked again.)
async function loaded(p) {
  for (let t = 0; t < 5; t++) {
    try {
      return await p.ev(`for (let i = 0; i < 150 && !(typeof S !== "undefined" && S.files.length && typeof openFile === "function"); i++)
        await new Promise((r) => setTimeout(r, 100)); return typeof S !== "undefined" && S.files.length > 0`);
    } catch (e) { if (!String(e).includes("context was destroyed")) throw e; await sleep(300); }
  }
  return false;
}
const holders = async (pages) => (await Promise.all(pages.map((p) => p.ev(`return PRESENCE.holdsStream()`)))).filter(Boolean).length;

const pages = [];
try {
  for (let i = 0; i < 8; i++) pages.push(await open());
  const ok = await Promise.all(pages.map(loaded));
  check("8 pages of one editor all load", ok.every(Boolean), JSON.stringify(ok));
  await sleep(1000);
  check("one of them holds the presence stream", (await holders(pages)) === 1, String(await holders(pages)));

  const last = pages[pages.length - 1];
  await last.ev(`await openFile("main.tex"); cm.replaceRange("% from page 8\\n", { line: 1, ch: 0 }); return true`);
  const saved = await last.ev(`for (let i = 0; i < 80 && isDirty(activeTab()); i++) await new Promise((r) => setTimeout(r, 100)); return !isDirty(activeTab())`);
  check("the eighth page saves", saved && readFileSync(join(root, "main.tex"), "utf8").includes("% from page 8"));

  const count = await last.ev(`return (await api("/api/ping")).pages`);
  check("the server counts every page", count === 8, String(count));

  const holder = (await Promise.all(pages.map((p) => p.ev(`return PRESENCE.holdsStream()`)))).indexOf(true);
  await pages[holder].close();
  pages.splice(holder, 1);
  let n = 0;
  for (let t = 0; t < 30 && n !== 1; t++) { await sleep(200); n = await holders(pages); }
  check("another page takes the stream over when its page closes", n === 1, String(n));

  // A turn followed by a hidden page: it asks for events without the server waiting.
  const [a, b] = pages;
  await a.ev(`$("#chat-mode").value = "ask"; $("#chat-input").value = "hello"; chatSend();
    for (let i = 0; i < 50 && !C.job; i++) await new Promise((r) => setTimeout(r, 100)); return true`);
  await b.ev(`location.reload(); return true`);
  await sleep(500);
  await loaded(b);
  const follows = await b.ev(`for (let i = 0; i < 50 && !C.job; i++) await new Promise((r) => setTimeout(r, 100)); return !!C.job`);
  const asked = await b.ev(`
    window.asked = [];
    const orig = window.fetch;
    window.fetch = (u, o) => { if (String(u).includes("/api/agent/events")) asked.push(String(u)); return orig(u, o); };
    Object.defineProperty(document, "visibilityState", { get: () => "hidden", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await new Promise((r) => setTimeout(r, 12000));
    return asked;`);
  check("a hidden page follows the turn without the server waiting", follows && asked.length >= 1 && asked.slice(1).every((u) => u.includes("wait=0"))
    && asked.slice(1).length >= 1, JSON.stringify({ follows, asked }));
  await a.ev(`if (C.job) await api("/api/agent/stop", { job: C.job }); for (let i = 0; i < 100 && C.job; i++) await new Promise((r) => setTimeout(r, 100)); return true`);
} catch (e) {
  check("no error on the way", false, String(e).slice(0, 200));
} finally {
  for (const p of pages) await p.close().catch(() => {});
}
console.log(results.join("\n"));
process.exit(0);
