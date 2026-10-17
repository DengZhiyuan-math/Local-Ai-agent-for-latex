/* Search with the agent: what the agent is asked (prompt), what its answer proposes
   (parse), and what each proposal changes where in the project (changes). The agent only
   reads and proposes, in an Ask turn; you review each change; Prism applies the ones you
   keep. No page: app.js runs the turn and draws, these decide; checked under Node.

     const p = AgentSearch.parse(replyText)        // {summary, rules, edits, problems} or {error}
     AgentSearch.changes(texts, p)                 // texts: [[path, content], …] in the tree's order
       -> {files: [{path, changes: [{from, to, text, by, index, line, before, after}]}],
           problems: [why …], counts: {rules: [n …], edits: placed}} */
"use strict";

const AgentSearch = (() => {
  const FL = typeof module !== "undefined" ? require("./findlogic.js") : FindLogic;
  const FENCE = "```";
  const WHERE = { math: "math", text: "text", anywhere: "anywhere", all: "anywhere", everywhere: "anywhere" };

  // The message for the agent: the request, the project's files, and the answer's form.
  function prompt(request, { main = "", documentFiles = [] } = {}) {
    return String.raw`[prism-search] Find what the request below asks to change in this LaTeX project, and propose the changes. Do not change, create or delete any file: you only propose. The author reviews every change, and Prism applies the ones they keep.

Request: ${request}

The project's main file is ${main || "not known"}; the document's files, in order: ${documentFiles.join(", ") || "not known"}. Other text files may matter too: search the whole project.

End your answer with exactly one block in this form:

${FENCE}prism-search
{"summary": "...", "rules": [{"find": "...", "replace": "...", "where": "math", "caseSensitive": true, "note": "..."}], "edits": [{"file": "...", "line": 1, "old": "...", "new": "...", "note": "..."}]}
${FENCE}

- rules (preferred): "find" is a JavaScript regular expression; "replace" its replacement ($1, $2 … for groups, $& for the whole match, $$ for a dollar sign); "where" is "math" (only inside formulas: $…$, $$…$$, \(…\), \[…\], equation, align, gather, multline … environments), "text" (only outside formulas and comments) or "anywhere"; "note" says what the rule changes. Prism runs every rule on every text file of the project and shows each match for review. Prism sets the flags (global, multiline; case-insensitive when "caseSensitive" is false).
- Make each rule as narrow as you can: a change the author must untick is cheap, but a rule that matches unrelated text is worse than a few explicit edits. Before you answer, search the files with your rules and check what they match; say in the note what the author should look at.
- In JSON a backslash is written twice: the regular expression \\sum (matching \sum) is "\\\\sum", and \bi\b is "\\bi\\b".
- edits: only for places no rule can express. "file" is the project-relative path, "line" the line number from 1, "old" the exact text on that line to replace (long enough to occur only once on that line), "new" its replacement.
- summary: what you propose, and what you left out on purpose and why, in the language of the request. If nothing should change, give empty lists and say why.`;
  }

  // The proposal in an answer: its last prism-search block (else the last JSON block that
  // reads, else the whole text). What cannot be used is said in `problems`, the rest kept.
  function parse(text) {
    const blocks = [...String(text || "").matchAll(/```([\w-]*)[ \t]*\r?\n([\s\S]*?)```/g)];
    const tagged = blocks.filter((b) => b[1] === "prism-search");
    const candidates = tagged.length ? [tagged[tagged.length - 1][2]]
      : [...blocks.filter((b) => !b[1] || b[1] === "json").map((b) => b[2]).reverse(), text];
    let data = null, error = "The agent's answer has no prism-search block.";
    for (const c of candidates) {
      try { data = JSON.parse(c); break; } catch (e) { if (tagged.length) error = `The agent's proposal is not valid JSON: ${e.message}`; }
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) return { error };
    const problems = [], rules = [], edits = [];
    (Array.isArray(data.rules) ? data.rules : []).forEach((r, i) => {
      const n = `Rule ${i + 1}`;
      if (!r || typeof r.find !== "string" || !r.find) return problems.push(`${n} has no expression to find.`);
      if (typeof r.replace !== "string") return problems.push(`${n} has no replacement.`);
      const where = WHERE[String(r.where || "anywhere").toLowerCase()];
      if (!where) problems.push(`${n}: "${r.where}" is no place; it applies anywhere.`);
      const caseSensitive = r.caseSensitive !== false && r.case !== false;
      if (FL.pattern({ query: r.find, regex: true, caseSensitive }).error)
        return problems.push(`${n} is not a valid regular expression: ${r.find}`);
      rules.push({ find: r.find, replace: r.replace, where: where || "anywhere", caseSensitive, note: String(r.note || "") });
    });
    (Array.isArray(data.edits) ? data.edits : []).forEach((e, i) => {
      const n = `Change ${i + 1}`;
      if (!e || typeof e.file !== "string" || !Number.isInteger(e.line) || e.line < 1
          || typeof e.old !== "string" || !e.old || typeof e.new !== "string")
        return problems.push(`${n} does not say which file, line and text.`);
      edits.push({ file: e.file.replace(/^\.\//, ""), line: e.line, old: e.old, new: e.new, note: String(e.note || "") });
    });
    return { summary: String(data.summary || ""), rules, edits, problems };
  }

  // The kinds of file whose formulas are known; in others a rule for "math" finds nothing.
  const hasMath = (path) => /\.(tex|ltx|sty|cls|md|tikz)$/i.test(path);

  // What the proposal changes in each file: the rules' matches (where they may be) and the
  // edits that are where they say, in order, one change per place (an edit before a rule's
  // match on the same text). Changes that change nothing are left out.
  function changes(texts, proposal) {
    const problems = [], counts = { rules: proposal.rules.map(() => 0), edits: 0 };
    const known = new Set(texts.map(([p]) => p));
    proposal.edits.forEach((e, i) => { if (!known.has(e.file)) problems.push(`Change ${i + 1}: ${e.file} is not a text file of this project.`); });
    const files = [];
    for (const [path, content] of texts) {
      const regs = hasMath(path) ? FL.regions(content) : { comments: [], math: [[0, 0]] };
      const found = [];
      proposal.rules.forEach((r, k) => {
        const list = FL.matches(content, { query: r.find, regex: true, caseSensitive: r.caseSensitive }, Infinity).list;
        for (const x of FL.scoped(list, regs, r.where)) {
          const text = FL.expand(r.replace, x.m, { regex: true });
          if (text !== content.slice(x.from, x.to)) found.push({ from: x.from, to: x.to, text, by: "rule", index: k });
        }
      });
      let starts = null;
      proposal.edits.forEach((e, k) => {
        if (e.file !== path) return;
        starts = starts || lineStarts(content);
        if (e.line > starts.length) return problems.push(`Change ${k + 1}: ${path} has no line ${e.line}.`);
        const a = starts[e.line - 1], lineText = content.slice(a, e.line < starts.length ? starts[e.line] - 1 : content.length).replace(/\r$/, "");
        const at = lineText.indexOf(e.old);
        if (at === -1) return problems.push(`Change ${k + 1}: line ${e.line} of ${path} does not have “${e.old}”.`);
        if (lineText.indexOf(e.old, at + 1) !== -1) return problems.push(`Change ${k + 1}: “${e.old}” is more than once on line ${e.line} of ${path}.`);
        if (e.old !== e.new) found.push({ from: a + at, to: a + at + e.old.length, text: e.new, by: "edit", index: k });
      });
      found.sort((x, y) => x.from - y.from || (x.by === "edit" ? -1 : 1));
      const kept = [];
      for (const c of found) {
        const last = kept[kept.length - 1];
        if (last && c.from < last.to) {
          if (c.by === "edit" && last.by === "rule") kept[kept.length - 1] = c;
          continue;
        }
        kept.push(c);
      }
      if (!kept.length) continue;
      const where = FL.lines(content, kept);
      kept.forEach((c, i) => {
        if (c.by === "rule") counts.rules[c.index]++; else counts.edits++;
        const w = where[i], end = Math.min(w.ch + w.len, w.text.length);
        Object.assign(c, { line: w.line, before: w.text, after: w.text.slice(0, w.ch) + c.text + w.text.slice(end), ch: w.ch });
      });
      files.push({ path, changes: kept });
    }
    return { files, problems, counts };
  }

  function lineStarts(text) {
    const s = [0];
    for (let i = text.indexOf("\n"); i !== -1; i = text.indexOf("\n", i + 1)) s.push(i + 1);
    return s;
  }

  // `content` with the given changes (in order, not overlapping) made.
  function apply(content, list) {
    let out = "", at = 0;
    for (const c of list) { out += content.slice(at, c.from) + c.text; at = c.to; }
    return out + content.slice(at);
  }

  return { prompt, parse, changes, apply };
})();

if (typeof module !== "undefined") module.exports = AgentSearch;
