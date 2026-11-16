// Exercise the resize handler, including pointer direction, bounds and persistence.
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const path = require("path");
const app = fs.readFileSync(path.join(__dirname, "../prism_local/static/app.js"), "utf8");
const start = app.indexOf("// Dragging the top edge grows the input upward");
const end = app.indexOf('document.addEventListener("keydown"', start);
assert(start >= 0 && end > start);
const handlers = {};
const input = { style: {}, getBoundingClientRect() { return { height: parseFloat(this.style.height) || 80 }; } };
const handle = { addEventListener(name, fn) { handlers[name] = fn; },
  removeEventListener(name) { delete handlers[name]; }, setPointerCapture() {}, setAttribute() {} };
const log = { getBoundingClientRect: () => ({ height: 400 }) };
const chat = { clientHeight: 600 };
const chatEditor = { refresh() {} };
const saved = {};
const store = { get: () => 0, set: (key, value) => { saved[key] = value; } };
vm.runInNewContext(app.slice(start, end), { store, chatEditor, $: (id) => ({
  "#chat-input-box": input, "#chat-resize": handle, "#chat-log": log, "#chat": chat,
})[id] });
handlers.pointerdown({ button: 0, clientY: 200, pointerId: 1, preventDefault() {} });
handlers.pointermove({ clientY: 150 });
assert.equal(input.style.height, "130px");
handlers.pointerup();
assert.equal(saved["chat.inputHeight"], 130);
assert.equal(handlers.pointermove, undefined);
handlers.keydown({ key: "ArrowDown", preventDefault() {} });
assert.equal(input.style.height, "110px");
handlers.pointerdown({ button: 0, clientY: 200, pointerId: 2, preventDefault() {} });
handlers.pointermove({ clientY: 10000 });
assert.equal(input.style.height, "48px");
handlers.pointercancel();
