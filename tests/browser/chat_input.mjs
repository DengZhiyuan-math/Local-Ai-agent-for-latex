import { closeSocket } from "./close.mjs";
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
const url = process.argv[2];
let id = 0; const pending = new Map(), errors = [], results = [];
let ws, tab, chrome, session, onPageLoad;
const pageLoaded = new Promise((resolve) => { onPageLoad = resolve; });
const receive = (d) => {
  if (d.method === "Page.loadEventFired") onPageLoad();
  if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
  if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
};
if (process.env.PRISM_TEST_CHROME_PROFILE) {
  chrome = spawn("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    ["--headless=new", "--remote-debugging-pipe", "--user-data-dir=" + process.env.PRISM_TEST_CHROME_PROFILE, "--no-first-run", "about:blank"],
    { stdio: ["ignore", "ignore", "ignore", "pipe", "pipe"] });
  let buffer = "";
  chrome.stdio[4].on("data", (data) => {
    buffer += data.toString(); let end;
    while ((end = buffer.indexOf("\0")) >= 0) { receive(JSON.parse(buffer.slice(0, end))); buffer = buffer.slice(end + 1); }
  });
} else {
  tab = await (await fetch("http://127.0.0.1:9333/json/new?about:blank", { method: "PUT" })).json();
  ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  ws.addEventListener("message", (m) => receive(JSON.parse(m.data)));
}
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const i = ++id; pending.set(i, (d) => d.error
    ? reject(new Error(`${method}: ${d.error.message}`)) : resolve(d));
  const msg = JSON.stringify({ id: i, method, params, ...(session ? { sessionId: session } : {}) });
  if (chrome) chrome.stdio[3].write(msg + "\0"); else ws.send(msg);
});
try {
if (chrome) {
  const target = await send("Target.createTarget", { url: "about:blank" });
  const attached = await send("Target.attachToTarget", { targetId: target.result.targetId, flatten: true });
  session = attached.result.sessionId;
}
const ev = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || JSON.stringify(r.result.exceptionDetails));
  return r.result.result.value;
};
const check = (name, ok, detail = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);
await send("Runtime.enable");
await send("Page.enable");
await send("Page.navigate", { url });
let loadTimer;
try {
  await Promise.race([pageLoaded, new Promise((_, reject) => {
    loadTimer = setTimeout(() => reject(new Error("The editor page did not load")), 15000);
  })]);
} finally { clearTimeout(loadTimer); }
await ev(`for (let i = 0; i < 100 && (typeof snipContent === 'undefined' || !S.ready); i++) await new Promise(r => setTimeout(r, 100));
  chatHidden(false); chatEditor.setValue(''); chatEditor.focus(); return true`);
