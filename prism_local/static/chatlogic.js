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

  function compose({ text, refs = "", attached = "", mentions = [], mode = "ask" }) {
    const slash = text.startsWith("/");
    let prompt = slash ? slashPrompt(text, refs) : refs ? refs + "\n\n" + text : text;
    if (attached) prompt = slash ? prompt + "\n\n" + attached : attached + "\n\n" + prompt;
    const scope = mode === "edit" && mentions.length ? [...new Set(mentions.map((m) => m.file))] : null;
    return { prompt, scope };
  }
  const tex = typeof module !== "undefined" ? require("./texcomplete.js") : TexComplete;
  return { storageKey, parseMentions, slashPrompt, compose, MENTION_RE, ...tex };
})();

if (typeof module !== "undefined") module.exports = ChatLogic;
