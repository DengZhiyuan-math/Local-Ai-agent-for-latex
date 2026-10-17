import { closeSocket } from "./close.mjs";
// The editor's basics on the real page, with real keys: find and replace in the open file.
// usage: node editing.mjs <editor url> <project folder>
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const [url, root] = process.argv.slice(2);
const results = [];
const check = (name, ok, detail = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);
const FIND_TEXT = "alpha beta alpha\nGamma Alpha\nalphabet\n";
writeFileSync(join(root, "find.tex"), FIND_TEXT);

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
// A key as the keyboard sends it. mods: 1 Alt, 2 Ctrl, 4 Meta, 8 Shift.
const KEYS = { Enter: 13, Escape: 27, F3: 114 };
async function key(k, mods = 0) {
  const code = KEYS[k] ? k : "Key" + k.toUpperCase();
  const vk = KEYS[k] || k.toUpperCase().charCodeAt(0);
  const base = { key: k, code, modifiers: mods, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk };
  await send("Input.dispatchKeyEvent", { type: "rawKeyDown", ...base });
  await send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
  await sleep(60);
}
const type = async (text) => { await send("Input.insertText", { text }); await sleep(250); };
const bar = () => ev(`return { open: !$("#find").hidden, count: $("#find-count").textContent, focus: document.activeElement.id,
  sel: cm.getSelection(), replace: !$("#find-replace").hidden, text: cm.getValue() }`);

await send("Runtime.enable");
await ev(`
  for (let i = 0; i < 150 && (typeof S === "undefined" || !S.files.length); i++) await new Promise((r) => setTimeout(r, 100));
  window.confirm = () => true;
  window.T = { until: async (fn, ms = 6000) => { for (let t = 0; t < ms; t += 100) { if (await fn()) return true; await new Promise((r) => setTimeout(r, 100)); } return false; } };
  await T.until(async () => { await poll(); return S.files.some((f) => f.path === "find.tex"); });
  await openFile("find.tex");
  store.set("find.opts", {}); Object.assign(FIND.opts, { caseSensitive: false, wholeWord: false, regex: false }); findOptsShown();
  cm.focus(); cm.setCursor({ line: 0, ch: 0 });
  return true`);
await sleep(300);

/* ---------------------------------------------------------------- find in the file */
await key("f", 2);
let b = await bar();
check("Ctrl+F in the editor opens the find bar, its box focused", b.open && b.focus === "find-q", JSON.stringify(b));
await type("alpha");
b = await bar();
check("typing finds: every match counted (any case), the first selected", b.count === "1 of 4" && b.sel === "alpha", JSON.stringify(b));
check("the matches are marked in the text", await ev(`return document.querySelectorAll("#editor .cm-find-hit").length >= 4
  && document.querySelectorAll("#editor .cm-find-cur").length === 1`));
await key("Enter");
b = await bar();
check("Enter goes to the next match", b.count === "2 of 4", b.count);
await key("Enter", 8);
await key("Enter", 8);
b = await bar();
check("Shift+Enter goes back, round past the first", b.count === "4 of 4" && b.sel === "alpha", JSON.stringify(b));
await key("w", 1);
b = await bar();
check("Alt+W: whole words only", b.count.endsWith("of 3") || b.count === "3", b.count);
await key("c", 1);
b = await bar();
check("Alt+C: match case", b.count.endsWith("2"), b.count);
check("the options are kept and shown", await ev(`return FIND.opts.caseSensitive && FIND.opts.wholeWord
  && $('#find [data-opt="wholeWord"]').getAttribute("aria-pressed") === "true" && store.get("find.opts").wholeWord`));
await key("c", 1); await key("w", 1);

/* ---------------------------------------------------------------- replace */
await key("h", 2);
b = await bar();
check("Ctrl+H shows the replace box and focuses it", b.replace && b.focus === "find-r", JSON.stringify(b));
await type("omega");
await ev(`$("#find-all").click(); return true`);
await sleep(200);
b = await bar();
check("Replace all changes every match", b.text === "omega beta omega\nGamma omega\nomegabet\n" && b.count === "No results", JSON.stringify(b));
await ev(`cm.undo(); return true`);
await sleep(250);
b = await bar();
check("one undo takes all of them back", b.text === FIND_TEXT && (b.count.endsWith("of 4") || b.count === "4"), JSON.stringify(b));
await ev(`cm.setCursor({ line: 0, ch: 0 }); $("#find-r").focus(); return true`);
await key("Enter");                      // in the replace box: selects the first match
await key("Enter");                      // and replaces it
b = await bar();
check("Enter in the replace box replaces the selected match and goes on", b.text.startsWith("omega beta alpha") && b.sel === "alpha"
  && b.count === "1 of 3", JSON.stringify(b));
await ev(`cm.undo(); return true`);

// A regular expression, with a group in the replacement; a broken one is said.
await ev(`$("#find-q").focus(); $("#find-q").select(); return true`);
await key("r", 1);
await type("(\\w+)bet");
await ev(`$("#find-r").value = "$1-bet"; $("#find-all").click(); return true`);
await sleep(200);
b = await bar();
check("a regular expression's $1 in the replacement", b.text.includes("alpha-bet\n"), JSON.stringify(b.text));
await ev(`cm.undo(); $("#find-q").focus(); $("#find-q").select(); return true`);
await type("(alpha");
b = await bar();
check("an invalid expression is said, without an error", b.count === "Invalid pattern"
  && await ev(`return $("#find-q").classList.contains("bad")`), b.count);
await key("r", 1);

// Esc closes the bar and gives the editor back; Ctrl+F from outside the editor opens it.
await key("Escape");
b = await bar();
check("Esc closes the bar, the marks go, the editor has the focus", !b.open && await ev(`return cm.hasFocus()
  && !document.querySelector("#editor .cm-find-hit")`), JSON.stringify(b));
await ev(`document.activeElement.blur(); $("#tree").focus(); return true`);
await key("f", 2);
b = await bar();
check("Ctrl+F elsewhere on the page opens the editor's bar, not the browser's", b.open, JSON.stringify(b));
await key("Escape");
check("the editor's text is as it was", (await bar()).text === FIND_TEXT);
check("no page errors", errors.length === 0, errors.join(" | "));

console.log(results.join("\n"));
await closeSocket(ws);
await fetch("http://127.0.0.1:9333/json/close/" + tab.id);   // its page goes too: it holds connections
process.exitCode = results.some(line => line.startsWith("FAIL")) ? 1 : 0;
