/* The editor's plain decisions about finding text: what a query matches (match case, whole
   word, regular expression), what a replacement becomes ($1, $&), where the matches are
   in a file's lines, and which parts of a LaTeX file are formulas and comments. No page:
   app.js draws, these decide; checked under Node (tests/frontend_check.js).

     FindLogic.matches("a cat, a Cat", { query: "cat" })        // {list: [{from: 2, to: 5, m}, …]}
     FindLogic.expand("<$1>", m, { regex: true })               // the replacement of one match
     FindLogic.regions(text)                                    // {comments, math}: [from, to) spans */
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

  /* ---------------------------------------------------------------- where in a LaTeX file */
  const MATH_ENV_NAMES = new Set(["equation", "align", "alignat", "flalign", "gather", "multline", "eqnarray",
    "math", "displaymath", "dmath"]);
  // Where the closer `close` starts, from `i` on, past escapes (\x) and comments; -1 if none.
  function findClose(text, i, close) {
    while (i < text.length) {
      if (text.startsWith(close, i)) return i;
      const c = text[i];
      if (c === "\\") i += 2;
      else if (c === "%") { const nl = text.indexOf("\n", i); if (nl === -1) return -1; i = nl + 1; }
      else i++;
    }
    return -1;
  }
  // A LaTeX source's comments (% to the line's end) and mathematics ($…$, $$…$$, \(…\),
  // \[…\], and the equation-like environments, starred too): {comments, math}, each a
  // sorted list of [from, to). \$ and \% are text.
  function regions(text) {
    const comments = [], math = [];
    let i = 0;
    while (i < text.length) {
      const c = text[i];
      if (c === "%") {
        const nl = text.indexOf("\n", i), end = nl === -1 ? text.length : nl;
        comments.push([i, end]); i = end;
      } else if (c === "$") {
        const display = text[i + 1] === "$", open = display ? 2 : 1;
        const end = findClose(text, i + open, display ? "$$" : "$");
        // An inline formula does not go past an empty line: a lone $ is not one.
        if (end === -1 || (!display && /\n\s*\n/.test(text.slice(i, end)))) { i += open; continue; }
        math.push([i, end + open]); i = end + open;
      } else if (c === "\\") {
        const n = text[i + 1];
        let close = n === "[" ? "\\]" : n === "(" ? "\\)" : null;
        const env = !close && /^\\begin\{([A-Za-z]+)(\*?)\}/.exec(text.slice(i, i + 40));
        if (env && MATH_ENV_NAMES.has(env[1])) close = `\\end{${env[1]}${env[2]}}`;
        const from = env ? i + env[0].length : i + 2;
        const end = close ? findClose(text, from, close) : -1;
        if (end === -1) { i += 2; continue; }
        math.push([i, end + close.length]); i = end + close.length;
        // comments inside a display: still comments
        for (let k = text.indexOf("%", from); k !== -1 && k < end; k = text.indexOf("%", k + 1)) {
          if (text[k - 1] === "\\" && text[k - 2] !== "\\") continue;
          const nl = text.indexOf("\n", k), e = nl === -1 || nl > end ? end : nl;
          comments.push([k, e]); k = e;
        }
      } else i++;
    }
    comments.sort((a, b) => a[0] - b[0]);
    return { comments, math };
  }
  // Whether [from, to) lies inside one of the sorted spans; whether it touches any.
  function within(spans, from, to) {
    let lo = 0, hi = spans.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1, s = spans[mid];
      if (s[1] <= from) lo = mid + 1; else if (s[0] > from) hi = mid - 1; else return to <= s[1];
    }
    return false;
  }
  const touches = (spans, from, to) => spans.some((s) => s[0] < to && from < s[1]);
  // Matches kept by where they are: "math" (inside one formula, not in a comment), "text"
  // (in neither), anything else everywhere.
  function scoped(list, regs, where) {
    if (where !== "math" && where !== "text") return list;
    return list.filter((x) => !touches(regs.comments, x.from, x.to)
      && (where === "math" ? within(regs.math, x.from, x.to) : !touches(regs.math, x.from, x.to)));
  }

  // "120", "120:5" or "120,5" (line, column from 1) as {line, ch} from 0; null if it is none.
  function lineSpec(text) {
    const m = /^\s*(\d+)\s*(?:[:,]\s*(\d+)\s*)?$/.exec(text);
    if (!m) return null;
    return { line: Math.max(0, +m[1] - 1), ch: m[2] ? Math.max(0, +m[2] - 1) : 0 };
  }

  return { pattern, matches, expand, replaceAll, lines, excerpt, regions, scoped, lineSpec, MAX_MATCHES };
})();

if (typeof module !== "undefined") module.exports = FindLogic;
