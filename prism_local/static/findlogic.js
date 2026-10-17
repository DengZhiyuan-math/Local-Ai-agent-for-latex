/* The editor's plain decisions about finding text: what a query matches (match case, whole
   word, regular expression), what a replacement becomes ($1, $&), where the matches are
   in a file's lines, and how many words a file has. No page: app.js draws, these decide;
   checked under Node (tests/frontend_check.js).

     FindLogic.matches("a cat, a Cat", { query: "cat" })        // {list: [{from: 2, to: 5, m}, …]}
     FindLogic.expand("<$1>", m, { regex: true })               // the replacement of one match
     FindLogic.wordCount(text, "tex")                           // words outside math and commands */
"use strict";

const FindLogic = (() => {
  // A letter or digit of any script, or _: what may not touch a whole-word match.
  const WORD_CHAR = /[\p{L}\p{N}_]/u;
  const MAX_MATCHES = 10000;

  // The RegExp of a query, or {error} for an expression that is not one. Without the u flag:
  // \% and \{ (common in LaTeX) are errors under it.
  function pattern(opts) {
    const q = opts.query || "";
    if (!q) return { re: null };
    const source = opts.regex ? q : q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    try {
      return { re: new RegExp(source, "gm" + (opts.caseSensitive ? "" : "i")) };
    } catch (e) {
      return { re: null, error: "Invalid pattern" };
    }
  }

  const isWordAt = (text, i) => i >= 0 && i < text.length && WORD_CHAR.test(text[i]);

  // The matches of a query in a text: {list: [{from, to, m}], error, more}. Empty matches
  // (^, a*) are left out: there is nothing to show or replace. `more`: stopped at `limit`.
  function matches(text, opts, limit = MAX_MATCHES) {
    const { re, error } = pattern(opts);
    const list = [];
    if (!re) return { list, error, more: false };
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(text))) {
      if (!m[0].length) { re.lastIndex++; continue; }
      const from = m.index, to = from + m[0].length;
      if (opts.wholeWord && (isWordAt(text, from - 1) || isWordAt(text, to))) { re.lastIndex = from + 1; continue; }
      if (list.length >= limit) return { list, error, more: true };
      list.push({ from, to, m });
    }
    return { list, error, more: false };
  }

  // What one match is replaced with: the text as typed, or, for a regular expression, with
  // $1…$99, $& and $$ as String.prototype.replace reads them.
  function expand(replacement, m, opts) {
    if (!opts.regex) return replacement;
    return replacement.replace(/\$(\$|&|\d{1,2})/g, (all, k) => {
      if (k === "$") return "$";
      if (k === "&") return m[0];
      let n = +k;
      if (n >= m.length && k.length === 2) { n = +k[0]; if (n && n < m.length) return (m[n] ?? "") + k[1]; }
      return n && n < m.length ? (m[n] ?? "") : all;
    });
  }

  // `text` with each match replaced (matches as matches() gives them, in order).
  function replaceAll(text, list, replacement, opts) {
    let out = "", at = 0;
    for (const x of list) {
      out += text.slice(at, x.from) + expand(replacement, x.m, opts);
      at = x.to;
    }
    return out + text.slice(at);
  }

  // Where each match is: {line, ch, len, text (its line)}, lines and columns from 0.
  function lines(text, list) {
    const out = [];
    let line = 0, start = 0;
    for (const x of list) {
      let nl;
      while ((nl = text.indexOf("\n", start)) !== -1 && nl < x.from) { line++; start = nl + 1; }
      const end = text.indexOf("\n", start);
      const lineText = text.slice(start, end === -1 ? text.length : end).replace(/\r$/, "");
      out.push({ line, ch: x.from - start, len: x.to - x.from, text: lineText });
    }
    return out;
  }

  // A long line shortened around its match: {before, hit, after}, at most about `width`.
  function excerpt(lineText, ch, len, width = 80) {
    const hit = lineText.slice(ch, ch + len);
    let from = Math.max(0, ch - 24), to = Math.min(lineText.length, ch + len + Math.max(16, width - 24 - len));
    const before = (from > 0 ? "…" : "") + lineText.slice(from, ch).replace(/^\s+/, "");
    const after = lineText.slice(ch + len, to) + (to < lineText.length ? "…" : "");
    return { before, hit, after };
  }

  /* ---------------------------------------------------------------- words */
  // Environments whose content is mathematics, not words (with or without *).
  const MATH_ENVS = "equation|align|alignat|flalign|gather|multline|eqnarray|math|displaymath|split|dmath|IEEEeqnarray";
  // Commands whose argument is a name, a key or a file, not words; for \href only the URL.
  const NAME_ARG_COMMANDS = "label|ref|eqref|cref|Cref|crefrange|autoref|pageref|nameref|vref|cite[a-zA-Z]*|nocite|"
    + "input|include|includeonly|subfile|includegraphics|includepdf|usepackage|RequirePackage|documentclass|"
    + "bibliography|bibliographystyle|addbibresource|begin|end|newcommand|renewcommand|providecommand|"
    + "DeclareMathOperator|newenvironment|renewenvironment|newtheorem|theoremstyle|url|href|hypersetup|"
    + "setlength|addtolength|setcounter|addtocounter|pagestyle|thispagestyle|numberwithin|graphicspath|color|textcolor";
  // Han, Hiragana and Katakana: each character is a word.
  const CJK = /[぀-ヿ㐀-䶿一-鿿豈-﫿]/g;
  const WORD = /[\p{L}\p{N}]+(?:['’-][\p{L}\p{N}]+)*/gu;

  // The text of a LaTeX source that is read as words: no comments, no preamble (when the
  // file has \begin{document}; `body: false` for a piece of a file, a selection), no math,
  // no command names, no labels, keys and file names.
  function texWords(src, body = true) {
    let t = src.replace(/(^|[^\\])((?:\\\\)*)%.*$/gm, "$1$2");
    if (body) {
      const b = t.indexOf("\\begin{document}");
      if (b !== -1) {
        t = t.slice(b + "\\begin{document}".length);
        const e = t.indexOf("\\end{document}");
        if (e !== -1) t = t.slice(0, e);
      }
    }
    t = t.replace(new RegExp(`\\\\begin\\{((?:${MATH_ENVS})\\*?)\\}[\\s\\S]*?\\\\end\\{\\1\\}`, "g"), " ");
    t = t.replace(/\\\\\*?(?:\s*\[[^\]\n]*\])?/g, " ").replace(/\\\$/g, " ");   // a line break (\\[2pt]); a dollar sign
    t = t.replace(/\$\$[\s\S]*?\$\$/g, " ").replace(/\$[^$]*\$/g, " ")
      .replace(/\\\[[\s\S]*?\\\]/g, " ").replace(/\\\([\s\S]*?\\\)/g, " ");
    t = t.replace(new RegExp(`\\\\(?:${NAME_ARG_COMMANDS})\\*?(?:\\s*\\[[^\\]]*\\])*\\s*\\{[^{}]*\\}`, "g"), " ");
    return t.replace(/\\[A-Za-z@]+\*?/g, " ").replace(/\\./g, " ");
  }

  function markdownWords(src) {
    return src.replace(/^(```|~~~)[\s\S]*?^\1/gm, " ").replace(/`[^`\n]*`/g, " ")
      .replace(/\$\$[\s\S]*?\$\$/g, " ").replace(/\$[^$\n]*\$/g, " ")
      .replace(/\]\([^)]*\)/g, "] ").replace(/<[^>\n]*>/g, " ");
  }

  // How many words a text has. kind: "tex" or "md" (others are counted as they are).
  function wordCount(text, kind = "tex", { body = true } = {}) {
    const t = kind === "tex" ? texWords(text, body) : kind === "md" ? markdownWords(text) : text;
    const cjk = (t.match(CJK) || []).length;
    return cjk + (t.replace(CJK, " ").match(WORD) || []).length;
  }

  // The kind wordCount() reads a file as, from its name; null: words are not counted.
  function wordKind(path) {
    return /\.(tex|ltx)$/i.test(path) ? "tex" : /\.(md|markdown)$/i.test(path) ? "md" : /\.txt$/i.test(path) ? "txt" : null;
  }

  return { pattern, matches, expand, replaceAll, lines, excerpt, wordCount, wordKind, MAX_MATCHES };
})();

if (typeof module !== "undefined") module.exports = FindLogic;
