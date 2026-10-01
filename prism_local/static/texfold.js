/* Code folding for LaTeX (CodeMirror's fold addon calls this for the stex mode).

   A fold starts on the line of
   - a sectioning command (\part … \subparagraph): it runs to the next heading of the same
     or a higher level, or to \appendix, the bibliography or \end{document};
   - \begin{env}: it runs to the matching \end{env} (equation, proof, itemize, …);
   - \[ : it runs to the matching \].
   Comments are ignored, so a commented-out \end does not end anything. */
"use strict";

(() => {
  const LEVEL = { part: 0, chapter: 1, section: 2, subsection: 3, subsubsection: 4, paragraph: 5, subparagraph: 6 };
  const HEADING = /^\s*\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\s*[[{]/;
  // Where every section stops: what follows is not part of the last section.
  const STOP = /^\s*\\(?:appendix\b|end\{document\}|bibliography\{|printbibliography\b|begin\{thebibliography\})/;
  const ENV = /\\(begin|end)\s*\{([^{}]+)\}/g;

  // The line without its comment (an unescaped %), as long as the original up to there.
  const code = (text) => {
    for (let i = 0; i < text.length; i++) {
      if (text[i] === "\\") i++;
      else if (text[i] === "%") return text.slice(0, i);
    }
    return text;
  };

  function section(cm, line, level) {
    const last = cm.lastLine();
    let end = line;
    for (let l = line + 1; l <= last; l++) {
      const t = code(cm.getLine(l));
      const h = HEADING.exec(t);
      if ((h && LEVEL[h[1]] <= level) || STOP.test(t)) break;
      if (t.trim()) end = l;
    }
    if (end === line) return null;
    return { from: CodeMirror.Pos(line, cm.getLine(line).length), to: CodeMirror.Pos(end, cm.getLine(end).length) };
  }

  // The first \begin{env} on `line` whose \end{env} is on a later line.
  function environment(cm, line) {
    const first = code(cm.getLine(line));
    const opens = [];
    for (const m of first.matchAll(ENV)) {
      if (m[1] === "begin") opens.push(m[2]);
      else if (opens.length && opens[opens.length - 1] === m[2]) opens.pop();
    }
    if (!opens.length) return null;
    // Look for the end of the outermost environment still open at the end of the line.
    const name = opens[0];
    let depth = opens.filter((n) => n === name).length;
    const last = Math.min(cm.lastLine(), line + 5000);
    for (let l = line + 1; l <= last; l++) {
      for (const m of code(cm.getLine(l)).matchAll(ENV)) {
        if (m[2] !== name) continue;
        depth += m[1] === "begin" ? 1 : -1;
        if (depth === 0) return { from: CodeMirror.Pos(line, cm.getLine(line).length), to: CodeMirror.Pos(l, m.index) };
      }
    }
    return null;
  }

  function displayMath(cm, line) {
    const t = code(cm.getLine(line));
    const open = t.lastIndexOf("\\["), close = t.lastIndexOf("\\]");
    if (open < 0 || close > open) return null;
    const last = Math.min(cm.lastLine(), line + 500);
    for (let l = line + 1; l <= last; l++) {
      const i = code(cm.getLine(l)).indexOf("\\]");
      if (i >= 0) return { from: CodeMirror.Pos(line, cm.getLine(line).length), to: CodeMirror.Pos(l, i) };
    }
    return null;
  }

  CodeMirror.registerHelper("fold", "stex", (cm, start) => {
    const text = code(cm.getLine(start.line));
    const h = HEADING.exec(text);
    if (h) return section(cm, start.line, LEVEL[h[1]]);
    return environment(cm, start.line) || displayMath(cm, start.line);
  });
})();
