import { closeSocket } from "./close.mjs";
// Search with the agent on the real page: the agent (the fake API, answering with what this
// check writes in agent-answer.txt next to the project) proposes rules and changes; each is
// listed, can be unticked, the rules edited; Apply makes the rest.
// usage: node agent_search.mjs <editor url> <project folder>
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const [url, root] = process.argv.slice(2);
const answerFile = join(root, "..", "agent-answer.txt");
const results = [];
const check = (name, ok, detail = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);
const NOTATION = "Let $x_i$ and $a_{i}$ be given.\nFor i in I, \\[\\sum_{i=1}^n y_i\\] holds.\n% x_i stays\n";
mkdirSync(join(root, "sec"), { recursive: true });
writeFileSync(join(root, "notation.tex"), NOTATION);
writeFileSync(join(root, "sec", "n2.tex"), "Here $z_i$.\n");
const answer = (o) => writeFileSync(answerFile, "I looked at the formulas.\n```prism-search\n" + JSON.stringify(o) + "\n```\n");
answer({ summary: "Rename the index i to j.", rules: [
  { find: "_i\\b", replace: "_j", where: "math", note: "a single index" },
  { find: "_\\{i\\}", replace: "_{j}", where: "math", note: "a braced index" }],
edits: [{ file: "notation.tex", line: 2, old: "\\sum_{i=1}", new: "\\sum_{j=1}", note: "the bound index" }] });

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
  // A step that throws is a failed check, and the rest still run.
  if (r.result.exceptionDetails) { check("a step ran", false, r.result.exceptionDetails.exception?.description || "evaluate failed"); return null; }
  return r.result.result.value;
};
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 900, deviceScaleFactor: 1, mobile: false });
await ev(`
  for (let i = 0; i < 150 && (typeof S === "undefined" || !S.files.length); i++) await new Promise((r) => setTimeout(r, 100));
  window.confirm = () => true;
  window.T = { until: async (fn, ms = 8000) => { for (let t = 0; t < ms; t += 100) { if (await fn()) return true; await new Promise((r) => setTimeout(r, 100)); } return false; } };
  await T.until(async () => { await poll(); return S.files.some((f) => f.path === "sec/n2.tex"); });
  await T.until(() => typeof C !== "undefined" && C.provider && P.projectKey);
  await openFile("notation.tex");
  store.set("psearch.mode", "agent");
  psOpen();
  return true`);
const state = () => ev(`return { agent: !$("#ps-agent").hidden, focus: document.activeElement.id, status: $("#as-status").textContent,
  bad: $("#as-status").classList.contains("bad"), count: $("#as-count").textContent, rules: [...document.querySelectorAll("#as-rules .n")].map((n) => n.textContent),
  changes: [...document.querySelectorAll("#as-results .as-change .diff")].map((d) => d.textContent), width: $("#sidebar").offsetWidth,
  summary: $("#as-summary").textContent.trim(), problems: $("#as-problems").hidden ? 0 : $("#as-problems").querySelectorAll("li").length }`);
let s = await state();
check("Search opens in the mode used last: with the agent, the request box focused", s.agent && s.focus === "as-q", JSON.stringify(s));

await ev(`$("#as-q").value = "rename the index i to j"; $("#as-go").click(); return true`);
const came = await ev(`return T.until(() => $("#as-count").textContent.startsWith("5 of 5 changes"), 30000)`);
s = await state();
check("the agent's proposal is listed: each change, by file, all ticked", came && s.count === "5 of 5 changes in 2 files"
  && s.summary === "Rename the index i to j." && s.rules.join("|") === "3 changes|1 change" && !s.bad, JSON.stringify(s));
check("only in formulas: not the text, not a comment", s.changes.every((c) => !c.includes("For i in") || c.includes("sum")) && s.changes.length === 5
  && !s.changes.some((c) => c.includes("stays")), JSON.stringify(s.changes));
check("the sidebar is widened for the changes", s.width >= 400, String(s.width));
check("the search turn is not in the agent's conversation", await ev(`return !$("#chat-log").textContent.includes("prism-search")
  && !$("#chat-log").textContent.includes("I looked at the formulas")`));

// Untick one change; edit a rule: the preview follows.
await ev(`[...document.querySelectorAll("#as-results .as-change")].find((li) => li.textContent.includes("y_i_j"))?.querySelector("input").click(); return true`);
s = await state();
check("an unticked change is left out", s.count === "4 of 5 changes in 2 files", s.count);
await ev(`const r = document.querySelectorAll("#as-rules li")[1].querySelector('[data-f="replace"]'); r.value = "_{k}"; r.dispatchEvent(new Event("input", { bubbles: true })); return true`);
const edited = await ev(`return T.until(() => [...document.querySelectorAll("#as-results ins")].some((i) => i.textContent === "_{k}"))`);
s = await state();
check("an edited rule is previewed again, the unticked change stays unticked", edited && s.count === "4 of 5 changes in 2 files", JSON.stringify(s));
await ev(`const f = document.querySelectorAll("#as-rules li")[0].querySelector('[data-f="find"]'); f.value = "(_i"; f.dispatchEvent(new Event("input", { bubbles: true })); return true`);
check("an invalid expression is marked and the last valid one stays", await ev(`await new Promise((r) => setTimeout(r, 500));
  return document.querySelectorAll("#as-rules li")[0].querySelector('[data-f="find"]').classList.contains("bad")`)
  && (await state()).count === "4 of 5 changes in 2 files");

// Apply: the open file in its tab, the other on disk.
await ev(`$("#as-apply").click(); return true`);
const applied = await ev(`await T.until(() => $("#as-count").textContent === "1 of 1 change in 1 file" || $("#as-count").textContent === "0 of 1 change in 1 file");
  await T.until(() => !S.tabs.some(isDirty));
  return { tab: TABS.get("notation.tex").doc.getValue(), count: $("#as-count").textContent }`);
const disk = (f) => readFileSync(join(root, ...f.split("/")), "utf8");
check("Apply makes the ticked changes, in the open file and on disk", applied?.tab === "Let $x_j$ and $a_{k}$ be given.\nFor i in I, \\[\\sum_{j=1}^n y_i\\] holds.\n% x_i stays\n"
  && disk("sec/n2.tex") === "Here $z_j$.\n" && disk("notation.tex") === applied.tab, JSON.stringify({ ...applied, n2: disk("sec/n2.tex") }));
check("what is left is listed again", applied?.count.endsWith("of 1 change in 1 file"), applied?.count);
await ev(`await openFile("notation.tex"); cm.undo(); return true`);
check("one undo in the file takes its changes back", await ev(`return cm.getValue()`) === NOTATION);

// An answer without a proposal is said, with the answer under Details.
writeFileSync(answerFile, "Sorry, I could not decide.");
await ev(`$("#as-go").click(); return true`);
const said = await ev(`return T.until(() => $("#as-status").classList.contains("bad"), 30000)`);
s = await state();
check("an answer without a proposal is said plainly, the last proposal gone", said && s.status.includes("no prism-search block")
  && !s.changes.length && !s.rules.length
  && await ev(`return $("#as-summary details")?.textContent.includes("could not decide")`), JSON.stringify(s));
await ev(`psClose(); return true`);
check("closing Search gives the sidebar its width back", (await state()).width < 400);
check("no page errors", errors.length === 0, errors.join(" | "));

console.log(results.join("\n"));
await closeSocket(ws);
await fetch("http://127.0.0.1:9333/json/close/" + tab.id);   // its page goes too: it holds connections
process.exitCode = results.some(line => line.startsWith("FAIL")) ? 1 : 0;
