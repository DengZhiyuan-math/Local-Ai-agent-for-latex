import { closeSocket } from "./close.mjs";
// The editor's basics on the real page, with real keys: find and replace in the open file, search the project.
// usage: node editing.mjs <editor url> <project folder>
import { readFileSync, writeFileSync } from "node:fs";
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
  // A step that throws is a failed check, and the rest still run.
  if (r.result.exceptionDetails) { check("a step ran", false, r.result.exceptionDetails.exception?.description || "evaluate failed"); return null; }
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
await send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 900, deviceScaleFactor: 1, mobile: false });   // the sidebar shows
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

/* ---------------------------------------------------------------- search the project */
writeFileSync(join(root, "sec", "s1.tex"), "needle one\nnothing\nneedle two\n");
writeFileSync(join(root, "sec", "s2.tex"), "a Needle\n");
await ev(`await T.until(async () => { await poll(); return S.files.some((f) => f.path === "sec/s2.tex"); });
  store.set("psearch.opts", {}); Object.assign(PS.opts, { caseSensitive: false, wholeWord: false, regex: false }); psOptsShown();
  cm.focus(); return true`);
const ps = () => ev(`return { open: !$("#search-pane").hidden, files: !$("#tree").offsetParent, focus: document.activeElement.id,
  summary: $("#ps-summary").textContent, hits: [...document.querySelectorAll("#ps-results .ps-hit")].map((li) => li.textContent) }`);
await key("f", 2 | 8);
let p = await ps();
check("Ctrl+Shift+F opens Search in the sidebar in place of the files", p.open && p.files && p.focus === "ps-q", JSON.stringify(p));
await type("needle");
await ev(`return T.until(() => $("#ps-summary").textContent.startsWith("3 results"))`);
p = await ps();
check("finds in files that are not open, grouped by file", p.summary === "3 results in 2 files"
  && p.hits.join("|") === "1needle one|3needle two|1a Needle", JSON.stringify(p));
await ev(`cm.replaceRange("needle ", { line: 0, ch: 0 }); return true`);
const typed = await ev(`return T.until(() => $("#ps-summary").textContent === "4 results in 3 files")`);
check("an open file is searched as it is in the editor, unsaved edits too", typed, (await ps()).summary);
await ev(`[...document.querySelectorAll("#ps-results .ps-hit")].find((li) => li.title === "sec/s2.tex:1")?.click(); return true`);
const went = await ev(`await T.until(() => S.active === "sec/s2.tex" && cm.getSelection() === "Needle"); return { active: S.active, sel: cm.getSelection() }`);
check("a click opens the file and selects the match", went.active === "sec/s2.tex" && went.sel === "Needle", JSON.stringify(went));
await ev(`$("#ps-results .ps-file[title='sec/s1.tex']")?.click(); return true`);
check("a file's results fold away and back", (await ps()).hits.length === 2);
await ev(`$("#ps-results .ps-file[title='sec/s1.tex']")?.click(); return true`);

// Replace all: the open files in their tabs (saved as typed), the others on disk.
await ev(`$("#ps-more").click(); return true`);
await type("pin");
await ev(`$("#ps-all").click(); return true`);
const replaced = await ev(`await T.until(() => $("#ps-summary").textContent === "No results");
  await T.until(() => !S.tabs.some(isDirty));
  return { s2: TABS.get("sec/s2.tex").doc.getValue(), find: TABS.get("find.tex").doc.getValue(), summary: $("#ps-summary").textContent }`);
const disk = (f) => readFileSync(join(root, ...f.split("/")), "utf8");
check("Replace all changes every file, open or not", replaced?.summary === "No results" && replaced.s2 === "a pin\n"
  && replaced.find.startsWith("pin alpha") && disk("sec/s1.tex") === "pin one\nnothing\npin two\n"
  && disk("sec/s2.tex") === "a pin\n" && disk("find.tex").startsWith("pin alpha"), JSON.stringify({ ...replaced, s1: disk("sec/s1.tex") }));
// A file changed on disk is searched again by itself.
writeFileSync(join(root, "sec", "s1.tex"), "needle again\n");
const again = await ev(`return T.until(() => $("#ps-summary").textContent === "1 result in 1 file", 8000)`);
check("a file changed on disk is searched again", again, (await ps()).summary);
await ev(`$("#ps-q").focus(); return true`);
await key("Escape");
p = await ps();
check("Esc closes Search: the files are back", !p.open && !p.files, JSON.stringify(p));
check("no page errors", errors.length === 0, errors.join(" | "));

console.log(results.join("\n"));
await closeSocket(ws);
await fetch("http://127.0.0.1:9333/json/close/" + tab.id);   // its page goes too: it holds connections
process.exitCode = results.some(line => line.startsWith("FAIL")) ? 1 : 0;
