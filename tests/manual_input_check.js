"use strict";
const assert = require("assert");
const fs = require("fs"), vm = require("vm");
const ManualInput = require("../prism_local/static/manualinput.js");
const Tabs = require("../prism_local/static/tabs.js");

class Doc {
  constructor(text = "") { this.text = text; this.version = 0; this.handlers = []; this.cursor = text.length; }
  getValue() { return this.text; }
  changeGeneration() { return this.version; }
  isClean(version) { return this.version === version; }
  on(_event, fn) { this.handlers.push(fn); }
  off(_event, fn) { this.handlers = this.handlers.filter((f) => f !== fn); }
  indexFromPos(pos) { return pos.ch; }
  getCursor() { return { line: 0, ch: this.cursor }; }
  edit(text, origin = "+input") {
    const change = { origin, from: { line: 0, ch: 0 }, to: { line: 0, ch: this.text.length }, text: text.split("\n") };
    this.text = text; this.cursor = text.length; this.version++;
    for (const fn of this.handlers) fn(this, change);
    return change;
  }
}
class Target {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, fn) { this.listeners.set(type, fn); }
  removeEventListener(type) { this.listeners.delete(type); }
  event(type, details = {}, during = () => {}) {
    const event = { type, ...details, eventPhase: 1 };
    this.listeners.get(type)?.(event); event.eventPhase = 2;
    during(); event.eventPhase = 0;
  }
}
function setup(text = "") {
  const doc = new Doc(text), tracker = ManualInput.createTracker(), input = new Target(), wrapper = new Target();
  tracker.track(doc, { token: "read-token", before_sha256: "version-a" });
  tracker.attachEditor({ getDoc: () => doc, getInputField: () => input, getWrapperElement: () => wrapper });
  const type = async (text) => {
    input.event("beforeinput", { isTrusted: true, data: text, inputType: "insertText" });
    input.event("input", { isTrusted: true, data: text, inputType: "insertText" });
    await Promise.resolve();
    return doc.edit(doc.getValue() + text);
  };
  return { doc, tracker, input, wrapper, type };
}

async function inputEvidence() {
  {
    const { doc, tracker } = setup();
    doc.edit("programmatic");
    assert.equal(tracker.freeze(doc).token, undefined, "origin +input does not prove human input");
  }
  {
    const { doc, tracker, wrapper } = setup();
    wrapper.event("keypress", { isTrusted: false, key: "a" });
    doc.edit("a");
    assert.equal(tracker.freeze(doc).token, undefined, "synthetic events do not supply evidence");
  }
  {
    const { doc, tracker, type } = setup();
    await type("x");
    assert.equal(tracker.freeze(doc).token, "read-token", "exact deferred input is proven");
    assert.equal(JSON.stringify(doc).includes("read-token"), false, "capabilities stay out of document state");
  }
  {
    const { doc, tracker, input } = setup();
    input.event("beforeinput", { isTrusted: true, data: "a" }); input.event("input", { isTrusted: true, data: "a" });
    input.event("beforeinput", { isTrusted: true, data: "b" }); input.event("input", { isTrusted: true, data: "b" });
    await Promise.resolve(); doc.edit("ab");
    assert.equal(tracker.freeze(doc).token, "read-token", "coalesced real input remains exact");
  }
  {
    const { doc, tracker, input } = setup();
    input.event("beforeinput", { isTrusted: true, data: "a" }); input.event("input", { isTrusted: true, data: "a" });
    await Promise.resolve(); doc.edit("different");
    assert.equal(tracker.freeze(doc).token, undefined, "a stale claim cannot cover different text");
  }
  {
    const { doc, tracker, wrapper } = setup("ab");
    wrapper.event("keydown", { isTrusted: true, key: "Backspace" }, () => doc.edit("a", "+delete"));
    assert.equal(tracker.freeze(doc).token, "read-token", "native deletion is captured during dispatch");
    await Promise.resolve(); doc.edit("", "+delete");
    assert.equal(tracker.freeze(doc).token, undefined, "dispatch evidence ends at the event boundary");
  }
  for (const origin of ["+find", "setValue", "undo", "redo"]) {
    const { doc, tracker, wrapper } = setup();
    wrapper.event("keydown", { isTrusted: true, key: "Enter" }, () => doc.edit("x", origin));
    assert.equal(tracker.freeze(doc).token, undefined, origin + " never inherits a trusted click/key");
  }
  {
    const { doc, tracker, wrapper, input } = setup();
    wrapper.event("paste", { isTrusted: true, clipboardData: { getData: () => "x\r\ny" } });
    input.event("input", { isTrusted: true, data: null });
    await Promise.resolve(); doc.edit("x\ny", "paste");
    assert.equal(tracker.freeze(doc).token, "read-token", "exact trusted paste is captured after polling");
  }
  {
    const { doc, tracker, input } = setup();
    input.event("beforeinput", { isTrusted: true, data: "x" });
    await Promise.resolve(); doc.edit("x");
    assert.equal(tracker.freeze(doc).token, undefined, "cancelled beforeinput is not evidence of a completed input");
  }
  {
    const { doc, tracker, input, type } = setup();
    await type("x");
    tracker.saved(doc, tracker.freeze(doc), { token: "next" });
    input.event("input", { isTrusted: true, data: "x" });
    doc.edit("xx");
    assert.equal(tracker.freeze(doc).token, undefined, "late nested input cannot authorize a later programmatic match");
  }
  {
    const { doc, tracker, type } = setup();
    doc.edit("AI", "+find"); await type("x");
    assert.equal(tracker.freeze(doc).token, undefined, "mixed programmatic and human changes stay unknown");
    tracker.saved(doc, tracker.freeze(doc), { token: "saved-token" }); await type("y");
    assert.equal(tracker.freeze(doc).token, "saved-token", "a later pure human save has its own evidence");
    assert.equal(tracker.freeze(doc, true).token, undefined, "force does not reuse a capability");
    tracker.synced(doc, { token: "reload-token" });
    assert.equal(tracker.freeze(doc).token, undefined, "a reload is not human input");
    await type("z"); assert.equal(tracker.freeze(doc).token, "reload-token");
    tracker.remove(doc); assert.equal(tracker.freeze(doc).token, undefined);
    assert.equal(doc.handlers.length, 0, "closing a tab removes its provenance listener");
  }
}

