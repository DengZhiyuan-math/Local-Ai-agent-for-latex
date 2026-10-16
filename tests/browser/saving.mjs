import { closeSocket } from "./close.mjs";
// Saving in the editor (tabs.js) on the real page: autosave, a file changed on disk while
// you have edits (the banner, Keep mine), and edits held during an agent's Edit turn.
// usage: node saving.mjs <editor url> <project folder>
import { readFileSync } from "node:fs";
import { join } from "node:path";

const [url, root] = process.argv.slice(2);
const file = join(root, "main.tex");
const results = [];
const check = (name, ok, detail = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);

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
  if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || "evaluate failed");
  return r.result.result.value;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await send("Runtime.enable");
await ev(`
  for (let i = 0; i < 150 && (typeof S === "undefined" || !S.files.length); i++) await new Promise((r) => setTimeout(r, 100));
  window.confirm = () => true;
  window.T = { until: async (fn, ms = 6000) => { for (let t = 0; t < ms; t += 100) { if (await fn()) return true; await new Promise((r) => setTimeout(r, 100)); } return false; } };
  await openFile("main.tex");
  return true;`);
// The page restores the last session's tabs (another file in front, from the earlier
// checks) while main.tex is opened: the file opened stays in front.
await sleep(1500);
const front = await ev(`return { active: S.active, tabs: S.tabs.map((t) => t.path) }`);
check("a file opened while the last tabs come back stays in front", front.active === "main.tex", JSON.stringify(front));

// Autosave: typed, then saved a moment later.
await ev(`cm.replaceRange("% typed\\n", { line: 1, ch: 0 }); return true`);
const auto = await ev(`
  const ok = await T.until(() => !isDirty(activeTab()));
  const t = activeTab();
  const state = { ok, path: t.path, dirty: isDirty(t), inFlight: t.inFlight, conflict: t.conflict, mtime: t.mtime,
                  status: ($("#notifications")?.textContent || ""), editTurn: C.editTurn };
  if (!ok) state.direct = await Promise.race([TABS.save(t), new Promise((r) => setTimeout(() => r("no answer in 5 s"), 5000))]);
  return state;`);
const disk0 = readFileSync(file, "utf8");
auto.disk = disk0.slice(0, 80);
check("autosave writes the edit", auto.ok && auto.path === "main.tex" && disk0.includes("% typed"), JSON.stringify(auto));

// Changed on disk while you have edits: the banner, then Keep mine. Another program's
// write (here: the server's own, around the tab) lands while the edit is not saved yet.
const banner = await ev(`
  cm.replaceRange("% mine\\n", { line: 1, ch: 0 });
  clearTimeout(activeTab().saveTimer);
  await new Promise((r) => setTimeout(r, 20));
  const w = await api("/api/file", { path: "main.tex", content: "\\\\documentclass{article}\\n% theirs\\n", force: true });
  await poll();
  const shown = await T.until(() => !$("#banner").hidden && activeTab().conflict === "disk");
  return { shown, written: w._status, dirty: isDirty(activeTab()), conflict: activeTab().conflict };`);
check("a change on disk under your edits shows the banner", banner.shown, JSON.stringify(banner));
if (banner.shown) {
  await ev(`$("#bn-keep").click(); return true`);
  check("Keep mine overwrites the disk and closes the banner",
    await ev(`return T.until(() => $("#banner").hidden && !isDirty(activeTab()))`) && readFileSync(file, "utf8").includes("% mine"),
    readFileSync(file, "utf8").slice(0, 80));
}

// During an agent's Edit turn nothing is written; it is saved when the turn ends.
await ev(`C.editTurn = true; TABS.turnStarted(); cm.replaceRange("% held\\n", { line: 1, ch: 0 }); return true`);
const held = await ev(`const r = await saveAll(); return { r, status: ($("#notifications")?.textContent || "") }`);
check("held during an Edit turn, and said so", held.r.reason === "held" && !readFileSync(file, "utf8").includes("% held")
  && held.status.includes("saved when the agent's turn ends"), JSON.stringify(held));
await ev(`C.editTurn = false; flushSaves(); return true`);
check("saved once the turn is over", await ev(`return T.until(() => !isDirty(activeTab()))`) && readFileSync(file, "utf8").includes("% held"));
check("no page errors", errors.length === 0, errors.join(" | "));
console.log(results.join("\n"));
await closeSocket(ws);
await fetch("http://127.0.0.1:9333/json/close/" + tab.id);   // its page goes too: it holds connections
process.exitCode = results.some(line => line.startsWith("FAIL")) ? 1 : 0;
