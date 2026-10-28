/* prism-local — TeX math in the agent's replies. The math is taken out before Markdown
   reads the reply, and put back as KaTeX after, so nothing in it is read as Markdown:
   not _ or * (emphasis), \\ (an escaped backslash), | (a table cell) or a line break.
   What counts as math is what LaTeX writes, outside code spans and fences:

     $…$  \(…\)                     inline; $ x $ too, and over a line break, not over a
                                    blank line; "$5 and $6" is money
     $$…$$  \[…\]                   display
     \begin{equation} … \end{…}     display, also align, gather, multline, eqnarray, …

   The project's own macros (\newcommand, \DeclareMathOperator, … from symbols) are KaTeX's
   too, and what KaTeX lacks is mapped (\label is dropped, \eqref shows its key, multline
   and eqnarray become gather and array). A delimiter that is not closed (a reply still
   streaming in) stays text. Plain: the page passes marked and KaTeX in, and
   tests/frontend_check.js runs it under Node.

     MathText.render(text, { markdown, katex, macros })   HTML; markdown: (text) => HTML
     MathText.split(text)        { text: with each formula a placeholder, math: [{raw, tex, display}] } */
"use strict";

const MathText = (() => {
  const ENVS = new Set(["equation", "equation*", "align", "align*", "alignat", "alignat*", "flalign", "flalign*",
    "gather", "gather*", "multline", "multline*", "eqnarray", "eqnarray*", "displaymath", "math", "dmath", "dmath*"]);
  // Placeholders: private-use characters, which no reply is expected to hold (any are dropped).
  const OPEN = "", CLOSE = "";
  const PLACE = /[]/g;
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  function split(source) {
    const src = String(source).replace(PLACE, ""), n = src.length, math = [];
    let out = "", i = 0;
    const put = (end, tex, display) => {
      out += OPEN + math.length + CLOSE;
      math.push({ raw: src.slice(i, end), tex, display });
      i = end;
    };
    // A blank line at j (a newline, then only spaces up to the next one): the paragraph ends.
    const blankAt = (j) => {
      if (src[j] !== "\n") return false;
      let k = j + 1;
      while (src[k] === " " || src[k] === "\t") k++;
      return k >= n || src[k] === "\n";
    };
    // Where `close` next starts from j, a backslash and the character after it skipped;
    // -1 if nowhere (or not before a blank line, with `para`).
    const find = (close, j, para) => {
      for (; j < n; j++) {
        if (src.startsWith(close, j)) return j;
        if (src[j] === "\\") j++;
        else if (para && blankAt(j)) return -1;
      }
      return -1;
    };
    const lineStart = (j) => j === 0 || src[j - 1] === "\n";

    while (i < n) {
      const c = src[i];
      // A code fence: up to the line that closes it, or the end.
      if ((c === "`" || c === "~" || c === " ") && lineStart(i)) {
        const f = /^ {0,3}(`{3,}|~{3,})[^\n]*/.exec(src.slice(i, i + 1000));
        if (f && !(f[1][0] === "`" && f[0].slice(f[0].indexOf(f[1]) + f[1].length).includes("`"))) {
          const fence = new RegExp("\\n {0,3}" + (f[1][0] === "`" ? "`" : "~") + "{" + f[1].length + ",}[ \\t]*(?=\\n|$)", "g");
          fence.lastIndex = i + f[0].length;
          const m = fence.exec(src);
          const end = m ? m.index + m[0].length : n;
          out += src.slice(i, end); i = end;
          continue;
        }
      }
      // A code span: up to the same number of backticks, within the paragraph.
      if (c === "`") {
        let k = i;
        while (src[k] === "`") k++;
        const run = k - i;
        let j = k, end = -1;
        while (j < n) {
          if (blankAt(j)) break;
          if (src[j] !== "`") { j++; continue; }
          let e = j;
          while (src[e] === "`") e++;
          if (e - j === run) { end = e; break; }
          j = e;
        }
        if (end < 0) end = k;
        out += src.slice(i, end); i = end;
        continue;
      }
      if (c === "\\") {
        const nx = src[i + 1];
        if (nx === "(" || nx === "[") {
          const close = nx === "(" ? "\\)" : "\\]", j = find(close, i + 2, nx === "(");
          if (j >= 0 && src.slice(i + 2, j).trim()) { put(j + 2, src.slice(i + 2, j), nx === "["); continue; }
        } else if (nx === "b") {
          const b = /^\\begin\{([A-Za-z]+\*?)\}/.exec(src.slice(i, i + 40));
          if (b && ENVS.has(b[1])) {
            const end = "\\end{" + b[1] + "}", j = find(end, i + b[0].length, false);
            if (j >= 0) { put(j + end.length, src.slice(i, j + end.length), b[1] !== "math"); continue; }
          }
        }
        out += src.slice(i, i + 2); i += 2;       // \$ \\ \* …: Markdown's escapes, and TeX's
        continue;
      }
      if (c === "$") {
        if (src[i + 1] === "$") {
          const j = find("$$", i + 2, false);
          if (j >= 0 && src.slice(i + 2, j).trim()) { put(j + 2, src.slice(i + 2, j), true); continue; }
          out += "$$"; i += 2;
          continue;
        }
        const j = find("$", i + 1, true), body = j >= 0 ? src.slice(i + 1, j) : "";
        // "$5 and $6": a digit after the first, a space before the second, is money.
        if (body.trim() && !(/^\d/.test(body) && /\s$/.test(body))) { put(j + 1, body, false); continue; }
        out += "$"; i++;
        continue;
      }
      out += c; i++;
    }
    return { text: out, math };
  }

  // What KaTeX does not know, in terms it does.
  const BASE_MACROS = {
    "\\eqref": "(\\text{#1})", "\\ref": "\\text{#1}", "\\cref": "\\text{#1}", "\\Cref": "\\text{#1}",
    "\\autoref": "\\text{#1}", "\\qedhere": "", "\\xspace": "", "\\ensuremath": "#1",
  };
  const ENV_MAP = { multline: "gather", "multline*": "gather*", eqnarray: "array}{rcl", "eqnarray*": "array}{rcl" };
  function prepare(tex) {
    return tex
      .replace(/\\label\s*\{[^{}]*\}/g, "")
      .replace(/\\(begin|end)\{(displaymath|math|dmath\*?)\}/g, "")
      .replace(/\\(begin|end)\{(multline\*?|eqnarray\*?)\}/g, (_, be, env) =>
        be === "begin" ? `\\begin{${ENV_MAP[env]}}` : `\\end{${ENV_MAP[env].split("}")[0]}}`);
  }

  // The macro set's part of a formula's key: a short id, the same for equal sets, worked
  // out once per macros object (the page replaces the object when the set changes; it
  // never changes one in place). Ids are never reused, so forgetting the sets seen only
  // costs a formula typeset again.
  const macroIds = new WeakMap(), idsBySet = new Map();
  let lastMacroId = 0;
  function macroId(macros) {
    if (!macros) return "";
    let id = macroIds.get(macros);
    if (id === undefined) {
      const set = JSON.stringify(Object.entries(macros).sort());
      id = idsBySet.get(set);
      if (id === undefined) {
        if (idsBySet.size >= 64) idsBySet.clear();
        id = String(++lastMacroId); idsBySet.set(set, id);
      }
      macroIds.set(macros, id);
    }
    return id;
  }

  const caches = new WeakMap();
  function texHtml(katex, f, macros) {
    let cache = caches.get(katex);
    if (!cache) { cache = { entries: new Map(), size: 0 }; caches.set(katex, cache); }
    const key = JSON.stringify([f.tex, f.display, macroId(macros)]);
    if (cache.entries.has(key)) {
      const html = cache.entries.get(key); cache.entries.delete(key); cache.entries.set(key, html); return html;
    }
    try {
      const html = katex.renderToString(prepare(f.tex), {
        displayMode: f.display, throwOnError: false, strict: "ignore",
        macros: { ...BASE_MACROS, ...(macros || {}) },
      });
      if (key.length + html.length <= 100000) {
        cache.entries.set(key, html); cache.size += key.length + html.length;
        while (cache.entries.size > 256 || cache.size > 2000000) {
          const oldest = cache.entries.keys().next().value;
          cache.size -= oldest.length + cache.entries.get(oldest).length; cache.entries.delete(oldest);
        }
      }
      return html;
    } catch {                                    // not a parse error: the source, as code
      return `<code class="tex-src">${esc(f.raw)}</code>`;
    }
  }

  // The reply as HTML: Markdown with the math taken out, the math put back as KaTeX. A
  // placeholder in code (an indented block) or in a tag's attribute goes back as its source.
  function render(text, { markdown, katex, macros, afterMath }) {
    const { text: md, math } = split(text);
    let code = 0;
    const source = (s) => s.replace(/(\d+)/g, (_, k) => esc(math[k] ? math[k].raw : ""));
    return markdown(md).replace(/<[^>]*>|(\d+)/g, (m, k) => {
      if (k === undefined) {
        const t = /^<(\/?)(pre|code)\b/i.exec(m);
        if (t) code = Math.max(0, code + (t[1] ? -1 : 1));
        return source(m);
      }
      const f = math[k];
      if (!f) return "";
      if (code) return esc(f.raw);
      const html = texHtml(katex, f, macros);
      return afterMath ? afterMath(html, f) : html;
    });
  }

  return { split, render, prepare, ENVS };
})();

if (typeof module !== "undefined") module.exports = MathText;