for (const ch of "mk") { await send("Input.insertText", { text: ch }); await new Promise(r => setTimeout(r, 80)); }
const mathStart = await ev(`return { text: chatEditor.getValue(), cursor: chatEditor.indexFromPos(chatEditor.getCursor()) }`);
check("mk expands in the chat editor", mathStart.text === "$$" && mathStart.cursor === 1, JSON.stringify(mathStart));
for (const ch of "@a") { await send("Input.insertText", { text: ch }); await new Promise(r => setTimeout(r, 80)); }
await new Promise(r => setTimeout(r, 300));
const typingFormula = await ev(`return { text: chatEditor.getValue(), rendered: !!$('#chat-input-box .chat-math-widget') }`);
check("editing a formula shows its TeX source", typingFormula.text === "$\\alpha$" && !typingFormula.rendered, JSON.stringify(typingFormula));
await ev(`chatEditor.setCursor(chatEditor.posFromIndex(chatEditor.getValue().length)); await new Promise(r => setTimeout(r, 200)); return true`);
const formula = await ev(`return { text: chatEditor.getValue(), rendered: !!$('#chat-input-box .chat-math-widget .katex') }`);
check("leaving a formula renders it in the input", formula.text === "$\\alpha$" && formula.rendered, JSON.stringify(formula));
const widget = await ev(`const r = $('#chat-input-box .chat-math-widget').getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }`);
await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: widget.x, y: widget.y });
await send("Input.dispatchMouseEvent", { type: "mousePressed", button: "left", buttons: 1, clickCount: 1, x: widget.x, y: widget.y });
await send("Input.dispatchMouseEvent", { type: "mouseReleased", button: "left", buttons: 0, clickCount: 1, x: widget.x, y: widget.y });
await new Promise(r => setTimeout(r, 200));
check("clicking the rendered formula opens its source for editing", await ev(`const i = chatEditor.indexFromPos(chatEditor.getCursor()); return !$('#chat-input-box .chat-math-widget') && chatEditor.hasFocus() && i > 0 && i < chatEditor.getValue().length`));
await send("Input.insertText", { text: "y" });
await new Promise(r => setTimeout(r, 150));
check("typing edits the original TeX", await ev(`return chatEditor.getValue() === '$y\\\\alpha$' && !$('#chat-input-box .chat-math-widget')`));
await ev(`chatEditor.setValue('A $x$ B'); chatEditor.setCursor(chatEditor.posFromIndex(5)); renderChatMath(); return true`);
await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37, nativeVirtualKeyCode: 37 });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37, nativeVirtualKeyCode: 37 });
await new Promise(r => setTimeout(r, 150));
const arrow = await ev(`return { cursor: chatEditor.indexFromPos(chatEditor.getCursor()), rendered: !!$('#chat-input-box .chat-math-widget'), text: chatEditor.getValue() }`);
check("arrow key enters the formula source", arrow.cursor > 2 && arrow.cursor < 5 && !arrow.rendered && arrow.text === 'A $x$ B', JSON.stringify(arrow));
const mixedText = "code `$x$` and $x$ plus $x$";
await ev(`chatEditor.setValue(${JSON.stringify(mixedText)}); chatEditor.setCursor(chatEditor.posFromIndex(chatEditor.getValue().length)); renderChatMath(); return true`);
check("repeated formulas render in place without rendering code", await ev(`return $('#chat-input-box').querySelectorAll('.chat-math-widget').length === 2`));
if (process.env.PRISM_TEST_SCREENSHOT) {
  const screenshot = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(process.env.PRISM_TEST_SCREENSHOT, Buffer.from(screenshot.result.data, "base64"));
}
const firstWidget = await ev(`const r = $('#chat-input-box .chat-math-widget').getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }`);
await send("Input.dispatchMouseEvent", { type: "mousePressed", button: "left", buttons: 1, clickCount: 1, x: firstWidget.x, y: firstWidget.y });
await send("Input.dispatchMouseEvent", { type: "mouseReleased", button: "left", buttons: 0, clickCount: 1, x: firstWidget.x, y: firstWidget.y });
await new Promise(r => setTimeout(r, 150));
check("editing one formula leaves the other rendered", await ev(`return $('#chat-input-box').querySelectorAll('.chat-math-widget').length === 1 && chatEditor.indexFromPos(chatEditor.getCursor()) > 15 && chatEditor.indexFromPos(chatEditor.getCursor()) < 18`));
await ev(`chatEditor.setSelection(chatEditor.posFromIndex(0), chatEditor.posFromIndex(chatEditor.getValue().length)); await new Promise(r => setTimeout(r, 150)); return true`);
check("selecting text preserves the original formula source", await ev(`return chatEditor.getSelection() === ${JSON.stringify(mixedText)} && !$('#chat-input-box .chat-math-widget')`));
await ev(`chatEditor.replaceSelection('replacement', 'end', '+input'); chatEditor.undo(); await new Promise(r => setTimeout(r, 150)); return true`);
check("undo restores the original text and formulas", await ev(`return chatEditor.getValue() === ${JSON.stringify(mixedText)}`));
await ev(`chatEditor.setValue('$x$'); chatEditor.setCursor(chatEditor.posFromIndex(3)); renderChatMath(); chatEditor.focus(); return true`);
await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8, nativeVirtualKeyCode: 8 });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8, nativeVirtualKeyCode: 8 });
await new Promise(r => setTimeout(r, 150));
const afterBackspace = await ev(`return chatEditor.getValue()`);
check("backspace does not delete a whole rendered formula", afterBackspace === "$x" || afterBackspace === "$x$", JSON.stringify(afterBackspace));
const displayMath = "before \\[\n a+b \n\\] after";
await ev(`chatEditor.setValue(${JSON.stringify(displayMath)}); chatEditor.setCursor(chatEditor.posFromIndex(chatEditor.getValue().length)); renderChatMath(); return true`);
check("multiline display math renders without changing its source", await ev(`return !!$('#chat-input-box .chat-math-widget .katex-display') && chatEditor.getValue().includes('a+b') && chatEditor.getValue().includes('\\n')`));
await ev(`chatEditor.setValue('$par'); chatEditor.setCursor({line:0,ch:4}); CHAT_SNIP.tab(() => {}); return true`);
const fraction = await ev(`return chatEditor.getValue()`);
check("Tab snippets use the shared engine", fraction.includes("\\frac"), JSON.stringify(fraction));
await ev(`chatEditor.setValue('$'); chatEditor.setCursor({line:0,ch:1}); chatEditor.focus(); return true`);
for (const ch of "//1") { await send("Input.insertText", { text: ch }); await new Promise(r => setTimeout(r, 60)); }
await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9, nativeVirtualKeyCode: 9 });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9, nativeVirtualKeyCode: 9 });
await send("Input.insertText", { text: "2" });
await new Promise(r => setTimeout(r, 100));
const fields = await ev(`return chatEditor.getValue()`);
check("Tab moves between fraction fields", /\\frac\{1\}\{2\}/.test(fields), JSON.stringify(fields));
await ev(`chatEditor.setValue('$x$'); S.symbols.katex_macros = { '\\\\xcustom': 'z' }; chatEditor.focus(); return true`);
await ev(`chatEditor.setValue('$\\\\xcustom$'); chatEditor.setCursor(chatEditor.posFromIndex(chatEditor.getValue().length)); renderChatMath(); return true`);
check("inline formula uses project macros", await ev(`return !!$('#chat-input-box .chat-math-widget .katex') && !$('#chat-input-box .katex-error')`));
await ev(`chatEditor.setValue('hello '); chatEditor.setCursor({line:0,ch:6}); insertMention('@sec/a.tex'); return true`);
check("inserting a mention keeps the draft", await ev(`return chatEditor.getValue() === 'hello @sec/a.tex '`));
await ev(`chatEditor.setValue('hello '); chatEditor.setCursor({line:0,ch:6}); chatEditor.focus(); return true`);
await send("Input.insertText", { text: "@" });
await new Promise(r => setTimeout(r, 100));
check("typing @ opens the mention picker", await ev(`return !$('#slash-menu').hidden && SM.kind === 'mention'`));
await ev(`chatEditor.setValue(''); chatEditor.focus(); return true`);
await send("Input.insertText", { text: "/" });
await new Promise(r => setTimeout(r, 100));
check("slash command picker opens", await ev(`return !$('#slash-menu').hidden && SM.kind === 'slash'`));
await ev(`$('#slash-menu').hidden = true; chatEditor.setValue('draft'); chatEditor.setCursor({line:0,ch:5}); chatEditor.focus(); window.savedChatSend = chatSend; window.sendCount = 0; chatSend = () => { window.sendCount++; }; return true`);
await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, modifiers: 8 });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, modifiers: 8 });
check("Shift+Enter inserts a newline without sending", await ev(`return chatEditor.getValue().includes('\\n') && window.sendCount === 0`));
await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
const sent = await ev(`return { count: window.sendCount, text: chatEditor.getValue(), menu: $('#slash-menu').hidden }`);
check("Enter sends without inserting another newline", sent.count === 1 && (sent.text.match(/\n/g) || []).length === 1, JSON.stringify(sent));
await ev(`chatSend = window.savedChatSend; return true`);
const resizeBefore = await ev(`const h = $('#chat-resize').getBoundingClientRect(); return {x: h.x + h.width / 2, y: h.y + h.height / 2, height: $('#chat-input-box').getBoundingClientRect().height}`);
await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: resizeBefore.x, y: resizeBefore.y });
await send("Input.dispatchMouseEvent", { type: "mousePressed", button: "left", buttons: 1, clickCount: 1, x: resizeBefore.x, y: resizeBefore.y });
await send("Input.dispatchMouseEvent", { type: "mouseMoved", button: "left", buttons: 1, x: resizeBefore.x, y: resizeBefore.y - 45 });
await send("Input.dispatchMouseEvent", { type: "mouseReleased", button: "left", buttons: 0, clickCount: 1, x: resizeBefore.x, y: resizeBefore.y - 45 });
check("input resize still works", await ev(`return $('#chat-input-box').getBoundingClientRect().height > ${resizeBefore.height} + 20`));
const delivery = await ev(`
  const realAgent = Q.agent, realEvents = Q.events; let payload;
  Q.agent = async (body) => { payload = body; return { _status: 200, job: 9876 }; };
  Q.events = async () => ({ _status: 200, done: true, events: [{ t: 'done', outcome: 'ok', exit: 0, changed: [] }] });
  chatEditor.setValue('Explain $x^2$'); await sendMessage();
  Q.agent = realAgent; Q.events = realEvents;
  return { prompt: payload && payload.prompt, draft: chatEditor.getValue() };
`);
check("sending preserves TeX and clears the draft", delivery.prompt?.includes('Explain $x^2$') && delivery.draft === '', JSON.stringify(delivery));
await ev(`chatEditor.setValue(''); renderChatMath(); return true`);
check("clearing the draft removes rendered formulas", await ev(`return !$('#chat-input-box .chat-math-widget') && !$('#chat-placeholder').hidden`));
check("no page exceptions", errors.length === 0, errors.join(" | "));
console.log(results.join("\n"));
process.exitCode = results.some(line => line.startsWith("FAIL")) ? 1 : 0;
} finally {
if (chrome) { chrome.kill(); } else {
  await closeSocket(ws);
  await fetch("http://127.0.0.1:9333/json/close/" + tab.id);
}
}
