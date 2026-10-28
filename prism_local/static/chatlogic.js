/* The editor's plain decisions about what you type: @-mentions in a message to the agent,
   a /command's prompt, where a conversation is kept, and completions in the source (\ref,
   \cite, \commands). No page: app.js draws, these decide; checked under Node. */
"use strict";

const ChatLogic = (() => {
  // Where a conversation's parts are kept (localStorage): its session, context, log and
  // snippets per project and provider (a conversation belongs to both); other settings
  // per provider only. null: the project is not known yet.
  const PER_CONVERSATION = ["session", "context", "log", "snips"];
  function storageKey(k, project, provider) {
    if (PER_CONVERSATION.includes(k)) return project ? `chat.${project}.${provider}.${k}` : null;
    return `chat.${k}.${provider}`;
  }

  // "@sec/intro.tex:3-5" in a message: the editable files it names, each once, with lines.
  const MENTION_RE = /(^|\s)@([^\s@]+)/g;
  function parseMentions(text, files) {
    const out = [], seen = new Set();
    for (const m of text.matchAll(MENTION_RE)) {
      const raw = m[2].replace(/[.,;!?)]+$/, "");
      const mm = /^(.+?)(?::(\d+)(?:-(\d+))?)?$/.exec(raw);
      if (!mm || seen.has(raw) || !files.some((f) => f.path === mm[1] && f.editable !== false)) continue;
      seen.add(raw);
      const from = mm[2] ? +mm[2] : null;
      out.push({ token: "@" + raw, file: mm[1], from, to: mm[3] ? +mm[3] : from });
    }
    return out;
  }

  // The prompt for a message starting with "/": the command must come first. Skills take
  // free text. Keep referenced text for unknown commands and all providers too.
  const slashPrompt = (text, refs) => (refs ? text + "\n\n" + refs : text);

  // Source navigation in an agent reply. Explicit TeX references remain clickable when
  // their label is missing; plain names must occur in the project's label index. The
  // caller inserts links with DOM APIs, so labels are returned as text, never HTML.
  function labelReferences(text, knownLabels = []) {
    text = String(text ?? "");
    const out = [];
    const commands = /\\(?:label|[cC]ref|[cC]pageref|ref|eqref|autoref|pageref|labelcref|namecref|nameref)\*?\s*\{([^{}]*)\}/g;
    for (const m of text.matchAll(commands)) {
      const start = m.index + m[0].indexOf("{") + 1;
      let offset = 0;
      const parts = m[0].startsWith("\\label{") || /^\\label\s/.test(m[0]) ? [m[1]] : m[1].split(",");
      for (const part of parts) {
        const label = part.trim();
        if (label && label.length <= 512) {
          const from = start + offset + part.indexOf(label);
          out.push({ from, to: from + label.length, label });
        }
        offset += part.length + 1;
      }
    }
    const names = [...new Set(Array.isArray(knownLabels) ? knownLabels : Object.keys(knownLabels || {}))]
      .filter((name) => typeof name === "string" && name.length > 0 && name.length <= 512)
      .sort((a, b) => b.length - a.length);
    if (names.length) {
      const pattern = names.map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
      const joined = (c) => !!c && /[\p{L}\p{N}_:/+\-\[\]\\]/u.test(c);
      for (const m of text.matchAll(new RegExp(pattern, "g"))) {
        const from = m.index, to = from + m[0].length;
        if (joined(text[from - 1]) || joined(text[to])
            || (text[from - 1] === "." && joined(text[from - 2]))
            || (text[to] === "." && joined(text[to + 1]))
            || out.some((r) => from < r.to && to > r.from)) continue;
        out.push({ from, to, label: m[0] });
      }
    }
    return out.sort((a, b) => a.from - b.from);
  }

  function compose({ text, refs = "", attached = "", mentions = [], mode = "ask" }) {
    const slash = /^\/[\w:.-]+(?:\s|$)/.test(text);    // a command as the page reads one (not /usr/bin)
    let prompt = slash ? slashPrompt(text, refs) : refs ? refs + "\n\n" + text : text;
    if (attached) prompt = slash ? prompt + "\n\n" + attached : attached + "\n\n" + prompt;
    const scope = mode === "edit" && mentions.length ? [...new Set(mentions.map((m) => m.file))] : null;
    return { prompt, scope };
  }
  // HTTP and stream updates share one ordering rule. A partial rate notification is
  // useful, but cannot replace the last complete subscription snapshot.
  function mergeUsage(current, incoming) {
    current = current || {};
    incoming = incoming || { rate: null, stale: true };
    const rate = incoming.rate;
    if (rate && !rate.unifiedWindows) return { ...current, stale: true };
    const sameIdentity = !current.identity || !incoming.identity || current.identity === incoming.identity;
    const before = current.last_success || (current.rate || {}).at || 0;
    const after = incoming.last_success || (rate || {}).at || 0;
    if (rate && current.rate && sameIdentity && after < before) return current;
    return incoming;
  }

  const tex = typeof module !== "undefined" ? require("./texcomplete.js") : TexComplete;
  return { storageKey, parseMentions, slashPrompt, compose, labelReferences, mergeUsage, MENTION_RE, ...tex };
})();

if (typeof module !== "undefined") module.exports = ChatLogic;
