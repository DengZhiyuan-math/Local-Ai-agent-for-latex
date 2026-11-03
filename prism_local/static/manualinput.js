/* Evidence for the editor's own input path, private to this page. A capability is
   never kept in a project file, a URL or the agent prompt. Uncertain or mixed
   programmatic changes remain unverified; they still save normally. */
"use strict";

const ManualInput = (() => {
  function createTracker() {
    const docs = new WeakMap();
    let dispatch = null;
    let deriving = null;
    const editingOrigins = new Set(["+input", "+delete", "paste", "cut"]);
    const derivedOrigins = new Set(["+snippet", "+input"]);
    const fingerprint = (change) => change && JSON.stringify({
      from: change.from, to: change.to, text: change.text, origin: change.origin,
    });

    function state(doc) {
      return docs.get(doc);
    }
    function changed(doc, change = {}) {
      const s = state(doc);
      if (!s) return;
      const content = doc.getValue();
      if (content === s.last) return;
      const forbidden = !editingOrigins.has(change.origin);
      const exact = s.pending?.confirmed && s.pending.before === s.last && s.pending.after === content;
      const synchronous = dispatch && dispatch.doc === doc
        && dispatch.event.isTrusted && dispatch.event.eventPhase > 0;
      const derived = derivedOrigins.has(change.origin) && ((deriving && deriving.doc === doc)
        || (s.derived && s.derived.before === s.last && s.derived.after === content));
      const human = derived || !forbidden && !!(exact || synchronous);
      s.events.push({ revision: ++s.revision, human });
      s.inputProof = human && !derived && change.origin === "+input"
        ? { revision: s.revision, fingerprint: fingerprint(change), after: content } : null;
      s.last = content;
      s.pending = null;
      s.derived = null;
    }
    function track(doc, capability = null) {
      if (!state(doc)) {
        docs.set(doc, { capability, revision: 0, events: [], last: doc.getValue(), pending: null, inputProof: null, derived: null });
        if (typeof doc.on === "function") doc.on("change", changed);
      } else synced(doc, capability);
    }
    function synced(doc, capability = null) {
      const s = state(doc);
      if (!s) return track(doc, capability);
      s.capability = capability;
      s.events = [];
      s.pending = null;
      s.inputProof = null; s.derived = null;
      s.last = doc.getValue();
    }
    function remove(doc) {
      if (typeof doc.off === "function") doc.off("change", changed);
      docs.delete(doc);
    }
    function freeze(doc, force = false) {
      const s = state(doc);
      if (!s) return { revision: 0 };
      return { revision: s.revision,
        token: !force && s.events.length && s.events.every((e) => e.human)
          ? s.capability?.token : undefined };
    }
    function saved(doc, snapshot, capability = null) {
      const s = state(doc);
      if (!s) return;
      s.events = s.events.filter((e) => e.revision > snapshot.revision);
      s.capability = capability;
      if (s.inputProof?.revision <= snapshot.revision) s.inputProof = null;
      // Any trusted input still waiting for CodeMirror is part of a later save.
    }
    function controlledDerived(doc, change, run) {
      const s = state(doc), proof = s?.inputProof;
      const nativeTab = !change && dispatch?.doc === doc && dispatch.event.isTrusted
        && dispatch.event.eventPhase > 0 && dispatch.event.key === "Tab";
      const exactInput = proof && proof.revision === s.revision && proof.after === doc.getValue()
        && proof.fingerprint === fingerprint(change);
      if (!s || s.last !== doc.getValue() || (!nativeTab && !exactInput)
          || s.events.some((event) => !event.human)) return run();
      s.inputProof = null;             // this input can authorize only this controlled expansion
      const before = doc.getValue(), previous = deriving;
      deriving = { doc };
      let complete = false;
      try { const result = run(); complete = true; return result; }
      finally {
        deriving = previous;
        const after = doc.getValue();
        // CodeMirror can queue Doc change callbacks until the outer operation ends.
        // Bind that deferred notification to the exact result we just generated.
        if (complete && after !== before && s.last === before) s.derived = { before, after };
        else if (!complete && after !== before) {
          s.events.push({ revision: ++s.revision, human: false }); s.derived = null;
        }
      }
    }
    function expected(cm, text) {
      const doc = cm.getDoc(), s = state(doc);
      if (!s || typeof text !== "string") return;
      const before = doc.getValue();
      const from = doc.indexFromPos(doc.getCursor("from"));
      const to = doc.indexFromPos(doc.getCursor("to"));
      if (s.pending && s.pending.before === before && s.pending.from === from && s.pending.to === to) {
        text = s.pending.inserted + text;
      }
      s.pending = { before, from, to, inserted: text, after: before.slice(0, from) + text + before.slice(to), confirmed: false };
    }
    function attachEditor(cm) {
      const input = cm.getInputField(), wrapper = cm.getWrapperElement();
      const native = (event) => {
        const doc = cm.getDoc(), s = state(doc);
        if (!s) return;
        if (!event.isTrusted) { s.pending = null; return; }
        if (event.type === "beforeinput") {
          if (typeof event.data === "string") expected(cm, event.data.replace(/\r\n?/g, "\n"));
          return;
        }
        if (event.type === "input") {
          // Chrome may deliver null data for a multiline insertion, then emit
          // nested input events after CodeMirror has already applied it. Only
          // confirm the matching beforeinput/paste claim; never create a late one.
          if (s.pending && s.pending.before === doc.getValue()) s.pending.confirmed = true;
          return;
        }
        if (event.type === "paste") {
          const text = event.clipboardData?.getData("text/plain");
          if (typeof text === "string") expected(cm, text.replace(/\r\n?/g, "\n"));
        }
        const manualKey = (event.type === "keydown" || event.type === "keypress")
          && !event.ctrlKey && !event.metaKey && !event.altKey
          && (event.key?.length === 1 || ["Backspace", "Delete", "Enter", "Tab"].includes(event.key));
        if (!manualKey && event.type !== "paste" && event.type !== "cut") return;
        // eventPhase returns to NONE after dispatch. It also remains valid across
        // browser microtask checkpoints between capture and CodeMirror listeners.
        dispatch = { doc, event };
      };
      for (const kind of ["keydown", "keypress", "paste", "cut"]) wrapper.addEventListener(kind, native, true);
      input.addEventListener("beforeinput", native, true);
      input.addEventListener("input", native, true);
      return () => {
        for (const kind of ["keydown", "keypress", "paste", "cut"]) wrapper.removeEventListener(kind, native, true);
        input.removeEventListener("beforeinput", native, true);
        input.removeEventListener("input", native, true);
      };
    }
    return { track, synced, remove, freeze, saved, changed, controlledDerived, attachEditor };
  }
  return { createTracker };
})();

if (typeof module !== "undefined") module.exports = ManualInput;
