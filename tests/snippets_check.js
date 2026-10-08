// Checks of prism_local/static/snippets.js under Node (run by tests/test_snippets.py).
// Typing is played through the same steps the editor takes: the context at the cursor,
// the first snippet that matches, its tabstops.
"use strict";
const assert = require("assert");
const S = require("../prism_local/static/snippets.js");

const snips = S.compile(S.DEFAULTS, 0);
const failures = [];
function check(name, fn) {
  try { fn(); } catch (e) { failures.push(`${name}: ${e.message}`); }
}

// The context at the end of `doc` (the cursor is at its end).
function ctx(doc) {
  const lines = doc.split("\n");
  const st = [];
  for (const l of lines.slice(0, -1)) S.scanLine(st, l);
  const last = lines[lines.length - 1];
  return S.contextAt(st, last, last.length);
}

// Type `keys` one at a time at the end of `doc`, expanding auto snippets as the editor
// does; "\t" is Tab (expand a Tab snippet, else jump to the next tabstop). Returns the
// text with "|" at the cursor.
function type(doc, keys) {
  let text = doc, cur = doc.length, stops = [], idx = -1, fresh = false;
  const select = (i) => { idx = i; cur = stops[i].to; fresh = true; };
  const expandAt = (auto) => {
    const before = text.slice(0, cur);
    const from = before.lastIndexOf("\n", before.lastIndexOf("\n") - 1) + 1;
    const c = ctx(before);
    if (c.mode === "raw") return false;
    const m = S.match(snips, before.slice(from), c.mode, auto);
    if (!m) return false;
    const indent = /^\s*/.exec(before.slice(before.lastIndexOf("\n") + 1))[0];
    const p = S.parse(m.text, indent);
    const start = from + m.start;
    text = text.slice(0, start) + p.text + text.slice(cur);
    const byN = new Map();
    for (const s of p.stops) if (!byN.has(s.n)) byN.set(s.n, { from: start + s.from, to: start + s.to });
    stops = [...byN.keys()].sort((a, b) => a - b).map((k) => byN.get(k));
    if (stops.length) select(0); else cur = start + p.text.length;
    return true;
  };
  for (const k of keys) {
    if (k === "\t") {
      if (expandAt(false)) continue;
      if (idx >= 0 && idx < stops.length - 1) { select(idx + 1); continue; }
      const skip = S.tabout(text.slice(cur).split("\n")[0]);
      if (skip >= 0) cur += skip;
      continue;
    }
    // A placeholder that is selected is typed over.
    if (fresh && stops[idx].from < stops[idx].to) {
      const s = stops[idx], d = s.to - s.from;
      text = text.slice(0, s.from) + text.slice(s.to);
      cur = s.from;
      for (const t of stops) { if (t.from > s.from) t.from -= d; if (t.to > s.from) t.to -= d; }
      s.to = s.from;
    }
    fresh = false;
    text = text.slice(0, cur) + k + text.slice(cur);
    for (const t of stops) { if (t.from > cur) t.from++; if (t.to >= cur) t.to++; }
    cur++;
    if (!expandAt(true) && k === "/") {
      const before = text.slice(0, cur), c = ctx(before);
      if (c.mode === "M" || c.mode === "n") {
        const lineStart = before.lastIndexOf("\n") + 1;
        const t = S.fractionTerm(before.slice(lineStart, -1));
        if (t) {
          const p = S.parse("\\frac{" + t.term + "}{$0}$1", "");
          const start = lineStart + t.start;
          text = text.slice(0, start) + p.text + text.slice(cur);
          stops = p.stops.map((s) => ({ from: start + s.from, to: start + s.to }));
          select(0);
        }
      }
    }
  }
  return text.slice(0, cur) + "|" + text.slice(cur);
}

