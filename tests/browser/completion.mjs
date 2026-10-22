// Real CodeMirror keys: command hints, automatic braces, environment hints and undo.
// Usage: node completion.mjs <editor URL> (headless Chrome debugging on port 9333).
import assert from "node:assert/strict";
import { closeSocket } from "./close.mjs";

const [url] = process.argv.slice(2);
const tab = await (await fetch("http://127.0.0.1:9333/json/new?" + encodeURIComponent(url), { method: "PUT" })).json();
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((resolve) => ws.addEventListener("open", resolve));
let id = 0;
const pending = new Map();
ws.addEventListener("message", (message) => {
  const data = JSON.parse(message.data);
  if (data.id && pending.has(data.id)) { pending.get(data.id)(data); pending.delete(data.id); }
});
const send = (method, params = {}) => new Promise((resolve) => {
  const next = ++id; pending.set(next, resolve); ws.send(JSON.stringify({ id: next, method, params }));
});
const evaluate = async (expression) => {
  const result = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (result.result.exceptionDetails) throw new Error(result.result.exceptionDetails.exception?.description || "page evaluation failed");
  return result.result.result.value;
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function key(key, code, text = "", modifiers = 0) {
  const virtualKey = { Enter: 13, Escape: 27, Tab: 9, "{": 219 }[key];
  const params = { key, code, modifiers, windowsVirtualKeyCode: virtualKey, nativeVirtualKeyCode: virtualKey };
  await send("Input.dispatchKeyEvent", { type: "keyDown", text, ...params });
  await send("Input.dispatchKeyEvent", { type: "keyUp", ...params });
  await sleep(60);
}
async function begin() {
  await evaluate('cm.closeHint(); cm.setValue(""); cm.clearHistory(); cm.focus();');
  await send("Input.insertText", { text: "\\be" });
  await sleep(80);
  assert.ok(await evaluate("!!cm.state.completionActive"), "command hints open while typing begin");
  await send("Input.insertText", { text: "gin" });
  await key("{", "BracketLeft", "{", 8);
  assert.equal(await evaluate("cm.getValue()"), "\\begin{}", "autoCloseBrackets handled the brace");
  assert.ok(await evaluate("Array.from(document.querySelectorAll('.CodeMirror-hint')).some(el => el.textContent.includes('align'))"),
    "environment candidates replace command hints after the automatic brace");
}
try {
  await send("Page.enable");
  await send("Page.navigate", { url });
  for (let i = 0; i < 100; i++) {
    if (await evaluate("typeof S !== 'undefined' && S.ready && !!S.active")) break;
    await sleep(100);
  }
  await begin();
  await send("Input.insertText", { text: "align" });
  await sleep(80);
  assert.ok(await evaluate("!!cm.state.completionActive"), "environment hints remain open after typing a full name");
  await key("Enter", "Enter", "\r");
  assert.equal(await evaluate("cm.getValue()"), "\\begin{align}\n  \n\\end{align}");
  assert.deepEqual(await evaluate("[cm.getCursor().line, cm.getCursor().ch]"), [1, 2]);
  await evaluate("cm.undo()");
  assert.equal(await evaluate("cm.getValue()"), "\\begin{align}", "completion is one undo step");
  await begin();
  await key("Escape", "Escape");
  assert.equal(await evaluate("cm.getValue()"), "\\begin{}", "cancel does not insert a template");
  await evaluate('cm.closeHint(); cm.setValue("\\\\begin{ali}\\n\\\\end{align}"); cm.setCursor({line: 0, ch: 10}); cm.focus();');
  await sleep(80);
  await key("Tab", "Tab");
  assert.equal(await evaluate("cm.getValue()"), "\\begin{align}\n  \n\\end{align}", "reuse creates an indented body without a duplicate end");
  console.log("PASS automatic brace popup, environment acceptance, body cursor, undo and cancellation");
} finally {
  await closeSocket(ws);
  await fetch("http://127.0.0.1:9333/json/close/" + tab.id);
}