async function saveSnapshots() {
  const { doc, tracker, type } = setup();
  const requests = [], replies = [];
  const store = Tabs.createTabStore({ manualInput: tracker,
    saveFile: (body) => { requests.push(body); return new Promise((resolve) => replies.push(resolve)); } });
  const tab = store.add("main.tex", doc, 1, { token: "read-token" });
  await type("x");
  const first = store.save(tab); await new Promise((r) => setImmediate(r));
  await type("y");
  assert.equal(requests[0].content, "x", "the request holds the frozen content");
  replies.shift()({ _status: 200, mtime: 2, manual_edit: { token: "second-token" } }); await first;
  assert.equal(tracker.freeze(doc).token, "second-token", "response keeps later trusted edits");
  const second = store.save(tab); await new Promise((r) => setImmediate(r));
  assert.equal(requests[1].content, "xy"); assert.equal(requests[1].manual_edit_token, "second-token");
  replies.shift()({ _status: 0, error: "offline" }); await second;
  assert.equal(tracker.freeze(doc).token, "second-token", "failed save preserves its evidence");
  const retry = store.save(tab); await new Promise((r) => setImmediate(r));
  assert.equal(requests[2].manual_edit_token, "second-token");
  doc.edit("xyz", "+find");
  replies.shift()({ _status: 200, mtime: 3, manual_edit: { token: "third-token" } }); await retry;
  assert.equal(tracker.freeze(doc).token, undefined, "response does not erase a later untrusted edit");
  const forced = store.save(tab, true); await new Promise((r) => setImmediate(r));
  assert.equal(requests[3].manual_edit_token, undefined, "forced saves carry no old evidence");
  replies.shift()({ _status: 200, mtime: 4, manual_edit: { token: "force-token" } }); await forced;
  store.synced(tab, 5, { token: "sync-token" });
  assert.equal(tracker.freeze(doc).token, undefined);
}