// ---------------------------------------------------------------- where math is
check("text and inline math", () => {
  assert.strictEqual(ctx("Let ").mode, "t");
  assert.strictEqual(ctx("Let $x").mode, "n");
  assert.strictEqual(ctx("Let $x$ and").mode, "t");
  assert.strictEqual(ctx("costs \\$5 and $").mode, "n");
  assert.strictEqual(ctx("\\(a").mode, "n");
  assert.strictEqual(ctx("\\(a\\) b").mode, "t");
});
check("display math", () => {
  assert.strictEqual(ctx("$$x").mode, "M");
  assert.strictEqual(ctx("\\[\n x").mode, "M");
  assert.strictEqual(ctx("\\[ x \\]\n").mode, "t");
  assert.strictEqual(ctx("\\begin{equation}\n  a").mode, "M");
  assert.strictEqual(ctx("\\begin{align*}\n  a &= b\n\\end{align*}\n").mode, "t");
  assert.strictEqual(ctx("\\begin{theorem}\n  Let").mode, "t");
});
check("environments inside math", () => {
  const c = ctx("\\begin{equation}\n\\begin{pmatrix}\n 1 & 2");
  assert.strictEqual(c.mode, "M"); assert.strictEqual(c.env, "pmatrix");
  assert.strictEqual(ctx("\\begin{equation}\n\\begin{pmatrix} 1 \\end{pmatrix} x").env, "equation");
  assert.strictEqual(ctx("\\begin{align}\n a").env, "align");
});
check("text inside math, and arguments with no snippets", () => {
  assert.strictEqual(ctx("$x \\text{for all").mode, "t");
  assert.strictEqual(ctx("$x \\text{for $y").mode, "n");
  assert.strictEqual(ctx("$x \\text{a} + y").mode, "n");
  assert.strictEqual(ctx("\\begin{equation}\\label{eq:inner").mode, "raw");
  assert.strictEqual(ctx("see \\cite[p.~2]{knuth").mode, "raw");
  assert.strictEqual(ctx("\\begin{ali").mode, "raw");
  assert.strictEqual(ctx("$x$ % a comment $y").mode, "raw");
  assert.strictEqual(ctx("100\\% sure $x").mode, "n");
  assert.strictEqual(ctx("a \\\\ $x").mode, "n");
});

// ---------------------------------------------------------------- typing
const cases = [
  // [document, keys, expected]
  ["$", "@a", "$\\alpha|"],
  ["$", "alpha", "$\\alpha|"],
  ["$x", "sr", "$x^{2}|"],
  ["$", "x2", "$x_{2}|"],
  ["$x_{2}", "3", "$x_{23}|"],
  ["$", "xhat", "$\\hat{x}|"],
  ["$", "cdot", "$\\cdot|"],
  ["$", "ddot", "$\\ddot{|}"],
  ["$", "RR", "$\\mathbb{R}|"],
  ["$", "a<= b", "$a\\leq b|"],
  ["$", "sin", "$\\sin|"],
  ["$", "->", "$\\to|"],
  ["$", "//a\tb\t", "$\\frac{a}{b}|"],
  ["$", "1/2\t", "$\\frac{1}{2}|"],
  ["$", "(a+b)/c", "$\\frac{a+b}{c|}"],
  ["$", "e^{x}/2", "$\\frac{e^{x}}{2|}"],
  ["$", "sq", "$\\sqrt{|}"],
  ["$", "sum", "$\\sum_{i=1|}^{n} "],
  ["$", "lr(x\t", "$\\left( x \\right) |"],
  ["$", "dint", "$\\int_{0|}^{1}  \\, dx "],
  ["$", "par\t", "$\\frac{\\partial y|}{\\partial x} "],
  ["", "mk", "$|$"],
  ["Let", " mk", "Let $|$"],
  ["", "beg\tproof", "\\begin{proof|}\n\t\n\\end{}"],
  // Words and commands are left alone.
  ["The ", "begin", "The begin|"],
  ["", "admin", "admin|"],
  ["$", "\\sum", "$\\sum|"],
  ["$", "\\alpha", "$\\alpha|"],
  ["$", "epsilon", "$\\epsilon|"],
  ["$", "zeta", "$\\zeta|"],
  ["$x \\text{", "and inn", "$x \\text{and inn|"],
  ["\\begin{equation}\\label{", "eq:inner", "\\begin{equation}\\label{eq:inner|"],
  ["\\begin{equation}\n", "a and b", "\\begin{equation}\na \\cap b|"],
  ["Text sr and", " RR", "Text sr and RR|"],
];
for (const [doc, keys, want] of cases) {
  check(`typing ${JSON.stringify(keys)} after ${JSON.stringify(doc)}`, () => assert.strictEqual(type(doc, keys), want));
}

