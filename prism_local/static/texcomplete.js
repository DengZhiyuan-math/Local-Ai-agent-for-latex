/* Source completion from project labels, bibliography keys and macros. No page state. */
"use strict";
const TexComplete = (() => {
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

  return { completions, REF_RE, CITE_RE, CMD_RE };
})();
if (typeof module !== "undefined") module.exports = TexComplete;