async function controlledSnippets() {
  {
    const { doc, tracker, type } = setup();
    await type("@a"); doc.edit("\\alpha", "+snippet");
    assert.equal(tracker.freeze(doc).token, undefined, "a +snippet origin alone cannot inherit prior input");
  }
  {
    const { doc, tracker, type } = setup();
    const change = await type("@a");
    tracker.controlledDerived(doc, { ...change, text: ["forged"] }, () => doc.edit("\\alpha", "+snippet"));
    assert.equal(tracker.freeze(doc).token, undefined, "a different triggering change cannot authorize expansion");
  }
  {
    const { doc, tracker, type } = setup();
    const change = await type("@a");
    tracker.controlledDerived(doc, change, () => doc.edit("\\alpha", "+snippet"));
    assert.equal(tracker.freeze(doc).token, "read-token", "the controlled expansion inherits its exact trusted input");
    tracker.controlledDerived(doc, change, () => doc.edit("\\beta", "+snippet"));
    assert.equal(tracker.freeze(doc).token, undefined, "the consumed input cannot authorize another expansion");
  }
  {
    const { doc, tracker } = setup();
    const change = doc.edit("@a", "+input");
    tracker.controlledDerived(doc, change, () => doc.edit("\\alpha", "+snippet"));
    assert.equal(tracker.freeze(doc).token, undefined, "programmatic input cannot authorize controlled expansion");
  }
  {
    const { doc, tracker, type } = setup();
    doc.edit("AI", "+find"); const change = await type("@a");
    tracker.controlledDerived(doc, change, () => doc.edit("AI\\alpha", "+snippet"));
    assert.equal(tracker.freeze(doc).token, undefined, "mixed-origin state cannot acquire snippet proof");
  }
  {
    const { doc, tracker, type } = setup();
    const change = await type("@r");
    let delayed;
    tracker.controlledDerived(doc, change, () => {
      const listeners = doc.handlers; doc.handlers = [];
      const derived = doc.edit("\\rho", "+snippet"); doc.handlers = listeners;
      delayed = () => listeners.forEach((fn) => fn(doc, derived));
    });
    delayed();
    assert.equal(tracker.freeze(doc).token, "read-token", "deferred Doc events match the exact controlled result");
  }
  {
    const { doc, tracker, wrapper } = setup("@a");
    wrapper.event("keydown", { isTrusted: true, key: "Tab" }, () => {
      tracker.controlledDerived(doc, null, () => doc.edit("\\alpha", "+snippet"));
    });
    assert.equal(tracker.freeze(doc).token, "read-token", "explicit native Tab may expand its controlled snippet");
  }
  {
    const { doc, tracker } = setup("@a");
    tracker.controlledDerived(doc, null, () => doc.edit("\\alpha", "+snippet"));
    assert.equal(tracker.freeze(doc).token, undefined, "a programmatic Tab call supplies no evidence");
  }
  {
    const { doc, tracker, wrapper } = setup("@a");
    const handlers = doc.handlers; doc.handlers = []; doc.edit("@r", "+find"); doc.handlers = handlers;
    wrapper.event("keydown", { isTrusted: true, key: "Tab" }, () => {
      tracker.controlledDerived(doc, null, () => doc.edit("\\rho", "+snippet"));
    });
    assert.equal(tracker.freeze(doc).token, undefined, "native Tab cannot cover an unobserved changed document");
  }
}

async function backgroundReports() {
  const source = fs.readFileSync(require.resolve("../prism_local/static/app.js"), "utf8");
  const slice = (from, to) => source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from)));
  let respond, dirty = false;
  const rendered = [], notifications = [], elements = {};
  const old = { initialized: true, terms: [], symbols: [] };
  const RESEARCH = { state: old, rendered: old, request: 0, busy: false, seen: new Set() };
  const context = {
    RESEARCH, Q: { research: () => new Promise((resolve) => { respond = resolve; }) },
    $: (id) => elements[id] ||= { setAttribute() {}, querySelectorAll: () => [] },
    bindProject: () => true, researchMessage: () => {}, toast: (m) => notifications.push(m),
    researchOpen: () => {}, researchCorrectionsDirty: () => dirty,
    renderResearch: (s) => { RESEARCH.state = s; RESEARCH.rendered = s; rendered.push(s); },
  };
  const run = vm.runInNewContext(slice("function researchBusy(", "\nfunction researchLocations(")
    + slice("function researchSummary(", "\nfunction researchOpen(")
    + slice("async function loadResearch(", '\n$("#research-toggle").onclick')
    + ";({loadResearch,researchBusy})", context);
  const reading = run.loadResearch();
  const answer = respond;
  run.researchBusy(true);
  const explicit = { ...old, question: "Explicitly updated" };
  RESEARCH.state = explicit; run.researchBusy(false);
  answer(old); await reading;
  assert.equal(RESEARCH.state, explicit, "an older maintenance read cannot replace an explicit update");
  dirty = true;
  const refresh = run.loadResearch();
  const latest = { ...old, unverified_symbols: [{ notation: "q" }], maintenance_notices: [{ id: "one", message: "New unverified q" }] };
  respond(latest); await refresh;
  assert.equal(rendered.length, 0, "background maintenance preserves unsaved report fields");
  assert.equal(RESEARCH.state, latest);
  assert.ok(elements["#research-summary"].textContent.includes("unsaved corrections"));
  const repeat = run.loadResearch(); respond(latest); await repeat;
  assert.equal(notifications.length, 1, "unchanged notices are not announced again");
}

(async () => {
  await inputEvidence(); await saveSnapshots(); await controlledSnippets(); await backgroundReports();
  console.log("PASS manual input evidence, mixed origins, snapshots, failures, force and reload");
})().catch((error) => { console.error(error); process.exitCode = 1; });
