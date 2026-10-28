/* LaTeX syntax colours. Track math independently of command arguments: the bundled
   stex mode doesn't recognise equation environments or \text inside inline math. */
"use strict";

(() => {
  const REFERENCES = new Set([
    "label", "ref", "eqref", "pageref", "autoref", "nameref", "vref", "Vref", "vpageref",
    "cref", "Cref", "cpageref", "Cpageref", "crefrange", "Crefrange", "cpagerefrange", "Cpagerefrange",
    "cite", "Cite", "citep", "Citep", "citet", "Citet", "citealt", "citealp", "citeauthor", "Citeauthor",
    "citeyear", "citeyearpar", "citetitle", "parencite", "Parencite", "textcite", "Textcite",
    "autocite", "Autocite", "footcite", "footcitetext", "smartcite", "supercite", "fullcite", "footfullcite",
    "nocite", "bibitem", "Bibitem", "RBibitem",
  ]);
  const RANGES = new Set(["crefrange", "Crefrange", "cpagerefrange", "Cpagerefrange"]);
  const ARGUMENTS = new Set(["documentclass", "usepackage", "RequirePackage", "include", "input", "bibliography", "bibliographystyle"]);
  const MATH_ENVS = new Set([
    "math", "displaymath", "equation", "equation*", "align", "align*", "alignat", "alignat*",
    "flalign", "flalign*", "gather", "gather*", "multline", "multline*", "eqnarray", "eqnarray*",
    "dmath", "dmath*", "IEEEeqnarray", "IEEEeqnarray*",
  ]);
  const TEXT = new Set(["text", "textrm", "textit", "textbf", "textsf", "texttt", "textup", "textnormal",
    "emph", "mbox", "hbox", "intertext", "shortintertext", "operatorname"]);
  const FORMAT = new Set(["frac", "dfrac", "tfrac", "cfrac", "sqrt", "binom", "dbinom", "tbinom",
    "left", "right", "middle", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr",
    "biggl", "biggr", "Biggl", "Biggr", "hat", "widehat", "bar", "overline", "underline", "vec",
    "dot", "ddot", "tilde", "widetilde", "overbrace", "underbrace", "overset", "underset", "stackrel",
    "mathrm", "mathit", "mathbf", "mathsf", "mathtt", "mathcal", "mathbb", "mathfrak", "mathnormal",
    "boldsymbol", "bm", "phantom", "hphantom", "vphantom", "displaystyle", "textstyle", "scriptstyle",
    "scriptscriptstyle", "limits", "nolimits", "substack", "tag", "notag", "nonumber"]);
  const SPACING = new Set(["quad", "qquad", "enspace", "thinspace", "medspace", "thickspace",
    "negthinspace", "negmedspace", "negthickspace", "hspace", "hskip", "vspace", "vskip", "kern", "mkern"]);
  const OPERATORS = new Set([
    "to", "mapsto", "longmapsto", "rightarrow", "leftarrow", "leftrightarrow", "Rightarrow", "Leftarrow",
    "Leftrightarrow", "longrightarrow", "longleftarrow", "longleftrightarrow", "Longrightarrow",
    "Longleftarrow", "Longleftrightarrow", "implies", "impliedby", "iff", "gets", "hookrightarrow",
    "le", "leq", "ge", "geq", "ne", "neq", "equiv", "sim", "simeq", "approx", "cong", "propto",
    "in", "notin", "ni", "subset", "subseteq", "supset", "supseteq", "parallel", "perp", "mid", "backslash",
    "oplus", "ominus", "otimes", "oslash", "times", "cdot", "div", "pm", "mp", "circ", "bullet",
    "cup", "cap", "setminus", "vee", "wedge", "land", "lor", "neg", "not", "forall", "exists", "nexists",
    "sum", "prod", "coprod", "int", "iint", "iiint", "oint", "bigcup", "bigcap", "bigoplus",
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
    "log", "ln", "exp", "lim", "limsup", "liminf", "sup", "inf", "max", "min", "det", "ker", "dim", "gcd", "supp",
  ]);
  const clone = (pending) => pending && { ...pending };
  function inMath(state) {
    if (!state.math.length) return false;
    for (let i = state.groups.length - 1; i >= 0; i--) {
      const group = state.groups[i];
      if (group.kind) return false;
      if (group.textDepth !== undefined) return state.math.length > group.textDepth;
    }
    return true;
  }
  function closeMath(state, close) {
    for (let i = state.math.length - 1; i >= 0; i--) {
      if (state.math[i] === close) { state.math.length = i; return; }
    }
  }

  CodeMirror.defineMode("prism-stex", () => {
    return {
      helperType: "stex", lineComment: "%",
      startState() { return { pending: null, groups: [], math: [], verbatim: null }; },
      copyState(state) {
        return { math: state.math.slice(), verbatim: state.verbatim, pending: clone(state.pending),
          groups: state.groups.map((g) => ({ ...g, resume: clone(g.resume) })) };
      },
      // A paragraph ends every argument (TeX: "Paragraph ended before … was complete"), so
      // one never closed, a \ref{ being typed, does not colour the rest of the document.
      blankLine(state) { state.pending = null; state.groups.length = 0; },
      token(stream, state) {
        if (state.verbatim) {
          if (stream.match("\\end{" + state.verbatim + "}", false)) state.verbatim = null;
          else {
            while (!stream.eol() && !stream.match("\\end{" + state.verbatim + "}", false)) stream.next();
            return "string";
          }
        }
        if (stream.eatSpace()) return null;
        if (stream.peek() === "%") { stream.skipToEnd(); return "comment"; }
        const parent = state.groups[state.groups.length - 1], math = inMath(state);
        // Keys and package/environment names are literal arguments, not formulas.
        if (parent && parent.kind && stream.match(/^[^\\{}\[\]%]+/)) {
          if (parent.environment) parent.value += stream.current();
          return parent.kind;
        }
        if (stream.match(/^\$\$?/) || stream.match(/^\\[()[\]]/)) {
          const delimiter = stream.current();
          if (delimiter === "$" || delimiter === "$$") {
            // A dollar inside \text{...} starts nested math even when the enclosing
            // formula also uses dollars. It must not close that outer formula.
            if (math && state.math[state.math.length - 1] === delimiter) state.math.pop();
            else state.math.push(delimiter);
          } else if (delimiter === "\\(" || delimiter === "\\[") state.math.push(delimiter);
          else closeMath(state, delimiter === "\\)" ? "\\(" : "\\[");
          state.pending = null;
          return "bracket";
        }
        if (stream.match(/^\\[a-zA-Z@]+/)) {
          const command = stream.current().slice(1);
          if (command === "verb") {
            stream.eat("*");
            const delimiter = stream.next();
            if (delimiter) { while (!stream.eol() && stream.next() !== delimiter) { /* literal */ } }
            state.pending = null;
            return "string";
          }
          const kind = command === "begin" || command === "end" ? "tex-environment"
            : REFERENCES.has(command) || command === "hyperref" ? "tex-reference"
            : ARGUMENTS.has(command) ? "tex-argument" : null;
          state.pending = kind || TEXT.has(command) ? { kind, remaining: RANGES.has(command) ? 2 : 1,
            optionalTarget: command === "hyperref", text: TEXT.has(command),
            environment: command === "begin" || command === "end" ? command : null } : null;
          if (math && SPACING.has(command)) return "tex-spacing";
          if (math && OPERATORS.has(command)) return "tex-math-operator";
          if (math && !kind && !TEXT.has(command) && !FORMAT.has(command)) return "tex-math-symbol";
          return "tag";
        }
        // Control symbols such as \% are commands, not comments or argument delimiters.
        if (stream.match(/^\\./)) {
          state.pending = null;
          if (math && /^\\[ ,;!:]$/.test(stream.current())) return "tex-spacing";
          return "bracket";
        }
        const text = stream.next();
        if (text === "*" && state.pending) return null;   // \ref*{key}, \cite*[note]{key}
        // [ is an optional argument only right after a command; elsewhere ($x\in[0,1)$,
        // [a,b[) it is text, and would never be closed.
        if (text === "[" && !state.pending) return "bracket";
        if (text === "{" || text === "[") {
          const pending = state.pending;
          const optional = text === "[";
          let kind = parent ? parent.kind : null, resume = null;
          if (pending) {
            kind = optional && !pending.optionalTarget && pending.kind !== "tex-argument" ? null : pending.kind;
            if (optional && !pending.optionalTarget) resume = pending;
            else if (pending.remaining > 1) resume = { ...pending, remaining: pending.remaining - 1 };
            // hyperref's key is in [...]; its {...} is ordinary link text.
            else if (pending.optionalTarget && !optional) kind = null;
          }
          state.groups.push({ close: optional ? "]" : "}", kind, resume,
            textDepth: pending && pending.text && !optional ? state.math.length : undefined,
            environment: pending && !optional ? pending.environment : null, value: "" });
          state.pending = null;
          return "bracket";
        }
        if (text === "}" || text === "]") {
          // A } also ends the [ … that are still open inside its group; a ] ends only its own.
          let top = state.groups.length - 1;
          if (text === "}") while (top >= 0 && state.groups[top].close === "]") top--;
          const group = state.groups[top];
          if (group && group.close === text) {
            state.groups.length = top; state.pending = group.resume;
            if (group.environment) {
              const env = group.value.trim();
              if (group.environment === "end") closeMath(state, "env:" + env);
              else if (MATH_ENVS.has(env) || inMath(state)) state.math.push("env:" + env);
              if (group.environment === "begin" && /^(verbatim\*?|lstlisting|minted)$/.test(env)) state.verbatim = env;
            }
          } else state.pending = null;
          return "bracket";
        }
        state.pending = null;
        if (math && /[\d.]/.test(text)) {
          stream.pos--;  // decimal constants are one token; subscripts remain separate.
          if (stream.match(/^(?:\d+\.?\d*|\.\d+)/)) return "number";
          stream.next();
        }
        // Literal operators and delimiters stay with the text. Reserve the accent
        // for named math commands (\int, \to, …), rather than every punctuation mark.
        if (math && /[_^&=+\-*/<>|:!~]/.test(text)) return null;
        if (math && /[a-zA-Z]/.test(text)) { stream.eatWhile(/[a-zA-Z]/); return "tex-math-variable"; }
        // Math punctuation is its own token. Eating letters after '(' or ','
        // would hide the identifier in '(t,v)' from its semantic colour.
        if (!math) stream.eatWhile(/[\w\-]/);
        return null;
      },
    };
  });
})();
