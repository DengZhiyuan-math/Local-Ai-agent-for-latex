// Public search-mode controls: a mouse click must reveal the chosen search form.
// Usage: node search_modes.mjs <editor URL> [Chrome debugging URL]
import assert from "node:assert/strict";
import { closeSocket } from "./close.mjs";

const [url, debug = "http://127.0.0.1:9333"] = process.argv.slice(2);
const tab = await (await fetch(debug + "/json/new?" + encodeURIComponent(url), { method: "PUT" })).json();
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
async function click(selector) {
  const rect = await evaluate(`(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
    return {x: r.x + r.width / 2, y: r.y + r.height / 2, width: r.width, height: r.height}; })()`);
  assert.ok(rect.width > 0 && rect.height > 0, `${selector} is visible`);
  await send("Input.dispatchMouseEvent", { type: "mousePressed", button: "left", clickCount: 1, x: rect.x, y: rect.y });
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", button: "left", clickCount: 1, x: rect.x, y: rect.y });
  await sleep(100);
}
try {
  await send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 900, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url });
  for (let i = 0; i < 100; i++) {
    if (await evaluate("typeof S !== 'undefined' && S.ready")) break;
    await sleep(100);
  }
  await evaluate("cm.focus()");
  const modifiers = await evaluate('navigator.platform.includes("Mac") ? 4 : 2');
  await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "f", code: "KeyF", modifiers, windowsVirtualKeyCode: 70, nativeVirtualKeyCode: 70 });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "f", code: "KeyF", modifiers, windowsVirtualKeyCode: 70, nativeVirtualKeyCode: 70 });
  await sleep(100);
  assert.equal(await evaluate('document.querySelector("#find").hidden'), false, "Ctrl+F opens file search");
  assert.ok(await evaluate('Array.from(document.querySelectorAll("#find button")).some(button => /agent/i.test(button.textContent))'),
    "Ctrl+F search exposes an agentic-search entry");
  await evaluate('document.querySelector("#find-q").value = "keep this query"');
  await click("#find-agent");
  assert.equal(await evaluate('document.querySelector("#find").hidden'), true, "file-search overlay closes");
  assert.equal(await evaluate('document.querySelector("#search-pane").hidden'), false, "project-search sidebar opens");
  assert.equal(await evaluate('document.querySelector("#ps-agent").hidden'), false, "agentic mode is explicitly selected");
  assert.equal(await evaluate('document.activeElement.id'), "as-q", "the request input receives focus");
  assert.equal(await evaluate('document.querySelector("#find-q").value'), "keep this query", "the file query is preserved");
  assert.equal(await evaluate('AS.job'), null, "switching modes does not submit an agent request");
  await click('[data-psmode="agent"]');
  assert.equal(await evaluate('document.querySelector(\'[data-psmode="agent"]\').getAttribute("aria-selected")'), "true",
    "clicking With the agent selects agentic search");
  assert.ok(await evaluate('(() => { const r = document.querySelector("#as-q").getBoundingClientRect(); return r.width > 0 && r.height > 0; })()'),
    "agentic search input is visible after switching modes");
  assert.equal(await evaluate('document.querySelector("#ps-find").hidden'), true);
  await click('[data-psmode="find"]');
  assert.equal(await evaluate('document.querySelector("#ps-agent").hidden'), true);
  assert.equal(await evaluate('document.querySelector("#ps-find").hidden'), false);
  await click("#ps-close");
  await click("#btn-search");
  assert.equal(await evaluate('document.querySelector("#ps-find").hidden'), false, "normal search remains usable and remembers its mode");
  console.log("PASS switching search modes with real mouse clicks reveals the correct form");
} finally {
  await closeSocket(ws);
  await fetch(debug + "/json/close/" + tab.id);
}