// ---------------------------------------------------------------- the pieces
check("tabstops", () => {
  assert.deepStrictEqual(S.parse("\\frac{$0}{$1}$2", ""), {
    text: "\\frac{}{}", stops: [{ n: 0, from: 6, to: 6 }, { n: 1, from: 8, to: 8 }, { n: 2, from: 9, to: 9 }] });
  const p = S.parse("\\lim_{${0:n} \\to ${1:\\infty}}", "");
  assert.strictEqual(p.text, "\\lim_{n \\to \\infty}");
  assert.deepStrictEqual(p.stops, [{ n: 0, from: 6, to: 7 }, { n: 1, from: 12, to: 18 }]);
  assert.strictEqual(S.parse("$$0$", "").text, "$$");
  assert.strictEqual(S.parse("a\nb", "  ").text, "a\n  b");
});
check("captures and selections are text, not tabstops", () => {
  const t = S.expandText("$${VISUAL}$", null, [], "2x");
  assert.strictEqual(S.parse(t, "").text, "$2x$");
  assert.deepStrictEqual(S.parse(t, "").stops, []);
  assert.strictEqual(S.parse(S.expandText("\\hat{[[0]]}", null, ["$1"], null), "").text, "\\hat{$1}");
});
check("visual snippets", () => {
  assert.strictEqual(S.visualMatch(snips, "S", "n").replacement, "\\sqrt{${VISUAL}}");
  assert.strictEqual(S.visualMatch(snips, "S", "t"), null);
  assert.strictEqual(S.visualMatch(snips, "E", "t").replacement, "\\emph{${VISUAL}}");
  // ${VISUAL} makes a snippet a selection one even without v (obsidian-latex-suite files do this),
  // so typing the letter never inserts "${VISUAL}".
  const mine = S.compile([{ trigger: "S", replacement: "\\sqrt{ ${VISUAL} }", options: "mA" }], 0);
  assert.strictEqual(S.match(mine, "$S", "n", true), null);
  assert.strictEqual(S.visualMatch(mine, "S", "n").replacement, "\\sqrt{ ${VISUAL} }");
});
check("function replacements", () => {
  const m = S.match(snips, "$iden2", "M", true);
  assert.strictEqual(m.text, "\\begin{pmatrix}\n\t1 & 0 \\\\\n\t0 & 1\n\\end{pmatrix}");
});
check("tabout", () => {
  assert.strictEqual(S.tabout("} + 1"), 1);
  assert.strictEqual(S.tabout(" b \\right) c"), 10);
  assert.strictEqual(S.tabout("x \\rangle"), 9);
  assert.strictEqual(S.tabout("$ and"), 1);
  assert.strictEqual(S.tabout(" plain"), -1);
});
check("fraction terms", () => {
  assert.deepStrictEqual(S.fractionTerm("a + 2x"), { start: 4, term: "2x" });
  assert.deepStrictEqual(S.fractionTerm("(a+b)"), { start: 0, term: "a+b" });
  assert.deepStrictEqual(S.fractionTerm("(a)(b)"), { start: 0, term: "(a)(b)" });
  assert.strictEqual(S.fractionTerm("a + "), null);
  assert.strictEqual(S.fractionTerm("\\sum"), null);
});
check("your snippets file", () => {
  assert.deepStrictEqual(S.load("[{trigger: 'a', replacement: 'b', options: 'mA'}]").list.length, 1);
  assert.strictEqual(S.load("export default [{trigger: 'a', replacement: 'b'}];").defaults, true);
  assert.strictEqual(S.load("({snippets: [], defaults: false})").defaults, false);
  assert.strictEqual(S.load("").list.length, 0);
  assert.strictEqual(S.load(S.TEMPLATE).list.length, 0);
  assert.throws(() => S.load("42"));
});

if (failures.length) {
  console.log(failures.join("\n"));
  process.exit(1);
}
console.log("ok");
