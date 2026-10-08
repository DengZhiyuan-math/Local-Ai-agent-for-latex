/* prism-local — snippets in the editor, after obsidian-latex-suite (MIT, artisticat1):
   https://github.com/artisticat1/obsidian-latex-suite

   A snippet is {trigger, replacement, options, priority?, description?}:
     trigger      a string, or a RegExp (or a string with option r) matched at the cursor
     replacement  text with tabstops $0 $1 … and ${1:placeholder} (the same number twice is
                  edited together), [[0]] [[1]] … for the trigger's capture groups, ${VISUAL}
                  for the selection; or a function (match or selection) => string | false
     options      t text, m math (M display, n inline), A expand as you type (otherwise Tab),
                  r regex trigger, v on a selection (the trigger is the key typed), w only
                  after a word boundary
   Tab jumps to the next tabstop (Shift-Tab back); with none, it leaves the bracket or the
   math the cursor is in, or puts & in a matrix. ${GREEK}, ${SYMBOL}, ${ACCENT}, ${MORE_SYMBOLS} in a
   trigger stand for those names. Math is found as LaTeX writes it: $ $, $$ $$, \( \), \[ \],
   equation, align, gather, … and \text{} inside them is text again; comments and the
   arguments of \label, \ref, \cite, \begin, … get no snippets at all.

   Your own snippets: ~/.prism-local/snippets.js (Snippets menu → Edit my snippets). Pure
   functions are exported for the tests (tests/test_snippets.py runs them under Node). */
"use strict";

