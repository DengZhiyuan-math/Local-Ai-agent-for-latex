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

  const REF_RE = /\\(?:[cC]ref|[cC]pageref|ref|eqref|autoref|pageref|labelcref|namecref|nameref)\*?\{([^}]*)$/;
  const CITE_RE = /\\(?:cite[tp]?|nocite|citeauthor|citeyear)\*?(?:\[[^\]]*\]){0,2}\{([^}]*)$/;
  const CMD_RE = /\\([A-Za-z]*)$/;
  const COMMON_CMDS = ["begin", "end", "label", "cref", "Cref", "eqref", "cite", "section", "subsection", "emph", "textbf",
    "mathbb", "mathcal", "mathrm", "operatorname", "frac", "sum", "int", "lim", "sup", "inf", "left", "right", "langle", "rangle",
    "varepsilon", "alpha", "beta", "gamma", "delta", "lambda", "mu", "sigma", "omega", "Omega", "partial", "nabla", "infty",
    "subseteq", "coloneqq", "quad", "qquad", "text", "item", "input", "footnote", "ref", "includegraphics"];

  // What to offer at the end of `before` (the line up to the cursor): {word, items:
  // [{text, kind}]}, word being what is typed already; null when nothing fits. symbols:
  // /api/symbols. A \command is offered from two letters on, or at once when `explicit`.
  function completions(before, symbols, explicit) {
    let m, items = [], word = "";
    if ((m = before.match(REF_RE))) {
      word = m[1].split(",").pop().trimStart();
      const titles = symbols.env_titles || {};
      items = symbols.labels.map((l) => ({ text: l.label, kind: `${titles[l.kind] || l.kind} · ${l.file.split("/").pop()}:${l.line}` }));
    } else if ((m = before.match(CITE_RE))) {
      word = m[1].split(",").pop().trimStart();
      items = symbols.bibkeys.map((k) => ({ text: k.key, kind: k.type }));
    } else if ((m = before.match(CMD_RE)) && (explicit || m[1].length >= 2)) {
      word = m[1];
      const mine = new Set((symbols.macros || []).map((x) => x.name));
      items = [...new Set([...mine, ...COMMON_CMDS])].map((n) => ({ text: n, kind: mine.has(n) ? "macros.tex" : "" }));
    } else return null;
    const w = word.toLowerCase();
    const list = items.filter((i) => i.text.toLowerCase().includes(w) && i.text !== word)
      .sort((a, b) => (a.text.toLowerCase().startsWith(w) ? 0 : 1) - (b.text.toLowerCase().startsWith(w) ? 0 : 1) || a.text.localeCompare(b.text))
      .slice(0, 60);
    return list.length ? { word, items: list } : null;
  }

  return { storageKey, parseMentions, slashPrompt, completions, MENTION_RE, REF_RE, CITE_RE, CMD_RE };
})();

if (typeof module !== "undefined") module.exports = ChatLogic;
