/* What an agent's turn shows, decided from its events (backends.py lists them); app.js
   draws it. Plain: the page gives the clock, so the Node checks feed it recorded turns.

     const t = Turn.create({ now });   // now(): ms (Date.now)
     for (const s of t.feed(events)) … // what to draw for a batch of events, in order
     t.status                          // the status line: "thinking… 3s", "writing a.tex… (12 lines)"
     t.thinking()                      // seconds thought so far, or null when not thinking
     t.session, t.changed              // from init and done
     t.end()                           // the turn is over: what still has to close

   The steps:
     {draw: "thinking"}                     a thinking box opens
     {draw: "thinking-text", text}          more of what it thinks (where the model shows it)
     {draw: "thinking-end", secs, shown}    it closes; shown: false when no text came
     {draw: "message", id, text}            a reply's whole text so far (once per batch, or
                                            before the next step that is not more of it)
     {draw: "tool", id, name, summary}      a tool call's row, new or filled in
     {draw: "tool-result", id, ok, preview} it ended
     {draw: "live", id, name}               the box a file being written streams into
     {draw: "live-path", id, name, path}    which file that is
     {draw: "live-text", id, text}          more of its text
     {draw: "live-close", id}               it is complete
     {view: "begin", id, name, path} / {view: "feed", event} / {view: "end", id, ok}
                                            the agent's view of the file it edits (AV)
     {mark: "on", path} / {mark: "read", path, from, to} / {mark: "clear-read"}
                                            marks in the editor (AH); lines 0-based
     {save: "session", id}                  the conversation's session (init)
     {draw: "context" | "build" | "error" | "done", event}   as the event says

   One reply is one message: its streamed pieces (delta) are joined, and the whole text
   sent after them (text) is not drawn twice. */
"use strict";

const Turn = (() => {
  function create({ now = () => Date.now() } = {}) {
    let think = null;               // {t0, shown} while thinking
    let msg = null;                 // {id, text} of the reply being streamed
    let msgs = 0, streamed = false, pending = false;
    let status = "working…";
    const names = new Map();        // tool id -> name
    const live = new Map();         // tool id -> {name, path, lines}
    const turn = { session: null, changed: [], feed, end, thinking };
    Object.defineProperty(turn, "status", {
      get: () => (think ? `thinking… ${thinking()}s` : status),
    });

    function thinking() {
      return think ? Math.round((now() - think.t0) / 1000) : null;
    }
    function endThinking(out) {
      if (!think) return;
      out.push({ draw: "thinking-end", secs: thinking(), shown: think.shown });
      think = null;
    }
    function startThinking(out) {
      msg = null;
      if (think) return;
      think = { t0: now(), shown: false };
      out.push({ draw: "thinking" });
    }
    function flush(out) {
      if (pending && msg) out.push({ draw: "message", id: msg.id, text: msg.text });
      pending = false;
    }
    const newMessage = (text) => (msg = { id: ++msgs, text });

    function feed(events) {
      const out = [];
      for (const e of events) {
        if (e.t !== "delta") flush(out);
        if (e.t !== "thinking" && e.t !== "thinking_start") endThinking(out);
        if (e.t === "thinking_start") startThinking(out);
        else if (e.t === "thinking") {
          startThinking(out);
          if (e.text) { think.shown = true; out.push({ draw: "thinking-text", text: e.text }); }
        } else if (e.t === "tool_start") {
          msg = null;
          names.set(e.id, e.name);
          out.push({ mark: "clear-read" }, { draw: "tool", id: e.id, name: e.name, summary: "" });
          status = `${e.name}…`;
        } else if (e.t === "tool_live") {
          let x = live.get(e.id);
          if (!x) {
            x = { name: names.get(e.id) || "Write", path: null, lines: 1 };
            live.set(e.id, x);
            out.push({ draw: "live", id: e.id, name: x.name });
          }
          if (e.path) {
            x.path = e.path;
            out.push({ draw: "live-path", id: e.id, name: x.name, path: e.path },
              { view: "begin", id: e.id, name: x.name, path: e.path }, { mark: "on", path: e.path });
          }
          if (e.text) {
            x.lines += (e.text.match(/\n/g) || []).length;
            out.push({ draw: "live-text", id: e.id, text: e.text });
          }
          out.push({ view: "feed", event: e });
          status = `writing ${x.path || "a file"}… (${x.lines} lines)`;
        } else if (e.t === "init") {
          turn.session = e.session_id;
          out.push({ save: "session", id: e.session_id });
        } else if (e.t === "message_start") {
          msg = null; streamed = false;
        } else if (e.t === "delta") {
          if (!msg) { newMessage(""); status = "writing a reply…"; }
          streamed = true; msg.text += e.text; pending = true;
        } else if (e.t === "text") {
          if (!streamed) { out.push({ draw: "message", id: newMessage(e.text).id, text: e.text }); msg = null; }
        } else if (e.t === "tool") {
          msg = null;
          names.set(e.id, e.name);
          out.push({ draw: "tool", id: e.id, name: e.name, summary: e.summary || "" }, { mark: "clear-read" });
          if (e.path) out.push({ mark: "on", path: e.path });
          if (e.path && e.lines) out.push({ mark: "read", path: e.path, from: e.lines[0] - 1, to: e.lines[1] - 1 });
          status = `${e.name} ${e.summary || ""}`.trim().slice(0, 80) + "…";
        } else if (e.t === "tool_result") {
          out.push({ draw: "tool-result", id: e.id, ok: !e.error, preview: e.preview || "" });
          if (live.delete(e.id)) out.push({ draw: "live-close", id: e.id });
          out.push({ view: "end", id: e.id, ok: !e.error });
          status = "working…";
        } else if (e.t === "build" || e.t === "error" || e.t === "context") {
          out.push({ draw: e.t, event: e });
        } else if (e.t === "done") {
          turn.changed = (e.changed || []).map((c) => c.path);
          out.push({ draw: "done", event: e });
        }
      }
      flush(out);
      return out;
    }

    // The turn is over (done, stopped, or the server gone): close what is still open.
    function end() {
      const out = [];
      flush(out);
      endThinking(out);
      for (const id of live.keys()) out.push({ draw: "live-close", id });
      live.clear();
      msg = null;
      return out;
    }

    return turn;
  }

  // After a turn: compile when a file it changed is newer than the PDF (or new to the list),
  // unless its own last build already has them.
  function needsCompile(changed, files, pdfMtime) {
    return changed.some((path) => {
      const f = files.find((x) => x.path === path);
      return !f || !pdfMtime || f.mtime > pdfMtime;
    });
  }

  const pollFailed = (status) => status === 0 || status === 408 || status === 429 || status >= 500 ? "retry" : "lost";
  const pollDelay = (attempts) => Math.min(30000, 1000 * 2 ** Math.min(attempts, 5));
  function result(e) {
    const outcome = e.outcome || (e.is_error || (e.exit != null && e.exit !== 0) ? "failed" : "ok");
    const error = outcome !== "failed" ? null : "outcome" in e ? e.error || null
      : e.error || `${e.subtype || "exit " + e.exit}${e.stderr ? "\n" + e.stderr : ""}`;
    return { outcome, error };
  }
  return { create, needsCompile, pollFailed, pollDelay, result };
})();

if (typeof module !== "undefined") module.exports = Turn;