const Snippets = (() => {
  const VARS = {
    GREEK: "alpha|beta|gamma|Gamma|delta|Delta|epsilon|varepsilon|zeta|eta|theta|vartheta|Theta|iota|kappa|"
      + "lambda|Lambda|mu|nu|xi|Xi|pi|Pi|rho|varrho|sigma|Sigma|tau|upsilon|Upsilon|phi|varphi|Phi|chi|psi|Psi|omega|Omega",
    SYMBOL: "parallel|perp|partial|nabla|hbar|ell|infty|oplus|ominus|otimes|oslash|square|star|dagger|vee|wedge|"
      + "subseteq|subset|supseteq|supset|emptyset|exists|nexists|forall|implies|impliedby|iff|setminus|neg|"
      + "bigcup|bigcap|cdot|times|simeq|approx",
    ACCENT: "hat|widehat|bar|overline|dot|ddot|tilde|widetilde|underline|vec",
    MORE_SYMBOLS: "leq|geq|neq|gg|ll|equiv|sim|propto|rightarrow|leftarrow|Rightarrow|Leftarrow|leftrightarrow|to|"
      + "mapsto|cap|cup|in|sum|prod|exp|ln|log|det|dots|vdots|ddots|pm|mp|int|iint|iiint|oint",
  };

  // Display math environments (M), and the inline one (n). Any environment begun inside
  // math is tracked too, for the matrix keys.
  const DISPLAY_ENVS = new Set(["equation", "equation*", "align", "align*", "alignat", "alignat*", "flalign",
    "flalign*", "gather", "gather*", "multline", "multline*", "eqnarray", "eqnarray*", "displaymath",
    "dmath", "dmath*", "IEEEeqnarray", "IEEEeqnarray*"]);
  const INLINE_ENVS = new Set(["math"]);
  // Where Tab puts & and Enter ends the row with \\.
  const ROW_ENVS = new Set(["matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "smallmatrix",
    "array", "cases", "dcases", "aligned", "alignedat", "split", "align", "align*", "alignat", "alignat*",
    "flalign", "flalign*", "eqnarray", "eqnarray*", "IEEEeqnarray", "IEEEeqnarray*"]);
  const NO_AMP_ENVS = new Set(["gather", "gather*", "gathered", "multline", "multline*"]);
  // Arguments that are text inside math, and arguments that get no snippets anywhere.
  const TEXT_ARG = new Set(["text", "textrm", "textit", "textbf", "textsf", "texttt", "textup", "emph", "mbox",
    "hbox", "intertext", "shortintertext"]);
  const RAW_ARG = new Set(["label", "ref", "eqref", "cref", "Cref", "autoref", "pageref", "nameref", "cite",
    "citep", "citet", "parencite", "textcite", "autocite", "footcite", "nocite", "begin", "end", "tag",
    "usepackage", "documentclass", "input", "include", "includegraphics", "url", "href", "bibliography",
    "bibliographystyle", "newcommand", "renewcommand", "DeclareMathOperator", "operatorname", "hyperref"]);

  /* ------------------------------------------------------------ where the cursor is */
  // The state at a point is a stack of frames: {m: "M" | "n" | "t" | "raw", env?, d?}.
  // m: what is written there (display math, inline math, text, nothing for snippets);
  // env: the environment a frame belongs to; d: brace depth of an argument frame.
  const top = (st) => st[st.length - 1];
  const modeOf = (st) => (st.length ? top(st).m : "t");

  function popTo(st, test) {
    for (let i = st.length - 1; i >= 0; i--) if (test(st[i])) { st.length = i; return true; }
    return false;
  }

  // Scan `text` (no newline in it) from state `st` (changed in place). Returns true if
  // it ends in a comment.
  function scanLine(st, text) {
    let i = 0;
    const n = text.length;
    while (i < n) {
      const c = text[i], f = top(st);
      if (f && f.d !== undefined) {                  // inside an argument: count braces
        if (c === "\\") { if (/[{}\\$%]/.test(text[i + 1] || "")) { i += 2; continue; } }
        else if (c === "{") { f.d++; i++; continue; }
        else if (c === "}") { if (--f.d === 0) st.pop(); i++; continue; }
        else if (c === "%") return true;
        if (f.m === "raw") { i++; continue; }
      }
      if (c === "%") return true;
      if (c === "$") {
        if (text[i + 1] === "$") {
          if (f && f.k === "$$") st.pop(); else st.push({ m: "M", k: "$$" });
          i += 2; continue;
        }
        if (f && f.k === "$") st.pop(); else st.push({ m: "n", k: "$" });
        i++; continue;
      }
      if (c !== "\\") { i++; continue; }
      const nx = text[i + 1] || "";
      if (nx === "(" || nx === "[") { st.push({ m: nx === "(" ? "n" : "M", k: "\\" + nx }); i += 2; continue; }
      if (nx === ")" || nx === "]") { const k = nx === ")" ? "\\(" : "\\["; popTo(st, (g) => g.k === k); i += 2; continue; }
      if (!/[A-Za-z@]/.test(nx)) { i += 2; continue; }      // \\ \$ \% \{ …
      let j = i + 1;
      while (j < n && /[A-Za-z@]/.test(text[j])) j++;
      const name = text.slice(i + 1, j);
      if (name === "begin" || name === "end") {
        const m = /^\s*\{([^{}]*)\}/.exec(text.slice(j));
        if (m) {
          const env = m[1].trim(), mode = modeOf(st);
          if (name === "begin") {
            if (DISPLAY_ENVS.has(env)) st.push({ m: "M", env });
            else if (INLINE_ENVS.has(env)) st.push({ m: "n", env });
            else if (mode === "M" || mode === "n") st.push({ m: mode, env });
          } else popTo(st, (g) => g.env === env);
          i = j + m[0].length; continue;
        }
      }
      i = j;
      const raw = RAW_ARG.has(name), txt = TEXT_ARG.has(name);
      if (raw || txt) {
        const m = /^\*?\s*(?:\[[^\]]*\]\s*)*\{/.exec(text.slice(j));
        if (m) { st.push({ m: raw ? "raw" : "t", d: 1 }); i = j + m[0].length; }
      }
    }
    return false;
  }

  // Context at line `line`, column `ch`, given `lineStates(line)` (the stack where that line starts).
  function contextAt(startState, lineText, ch) {
    const st = startState.map((f) => ({ ...f }));
    const comment = scanLine(st, lineText.slice(0, ch));
    let env = null;
    for (let i = st.length - 1; i >= 0; i--) if (st[i].env) { env = st[i].env; break; }
    return { mode: comment ? "raw" : modeOf(st), env, stack: st };
  }

  /* ------------------------------------------------------------ snippets */
  const fill = (s) => s.replace(/\$\{(GREEK|SYMBOL|ACCENT|MORE_SYMBOLS)\}/g, (_, k) => VARS[k]);

  function compile(list, origin) {
    const out = [];
    list.forEach((s, idx) => {
      if (!s || s.trigger === undefined || s.replacement === undefined) return;
      const o = String(s.options || "");
      const regex = s.trigger instanceof RegExp || o.includes("r");
      let re = null;
      if (regex) {
        const src = s.trigger instanceof RegExp ? s.trigger.source : String(s.trigger);
        const flags = (s.trigger instanceof RegExp ? s.trigger.flags : String(s.flags || "")).replace(/[gy]/g, "");
        try { re = new RegExp("(?:" + fill(src) + ")$", flags); } catch (e) { console.warn("snippet", s.trigger, e); return; }
      }
      out.push({
        trigger: regex ? null : String(s.trigger), re, replacement: s.replacement,
        // A replacement with ${VISUAL} in it is for a selection, as in obsidian-latex-suite.
        auto: o.includes("A"), word: o.includes("w"),
        visual: o.includes("v") || (typeof s.replacement === "string" && s.replacement.includes("${VISUAL}")),
        t: o.includes("t"), M: o.includes("M") || o.includes("m"), n: o.includes("n") || o.includes("m"),
        priority: Number(s.priority) || 0, order: (origin || 0) + idx, description: s.description || "",
      });
    });
    return out.sort(byPriority);
  }
  const byPriority = (a, b) => b.priority - a.priority || a.order - b.order;

  function modeOk(s, mode) {
    if (mode === "raw") return false;
    if (!s.t && !s.M && !s.n) return true;
    return (mode === "t" && s.t) || (mode === "M" && s.M) || (mode === "n" && s.n);
  }

  // The first snippet whose trigger ends `before` (the text up to the cursor). Returns
  // {snippet, start (index in before), text (expanded, with tabstops still in it)}.
  function match(snips, before, mode, auto) {
    for (const s of snips) {
      if (s.visual || s.auto !== auto || !modeOk(s, mode)) continue;
      let start, groups, arg;
      if (s.re) {
        const m = s.re.exec(before);
        if (!m || !m[0]) continue;
        start = m.index; groups = m.slice(1); arg = m;
      } else {
        if (!s.trigger || !before.endsWith(s.trigger)) continue;
        start = before.length - s.trigger.length; groups = []; arg = s.trigger;
      }
      const prev = before.slice(0, start);
      // Typing a command name (\sum, \alpha) must not set off a snippet inside it.
      if (/^[A-Za-z]/.test(before.slice(start)) && /\\[A-Za-z]*$/.test(prev)) continue;
      if (s.word && prev && !/[^A-Za-z0-9\\]$/.test(prev)) continue;
      const text = expandText(s.replacement, arg, groups, null);
      if (text === null) continue;
      return { snippet: s, start, text };
    }
    return null;
  }

  function visualMatch(snips, key, mode) {
    return snips.find((s) => s.visual && s.trigger === key && modeOk(s, mode)) || null;
  }

  // Replacement text: captures and the selection put in, tabstops left in. $ in what was
  // put in is kept apart (\u0001), and \u0002 marks where it begins, so that neither is
  // ever read as a tabstop ("$" then "2x" is not $2).
  function expandText(repl, arg, groups, visual) {
    let r = repl;
    if (typeof r === "function") {
      try { r = r(visual !== null ? visual : arg); } catch (e) { console.warn("snippet function", e); return null; }
      if (typeof r !== "string") return null;
    }
    const keep = (x) => "\u0002" + String(x === undefined ? "" : x).replace(/\$/g, "\u0001");
    r = r.replace(/\[\[(\d+)\]\]/g, (_, k) => keep(groups[+k]));
    if (visual !== null) r = r.replace(/\$\{VISUAL\}/g, () => keep(visual));
    return r;
  }

  // Tabstops out of a replacement: {text, stops: [{n, from, to}]} (offsets into text).
  // `indent` goes after each newline.
  function parse(r, indent) {
    let text = "";
    const stops = [];
    for (let i = 0; i < r.length;) {
      const c = r[i];
      if (c === "\u0002") { i++; continue; }
      if (c === "$") {
        let m = /^\$(\d+)/.exec(r.slice(i));
        if (m) { stops.push({ n: +m[1], from: text.length, to: text.length }); i += m[0].length; continue; }
        m = /^\$\{(\d+):/.exec(r.slice(i));
        if (m) {
          let j = i + m[0].length, depth = 1;
          for (; j < r.length; j++) {
            if (r[j] === "\\") { j++; continue; }
            if (r[j] === "{") depth++;
            else if (r[j] === "}" && --depth === 0) break;
          }
          const inner = parse(r.slice(i + m[0].length, j), indent);
          const from = text.length;
          text += inner.text;
          stops.push({ n: +m[1], from, to: text.length });
          i = j + 1; continue;
        }
      }
      text += c === "\n" ? "\n" + indent : c;
      i++;
    }
    return { text: text.replace(/\u0001/g, "$"), stops };
  }

  /* ------------------------------------------------------------ auto-fraction and tabout */
  // The term before a "/" just typed: "x" in "2x", "(a+b)" in "(a+b)", "e^{x}". Returns
  // {start, term} (start: index in before) or null.
  function fractionTerm(before) {
    let i = before.length - 1;
    const open = { ")": "(", "]": "[", "}": "{" };
    while (i >= 0) {
      const c = before[i];
      if (open[c]) {
        let depth = 0, j = i;
        for (; j >= 0; j--) {
          if (before[j] === c) depth++;
          else if (before[j] === open[c] && --depth === 0) break;
        }
        if (j < 0) break;
        i = j - 1; continue;
      }
      if (/[\s$({[=+\-,&<>|;:]/.test(c)) break;
      if (c === "\\" && before[i - 1] === "\\") break;          // \\ ends a row
      i--;
    }
    let term = before.slice(i + 1);
    if (!term || term === "\\" || /^\\(sum|prod|int|iint|oint|lim|frac|left|right|big|Big)$/.test(term)) return null;
    const start = i + 1;
    if (term[0] === "(" && term.endsWith(")")) {
      let depth = 0, whole = true;
      for (let k = 0; k < term.length; k++) {
        if (term[k] === "(") depth++;
        else if (term[k] === ")" && --depth === 0 && k < term.length - 1) { whole = false; break; }
      }
      if (whole) term = term.slice(1, -1);
    }
    return { start, term };
  }

  // How far Tab moves to leave the closest bracket or math ahead on this line: the number of
  // characters to skip, or -1.
  function tabout(after) {
    const m = /\\right\s*(?:\\[A-Za-z]+|\\[{}|]|[)\]|.>])|\\(?:rangle|rvert|rVert|rfloor|rceil|\}|\)|\])|\$\$|[)\]}$]/.exec(after);
    return m ? m.index + m[0].length : -1;
  }

  // Unclosed ( [ { before the cursor on this line (to tell a bracket from a matrix cell).
  function openDepth(before) {
    let d = 0;
    for (let i = 0; i < before.length; i++) {
      const c = before[i];
      if (c === "\\") { i++; continue; }
      if ("([{".includes(c)) d++;
      else if (")]}".includes(c)) d = Math.max(0, d - 1);
    }
    return d;
  }

  /* ------------------------------------------------------------ the default snippets */
  // Mostly obsidian-latex-suite's defaults, for LaTeX documents rather than Markdown.
  const DEFAULTS = [
    // Math
    { trigger: "mk", replacement: "$$0$", options: "tAw", description: "inline math" },
    { trigger: "dm", replacement: "\\[\n\t$0\n\\]", options: "tAw", description: "display math" },
    // Words begin with "beg": these wait for Tab.
    { trigger: "beg", replacement: "\\begin{$0}\n\t$1\n\\end{$0}", options: "tw", description: "environment" },
    { trigger: "beq", replacement: "\\begin{equation}\n\t$0\n\\end{equation}", options: "tw" },
    { trigger: "bal", replacement: "\\begin{align}\n\t$0\n\\end{align}", options: "tw" },

    // Greek letters
    { trigger: "@a", replacement: "\\alpha", options: "mA" },
    { trigger: "@b", replacement: "\\beta", options: "mA" },
    { trigger: "@g", replacement: "\\gamma", options: "mA" },
    { trigger: "@G", replacement: "\\Gamma", options: "mA" },
    { trigger: "@d", replacement: "\\delta", options: "mA" },
    { trigger: "@D", replacement: "\\Delta", options: "mA" },
    { trigger: "@e", replacement: "\\epsilon", options: "mA" },
    { trigger: ":e", replacement: "\\varepsilon", options: "mA" },
    { trigger: "@z", replacement: "\\zeta", options: "mA" },
    { trigger: "@t", replacement: "\\theta", options: "mA" },
    { trigger: "@T", replacement: "\\Theta", options: "mA" },
    { trigger: ":t", replacement: "\\vartheta", options: "mA" },
    { trigger: "@i", replacement: "\\iota", options: "mA" },
    { trigger: "@k", replacement: "\\kappa", options: "mA" },
    { trigger: "@l", replacement: "\\lambda", options: "mA" },
    { trigger: "@L", replacement: "\\Lambda", options: "mA" },
    { trigger: "@m", replacement: "\\mu", options: "mA" },
    { trigger: "@r", replacement: "\\rho", options: "mA" },
    { trigger: "@s", replacement: "\\sigma", options: "mA" },
    { trigger: "@S", replacement: "\\Sigma", options: "mA" },
    { trigger: "@u", replacement: "\\upsilon", options: "mA" },
    { trigger: "@U", replacement: "\\Upsilon", options: "mA" },
    { trigger: "@f", replacement: "\\phi", options: "mA" },
    { trigger: ":f", replacement: "\\varphi", options: "mA" },
    { trigger: "@F", replacement: "\\Phi", options: "mA" },
    { trigger: "@o", replacement: "\\omega", options: "mA" },
    { trigger: "@O", replacement: "\\Omega", options: "mA" },
    // A Greek name or a symbol name typed out gets its backslash (not inside a longer word).
    { trigger: "(^|[^\\\\A-Za-z])(${GREEK}|${SYMBOL})", replacement: "[[0]]\\[[1]]", options: "rmA",
      description: "backslash before Greek letters and symbols" },

    // Sub- and superscripts
    { trigger: "sr", replacement: "^{2}", options: "mA" },
    { trigger: "cb", replacement: "^{3}", options: "mA" },
    { trigger: "rd", replacement: "^{$0}$1", options: "mA", description: "raise to the power" },
    { trigger: "invs", replacement: "^{-1}", options: "mA" },
    { trigger: "conj", replacement: "^{*}", options: "mA" },
    { trigger: "dag", replacement: "^{\\dagger}", options: "mA" },
    { trigger: "__", replacement: "_{$0}$1", options: "mA" },
    { trigger: "(^|[^\\\\A-Za-z])([A-Za-z])(\\d)", replacement: "[[0]][[1]]_{[[2]]}", options: "rmA",
      priority: -1, description: "x2 → x_{2}" },
    { trigger: "\\\\(${GREEK})(\\d)", replacement: "\\[[0]]_{[[1]]}", options: "rmA", priority: -1 },
    { trigger: "([A-Za-z]|\\\\(?:${GREEK}))_\\{(\\d+)\\}(\\d)", replacement: "[[0]]_{[[1]][[2]]}", options: "rmA",
      priority: -1, description: "x_{2}3 → x_{23}" },
    { trigger: "([A-Za-z])(nn|ii|jj|kk)", replacement: (m) => `${m[1]}_{${m[2][0]}}`, options: "rmA",
      priority: -2, description: "xnn → x_{n}" },

    // Fractions, roots, operators
    { trigger: "//", replacement: "\\frac{$0}{$1}$2", options: "mA" },
    { trigger: "sq", replacement: "\\sqrt{$0}$1", options: "mA" },
    { trigger: "(\\d)rt", replacement: "\\sqrt[[[0]]]{$0}$1", options: "rmA", description: "n-th root" },
    { trigger: "ee", replacement: "e^{$0}$1", options: "mAw" },
    { trigger: "(^|[^\\\\A-Za-z])(exp|log|ln|det|dim|ker|deg|gcd|max|min|sup|inf|arg)", replacement: "[[0]]\\[[1]]",
      options: "rmA", description: "backslash before operator names" },
    { trigger: "(^|[^\\\\A-Za-z])(arcsin|sin|arccos|cos|arctan|tan|csc|sec|cot|sinh|cosh|tanh)", replacement: "[[0]]\\[[1]]",
      options: "rmA", description: "backslash before trigonometric functions" },
    { trigger: "trace", replacement: "\\operatorname{tr}", options: "mA" },
    { trigger: "Re", replacement: "\\operatorname{Re}", options: "mA" },
    { trigger: "Im", replacement: "\\operatorname{Im}", options: "mA" },
    { trigger: "bf", replacement: "\\mathbf{$0}$1", options: "mA" },
    { trigger: "rm", replacement: "\\mathrm{$0}$1", options: "mA" },
    { trigger: "cal", replacement: "\\mathcal{$0}$1", options: "mA" },
    { trigger: "frk", replacement: "\\mathfrak{$0}$1", options: "mA" },
    { trigger: "text", replacement: "\\text{$0}$1", options: "mA" },
    { trigger: "pmod", replacement: "\\pmod{${0:n}}$1", options: "mA" },

    // Accents: xhat → \hat{x}; hat → \hat{}
    { trigger: "([A-Za-z])(${ACCENT})", replacement: "\\[[1]]{[[0]]}", options: "rmA", priority: 1 },
    { trigger: "\\\\(${GREEK}) ?(${ACCENT})", replacement: "\\[[1]]{\\[[0]]}", options: "rmA", priority: 1 },
    { trigger: "hat", replacement: "\\hat{$0}$1", options: "mA" },
    { trigger: "bar", replacement: "\\bar{$0}$1", options: "mA" },
    { trigger: "dot", replacement: "\\dot{$0}$1", options: "mA", priority: -1 },
    { trigger: "ddot", replacement: "\\ddot{$0}$1", options: "mA", priority: 2 },
    { trigger: "cdot", replacement: "\\cdot", options: "mA", priority: 2 },
    { trigger: "tilde", replacement: "\\tilde{$0}$1", options: "mA" },
    { trigger: "und", replacement: "\\underline{$0}$1", options: "mA" },
    { trigger: "vec", replacement: "\\vec{$0}$1", options: "mA" },
    { trigger: "ovl", replacement: "\\overline{$0}$1", options: "mA" },

    // Symbols and relations
    { trigger: "ooo", replacement: "\\infty", options: "mA" },
    { trigger: "sum", replacement: "\\sum_{${0:i=1}}^{${1:n}} $2", options: "mA" },
    { trigger: "prod", replacement: "\\prod_{${0:i=1}}^{${1:n}} $2", options: "mA" },
    { trigger: "lim", replacement: "\\lim_{${0:n} \\to ${1:\\infty}} $2", options: "mA" },
    { trigger: "+-", replacement: "\\pm", options: "mA" },
    { trigger: "-+", replacement: "\\mp", options: "mA" },
    { trigger: "...", replacement: "\\dots", options: "mA" },
    { trigger: "nabl", replacement: "\\nabla", options: "mA" },
    { trigger: "xx", replacement: "\\times", options: "mA" },
    { trigger: "**", replacement: "\\cdot", options: "mA" },
    { trigger: "para", replacement: "\\parallel", options: "mA" },
    { trigger: "===", replacement: "\\equiv", options: "mA" },
    { trigger: "!=", replacement: "\\neq", options: "mA" },
    { trigger: ">=", replacement: "\\geq", options: "mA" },
    { trigger: "<=", replacement: "\\leq", options: "mA" },
    { trigger: ">>", replacement: "\\gg", options: "mA" },
    { trigger: "<<", replacement: "\\ll", options: "mA" },
    { trigger: "simm", replacement: "\\sim", options: "mA" },
    { trigger: "sim=", replacement: "\\simeq", options: "mA" },
    { trigger: "~~", replacement: "\\approx", options: "mA" },
    { trigger: "prop", replacement: "\\propto", options: "mA" },
    { trigger: "<->", replacement: "\\leftrightarrow", options: "mA" },
    { trigger: "->", replacement: "\\to", options: "mA" },
    { trigger: "!>", replacement: "\\mapsto", options: "mA" },
    { trigger: "=>", replacement: "\\implies", options: "mA" },
    { trigger: "=<", replacement: "\\impliedby", options: "mA" },
    { trigger: "iff", replacement: "\\iff", options: "mA" },
    { trigger: "and", replacement: "\\cap", options: "mAw" },
    { trigger: "orr", replacement: "\\cup", options: "mA" },
    { trigger: "inn", replacement: "\\in", options: "mA" },
    { trigger: "notin", replacement: "\\notin", options: "mA" },
    { trigger: "\\\\\\", replacement: "\\setminus", options: "mA" },
    { trigger: "sub=", replacement: "\\subseteq", options: "mA" },
    { trigger: "sup=", replacement: "\\supseteq", options: "mA" },
    { trigger: "eset", replacement: "\\emptyset", options: "mA" },
    { trigger: "set", replacement: "\\{ $0 \\}$1", options: "mAw" },
    { trigger: "AA", replacement: "\\forall", options: "mA" },
    { trigger: "EE", replacement: "\\exists", options: "mA" },
    { trigger: "LL", replacement: "\\mathcal{L}", options: "mA" },
    { trigger: "HH", replacement: "\\mathcal{H}", options: "mA" },
    { trigger: "CC", replacement: "\\mathbb{C}", options: "mA" },
    { trigger: "RR", replacement: "\\mathbb{R}", options: "mA" },
    { trigger: "ZZ", replacement: "\\mathbb{Z}", options: "mA" },
    { trigger: "NN", replacement: "\\mathbb{N}", options: "mA" },
    { trigger: "QQ", replacement: "\\mathbb{Q}", options: "mA" },
    { trigger: "o+", replacement: "\\oplus", options: "mA" },
    { trigger: "ox", replacement: "\\otimes", options: "mAw" },

    // Derivatives and integrals
    { trigger: "par", replacement: "\\frac{\\partial ${0:y}}{\\partial ${1:x}} $2", options: "m" },
    { trigger: "par(\\d)", replacement: "\\frac{\\partial^{[[0]]} ${0:y}}{\\partial ${1:x}^{[[0]]}} $2", options: "rmA" },
    { trigger: "ddt", replacement: "\\frac{d}{dt} ", options: "mA" },
    { trigger: "(^|[^\\\\A-Za-z])(o?int|iint|iiint)", replacement: "[[0]]\\[[1]]", options: "rmA", priority: -1 },
    { trigger: "\\int", replacement: "\\int $0 \\, d${1:x} $2", options: "m" },
    { trigger: "dint", replacement: "\\int_{${0:0}}^{${1:1}} $2 \\, d${3:x} $4", options: "mA", priority: 1,
      description: "definite integral" },
    { trigger: "oinf", replacement: "\\int_{0}^{\\infty} $0 \\, d${1:x} $2", options: "mA", priority: 1 },

    // Brackets
    { trigger: "avg", replacement: "\\langle $0 \\rangle $1", options: "mA" },
    { trigger: "norm", replacement: "\\lvert $0 \\rvert $1", options: "mA", priority: 1 },
    { trigger: "Norm", replacement: "\\lVert $0 \\rVert $1", options: "mA", priority: 1 },
    { trigger: "ceil", replacement: "\\lceil $0 \\rceil $1", options: "mA" },
    { trigger: "floor", replacement: "\\lfloor $0 \\rfloor $1", options: "mA" },
    { trigger: "lr(", replacement: "\\left( $0 \\right) $1", options: "mA" },
    { trigger: "lr[", replacement: "\\left[ $0 \\right] $1", options: "mA" },
    { trigger: "lr{", replacement: "\\left\\{ $0 \\right\\} $1", options: "mA" },
    { trigger: "lr|", replacement: "\\left| $0 \\right| $1", options: "mA" },
    { trigger: "lra", replacement: "\\left\\langle $0 \\right\\rangle $1", options: "mA" },

    // Environments inside math
    { trigger: "([pbBvV]?)mat", replacement: "\\begin{[[0]]matrix}\n\t$0\n\\end{[[0]]matrix}", options: "rMA" },
    { trigger: "([pbBvV]?)mat", replacement: "\\begin{[[0]]matrix} $0 \\end{[[0]]matrix}", options: "rnA" },
    { trigger: "cases", replacement: "\\begin{cases}\n\t$0\n\\end{cases}", options: "MA" },
    { trigger: "algn", replacement: "\\begin{aligned}\n\t$0\n\\end{aligned}", options: "MA" },
    { trigger: "iden(\\d)", replacement: (m) => {
      const n = +m[1], rows = [];
      for (let i = 0; i < n; i++) rows.push(Array.from({ length: n }, (_, j) => (i === j ? 1 : 0)).join(" & "));
      return "\\begin{pmatrix}\n\t" + rows.join(" \\\\\n\t") + "\n\\end{pmatrix}";
    }, options: "rMA", description: "n × n identity matrix" },

    // On a selection
    { trigger: "U", replacement: "\\underbrace{${VISUAL}}_{$0}", options: "mv" },
    { trigger: "O", replacement: "\\overbrace{${VISUAL}}^{$0}", options: "mv" },
    { trigger: "B", replacement: "\\underset{$0}{${VISUAL}}", options: "mv" },
    { trigger: "C", replacement: "\\cancel{${VISUAL}}", options: "mv" },
    { trigger: "K", replacement: "\\cancelto{$0}{${VISUAL}}", options: "mv" },
    { trigger: "S", replacement: "\\sqrt{${VISUAL}}", options: "mv" },
    { trigger: "F", replacement: "\\frac{${VISUAL}}{$0}", options: "mv" },
    { trigger: "E", replacement: "\\emph{${VISUAL}}", options: "tv" },
    { trigger: "M", replacement: "$${VISUAL}$", options: "tv" },
  ];

  // After a snippet that ends in a command (\alpha), a letter typed next gets a space before
  // it ("\alpha b"), unless the command may be growing into one of these (\in → \inf).
  const MACROS = ("infty inf int iint iiint in subset subseteq supset supseteq sup cdot cdots ldots dots sim "
    + "simeq setminus exists nexists to top times tan tanh sin sinh cos cosh cot coth sec csc log ln lim liminf "
    + "limsup leq le geq ge neq ne not notin perp partial pm mp pi phi psi chi varphi vartheta varepsilon varrho "
    + "epsilon eta beta theta zeta det dim deg ker exp max min arg gcd forall oplus otimes ominus equiv approx "
    + "propto implies impliedby iff mapsto emptyset nabla hbar ell star vee wedge cup cap bigcup bigcap parallel "
    + "left right langle rangle").split(" ");

  /* ------------------------------------------------------------ in the editor */
  function attach(cm, settings) {
    let snips = compile(DEFAULTS, 1e6);
    const sessions = [];           // tabstop sessions, innermost last
    let pendingSpace = null;       // where a letter typed next gets a space (see MACROS)
    const cache = new WeakMap();   // doc → line start states

    function lineState(doc, line) {
      let c = cache.get(doc);
      if (!c) {
        c = [[]];
        cache.set(doc, c);
        doc.on("change", (_, ch) => { c.length = Math.min(c.length, ch.from.line + 1); });
      }
      for (let l = c.length - 1; l < line; l++) {
        const st = c[l].map((f) => ({ ...f }));
        scanLine(st, doc.getLine(l) || "");
        c.push(st);
      }
      return c[line];
    }
    function context(pos) {
      const doc = cm.getDoc();
      if (!/stex|markdown/.test(String(doc.getMode().name))) return { mode: "raw", env: null };
      return contextAt(lineState(doc, pos.line), doc.getLine(pos.line), pos.ch);
    }

    // Text before `pos` back to the start of the previous line, and where it starts.
    function beforeText(pos) {
      const fromLine = Math.max(0, pos.line - 1);
      return { text: cm.getRange({ line: fromLine, ch: 0 }, pos), from: { line: fromLine, ch: 0 } };
    }
    const posAt = (base, offset) => cm.posFromIndex(cm.indexFromPos(base) + offset);

    function clearSessions() {
      while (sessions.length) sessions.pop().markers.forEach((g) => g.forEach((m) => m.clear()));
    }
    function dropSession() {
      const s = sessions.pop();
      if (s) s.markers.forEach((g) => g.forEach((m) => m.clear()));
    }

    // Replace from–to with `repl` (tabstops in it), and go to its first tabstop.
    function insert(from, to, repl) {
      const indent = (/^\s*/.exec(cm.getLine(from.line)) || [""])[0];
      const { text, stops } = parse(repl, indent);
      const unit = " ".repeat(cm.getOption("indentUnit") || 2);
      const tabbed = text.replace(/\t/g, unit);
      // Offsets move with the tabs that became spaces.
      const shift = (off) => off + (text.slice(0, off).match(/\t/g) || []).length * (unit.length - 1);
      cm.getDoc().changeGeneration(true);           // undo goes back to what you typed
      cm.operation(() => {
        cm.replaceRange(tabbed, from, to, "+snippet");
        const groups = new Map();
        for (const s of stops) {
          if (!groups.has(s.n)) groups.set(s.n, []);
          groups.get(s.n).push([posAt(from, shift(s.from)), posAt(from, shift(s.to))]);
        }
        const order = [...groups.keys()].sort((a, b) => a - b).map((k) => groups.get(k));
        pendingSpace = null;
        if (!order.length) {
          const end = posAt(from, tabbed.length);
          cm.setCursor(end);
          if (/\\[A-Za-z]+$/.test(tabbed)) pendingSpace = end;
          return;
        }
        if (order.length > 1) {
          const markers = order.map((g) => g.map(([a, b]) => cm.markText(a, b, {
            className: "cm-snippet-stop", clearWhenEmpty: false, inclusiveLeft: true, inclusiveRight: true,
          })));
          const span = cm.markText(from, posAt(from, tabbed.length), { clearWhenEmpty: false, inclusiveLeft: true, inclusiveRight: true });
          sessions.push({ markers, span, idx: -1 });
          go(1);
        } else cm.setSelections(order[0].map(([a, b]) => ({ anchor: a, head: b })));
      });
    }

    // Move to the next (+1) or previous (-1) tabstop. False if there is no session.
    function go(dir) {
      const s = sessions[sessions.length - 1];
      if (!s) return false;
      let idx = s.idx + dir;
      while (idx >= 0 && idx < s.markers.length) {
        const ranges = s.markers[idx].map((m) => m.find()).filter(Boolean);
        if (ranges.length) {
          s.idx = idx;
          cm.setSelections(ranges.map((r) => ({ anchor: r.from, head: r.to })));
          if (idx === s.markers.length - 1) { s.span.clear(); dropSession(); }   // the last one: done
          return true;
        }
        idx += dir;
      }
      if (dir > 0) { s.span.clear(); dropSession(); return go(1) || true; }
      return true;
    }

    function tryExpand(auto) {
      if (cm.somethingSelected() || cm.listSelections().length > 1) return false;
      const pos = cm.getCursor(), ctx = context(pos);
      if (ctx.mode === "raw") return false;
      const b = beforeText(pos);
      const m = match(snips, b.text, ctx.mode, auto);
      if (!m) return false;
      insert(posAt(b.from, m.start), pos, m.text);
      return true;
    }

    function tryFraction() {
      const pos = cm.getCursor(), ctx = context(pos);
      if (ctx.mode !== "M" && ctx.mode !== "n") return false;
      const line = cm.getLine(pos.line).slice(0, pos.ch);
      if (!line.endsWith("/")) return false;
      const t = fractionTerm(line.slice(0, -1));
      if (!t) return false;
      insert({ line: pos.line, ch: t.start }, pos, "\\frac{" + t.term.replace(/\$/g, "\u0001") + "}{$0}$1");
      return true;
    }

    cm.on("inputRead", (ed, ch) => {
      if (!settings.enabled() || ch.origin !== "+input" || ch.text.length !== 1) return;
      const typed = ch.text[0];
      if (pendingSpace && /^[A-Za-z]$/.test(typed) && ch.from.line === pendingSpace.line && ch.from.ch === pendingSpace.ch) {
        pendingSpace = null;
        const name = /\\([A-Za-z]+)$/.exec(cm.getLine(ch.from.line).slice(0, ch.from.ch));
        if (name && !MACROS.some((k) => k.startsWith(name[1] + typed))) cm.replaceRange(" ", ch.from, ch.from, "+input");
      } else pendingSpace = null;
      if (typed.length !== 1) return;
      if (tryExpand(true)) return;
      if (typed === "/" && settings.fraction()) tryFraction();
    });
    cm.on("beforeChange", (ed, ch) => {
      if (ch.origin === "undo" || ch.origin === "redo") { clearSessions(); return; }
      if (!settings.enabled() || ch.origin !== "+input" || !cm.somethingSelected() || cm.listSelections().length > 1) return;
      if (ch.text.length !== 1 || ch.text[0].length !== 1) return;
      const sel = cm.listSelections()[0];
      const from = CodeMirror.cmpPos(sel.anchor, sel.head) < 0 ? sel.anchor : sel.head;
      const to = from === sel.anchor ? sel.head : sel.anchor;
      const s = visualMatch(snips, ch.text[0], context(from).mode);
      if (!s) return;
      const text = expandText(s.replacement, null, [], cm.getRange(from, to));
      if (text === null) return;
      ch.cancel();
      setTimeout(() => insert(from, to, text), 0);
    });

    cm.on("cursorActivity", () => {
      const s = sessions[sessions.length - 1];
      if (!s) return;
      const r = s.span.find(), c = cm.getCursor();
      if (!r || CodeMirror.cmpPos(c, r.from) < 0 || CodeMirror.cmpPos(c, r.to) > 0) clearSessions();
    });
    cm.on("swapDoc", () => { clearSessions(); pendingSpace = null; });

    // Keys: Tab expands a snippet, jumps to the next tabstop, leaves a bracket or puts & in a
    // matrix; otherwise `fallback`.
    function tab(fallback) {
      if (!settings.enabled()) return fallback(cm);
      if (tryExpand(false) || go(1)) return;
      if (!cm.somethingSelected() && cm.listSelections().length === 1) {
        const pos = cm.getCursor(), ctx = context(pos);
        if (ctx.mode === "M" || ctx.mode === "n") {
          const line = cm.getLine(pos.line);
          const before = line.slice(0, pos.ch);
          if (settings.matrix() && ROW_ENVS.has(ctx.env) && !NO_AMP_ENVS.has(ctx.env) && before.trim() && !openDepth(before)) {
            cm.replaceRange(/\s$/.test(before) ? "& " : " & ", pos, pos, "+input");
            return;
          }
          const skip = tabout(line.slice(pos.ch));
          if (skip >= 0) { cm.setCursor({ line: pos.line, ch: pos.ch + skip }); return; }
        }
      }
      return fallback(cm);
    }
    function shiftTab() {
      if (settings.enabled() && go(-1)) return;
      return CodeMirror.Pass;
    }
    function enter() {
      if (!settings.enabled() || !settings.matrix() || cm.somethingSelected() || cm.listSelections().length > 1) return CodeMirror.Pass;
      const pos = cm.getCursor(), ctx = context(pos);
      if ((ctx.mode !== "M" && ctx.mode !== "n") || (!ROW_ENVS.has(ctx.env) && !NO_AMP_ENVS.has(ctx.env))) return CodeMirror.Pass;
      const line = cm.getLine(pos.line);
      const before = line.slice(0, pos.ch).trimEnd();
      if (line.slice(pos.ch).trim() || !before.trim() || /\\\\(\s*\[[^\]]*\])?$/.test(before)
          || /\\(begin|end)\{/.test(before) || /^\s*%/.test(before)) return CodeMirror.Pass;
      clearSessions();
      const indent = (/^\s*/.exec(line) || [""])[0];
      cm.replaceRange((/\s$/.test(line.slice(0, pos.ch)) ? "" : " ") + "\\\\\n" + indent, pos, pos, "+input");
    }
    function escape() {
      if (!sessions.length) return CodeMirror.Pass;
      clearSessions();
    }

    return {
      tab, shiftTab, enter, escape, context,
      // Your snippets come before the defaults at the same priority.
      setUser(list, keepDefaults) {
        const mine = compile(list, 0);
        snips = (keepDefaults ? mine.concat(compile(DEFAULTS, 1e6)) : mine).sort(byPriority);
      },
    };
  }

  // Your snippets file: JavaScript ending in (or being) an array, or `export default [...]`.
  // An object {snippets: [...], defaults: false} drops the defaults.
  function load(source) {
    const src = String(source || "").trim();
    if (!src) return { list: [], defaults: true };
    let v;
    if (/\bexport\s+default\b/.test(src)) v = new Function(src.replace(/\bexport\s+default\b/, "return"))();
    else v = new Function("return (" + src.replace(/;\s*$/, "") + "\n)")();
    if (Array.isArray(v)) return { list: v, defaults: true };
    if (v && Array.isArray(v.snippets)) return { list: v.snippets, defaults: v.defaults !== false };
    throw new Error("the file must give an array of snippets");
  }

  const TEMPLATE = `// Your snippets for prism-local. They come before the built-in ones (prism_local/static/snippets.js).
// {trigger, replacement, options, priority?, description?}
//   options: t text, m math (M display, n inline), A expand as you type (else Tab),
//            r regex trigger, v on a selection, w after a word boundary
//   replacement: $0 $1 … tabstops, \${1:placeholder}, [[0]] … regex groups, \${VISUAL} the selection
// Write {snippets: [...], defaults: false} instead of the array to drop the built-in ones.
[
  // {trigger: "RRn", replacement: "\\\\mathbb{R}^{\${0:n}}", options: "mA"},
  // {trigger: "thm", replacement: "\\\\begin{theorem}\\n\\t$0\\n\\\\end{theorem}", options: "tAw"},
]
`;

  return { attach, load, compile, match, parse, expandText, contextAt, scanLine, fractionTerm, tabout, openDepth,
    visualMatch, DEFAULTS, TEMPLATE, VARS };
})();

if (typeof module !== "undefined") module.exports = Snippets;
