/* Source completion from project labels, bibliography keys and macros. No page state. */
"use strict";
const TexComplete = (() => {
  const REF_RE = /\\(?:[cC]ref|[cC]pageref|ref|eqref|autoref|pageref|labelcref|namecref|nameref)\*?\{([^}]*)$/;
  const CITE_RE = /\\(?:cite[tp]?|nocite|citeauthor|citeyear)\*?(?:\[[^\]]*\]){0,2}\{([^}]*)$/;
  const CMD_RE = /\\([A-Za-z]*)$/;
  const BEGIN_RE = /\\begin\{([A-Za-z*]*)$/;
  const ENVIRONMENTS = ["equation", "equation*", "align", "align*", "aligned", "gather", "gather*",
    "multline", "multline*", "split", "cases", "matrix", "pmatrix", "bmatrix", "vmatrix", "Vmatrix",
    "itemize", "enumerate", "description", "figure", "figure*", "table", "table*", "tabular",
    "center", "flushleft", "flushright", "quote", "quotation", "verbatim", "proof", "theorem",
    "lemma", "proposition", "corollary", "definition", "remark", "example", "abstract", "document"];
  const COMMON_CMDS = ["begin", "end", "label", "cref", "Cref", "eqref", "cite", "section", "subsection", "emph", "textbf",
    "mathbb", "mathcal", "mathrm", "operatorname", "frac", "sum", "int", "lim", "sup", "inf", "left", "right", "langle", "rangle",
    "varepsilon", "alpha", "beta", "gamma", "delta", "lambda", "mu", "sigma", "omega", "Omega", "partial", "nabla", "infty",
    "subseteq", "coloneqq", "quad", "qquad", "text", "item", "input", "footnote", "ref", "includegraphics"];

  function beginMatch(before) {
    const match = before.match(BEGIN_RE);
    if (!match) return null;
    const prefix = before.slice(0, match.index);
    if ((prefix.match(/\\+$/) || [""])[0].length % 2) return null;
    const code = prefix.replace(/\\[\s\S]|%[^\n]*/g, (token) => token[0] === "%" ? "" : token);
    return code === prefix ? match : null;
  }

  // What to offer at the end of `before` (the line up to the cursor): {word, items:
  // [{text, kind}]}, word being what is typed already; null when nothing fits. symbols:
  // /api/symbols. A \command is offered from two letters on, or at once when `explicit`.
  function completions(before, symbols, explicit) {
    let m, items = [], word = "", environment = false;
    if ((m = beginMatch(before))) {
      environment = true;
      word = m[1];
      items = ENVIRONMENTS.map((text) => ({ text, kind: "environment" }));
    } else if ((m = before.match(REF_RE))) {
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
    const list = items.filter((i) => environment ? i.text.toLowerCase().startsWith(w)
      : i.text.toLowerCase().includes(w) && i.text !== word)
      .sort((a, b) => (a.text.toLowerCase().startsWith(w) ? 0 : 1) - (b.text.toLowerCase().startsWith(w) ? 0 : 1) || a.text.localeCompare(b.text))
      .slice(0, 60);
    return list.length ? { word, items: list } : null;
  }

  // A completion edit uses document offsets so CodeMirror can apply it as one undo step.
  function environmentEdit(source, cursor, name, indent = "  ") {
    const start = source.lastIndexOf("\n", cursor - 1) + 1;
    const before = source.slice(start, cursor), match = beginMatch(before);
    if (!match) return null;
    const from = cursor - match[1].length;
    const closing = source.slice(cursor).match(/^[A-Za-z*]*\}/);
    const to = cursor + (closing ? closing[0].length : 0);
    const leading = before.match(/^[\t ]*/)[0], body = leading + indent;
    // Count this name's enclosing environments too: a lone parent's closing tag must
    // stay with the parent. Comments and escaped backslashes are not environment tags.
    const tokens = /\\(begin|end)\{([^{}\n]+)\}|%[^\n]*|\\[\s\S]/g;
    let pending = 1;
    for (const token of source.slice(0, from - "\\begin{".length).matchAll(tokens)) {
      if (token[2] === name) pending = Math.max(1, pending + (token[1] === "begin" ? 1 : -1));
    }
    let hasEnd = false;
    for (const token of source.slice(to).matchAll(tokens)) {
      if (token[2] !== name) continue;
      pending += token[1] === "begin" ? 1 : -1;
      if (pending === 0) { hasEnd = true; break; }
    }
    if (hasEnd) {
      const tail = source.slice(to), emptyEnd = tail.match(/^[\t ]*(?:\n[\t ]*)?\\end\{([^}]+)\}/);
      if (emptyEnd && emptyEnd[1] === name) {
        const onNextLine = /^[\t ]*\n/.test(tail);
        return { from, to, text: name + "}\n" + body + (onNextLine ? "" : "\n" + leading),
          cursor: from + name.length + 2 + body.length };
      }
      const nextLine = tail.match(/^[\t ]*\n[\t ]*/);
      return { from, to, text: name + "}", cursor: from + name.length + 1 + (nextLine ? nextLine[0].length : 0) };
    }
    const text = name + "}\n" + body + "\n" + leading + "\\end{" + name + "}";
    return { from, to, text, cursor: from + name.length + 2 + body.length };
  }

  return { completions, environmentEdit, REF_RE, CITE_RE, CMD_RE };
})();
if (typeof module !== "undefined") module.exports = TexComplete;
