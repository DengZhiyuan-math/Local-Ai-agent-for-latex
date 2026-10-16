// Every page loads without an error, and what util.js now provides works on it.
// usage: node e2e_pages.mjs <editor url> <home url>
const [editor, home] = process.argv.slice(2);
const results = [];
const check = (name, ok, detail = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);

async function open(url) {
  const tab = await (await fetch("http://127.0.0.1:9333/json/new?" + encodeURIComponent(url), { method: "PUT" })).json();
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  let id = 0;
  const pending = new Map(), errors = [];
  ws.addEventListener("message", (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
    if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  });
  const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  await send("Runtime.enable");
  await send("Page.enable");
  await send("Page.reload");                     // errors thrown while loading are seen
  await new Promise((r) => setTimeout(r, 2500));
  const ev = async (expr) => {
    const r = await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
    if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || "evaluate failed");
    return r.result.result.value;
  };
  // Its page goes too when done: an open page holds connections the next one needs.
  return { ev, send, errors, close: () => { ws.close(); return fetch("http://127.0.0.1:9333/json/close/" + tab.id); } };
}

const ed = await open(editor);
check("editor loads", ed.errors.length === 0, ed.errors.join(" | "));
check("theme button cycles", await ed.ev(`
  const b = document.querySelector("#btn-theme"), seen = [];
  for (let i = 0; i < 3; i++) { b.click(); seen.push(document.documentElement.dataset.theme || "system"); }
  return seen.join(",") === "dark,light,system";`), "");
check("diff panel colours lines", await ed.ev(`
  $("#diff").innerHTML = diffHtml("+a\\n-b");
  return document.querySelectorAll("#diff .add, #diff .del").length === 2;`));
const syntax = await ed.ev(`
  const mode = CodeMirror.getMode({ indentUnit: 2 }, modeFor("main.tex"));
  function tokens(text) {
    const state = CodeMirror.startState(mode), out = [];
    for (const line of text.split("\\n")) {
      if (!line) { if (mode.blankLine) mode.blankLine(state); continue; }
      const stream = new CodeMirror.StringStream(line);
      while (!stream.eol()) {
        stream.start = stream.pos;
        const type = mode.token(stream, state);
        if (stream.current().trim()) out.push([stream.current(), type]);
      }
    }
    return out;
  }
  const simple = tokens(String.raw\`\\begin{equation} \\label{eq:ridge} x=1 \\end{equation}\`);
  const has = (out, text, type) => out.some(t => t[0] === text && t[1] === type);
  const styled = (out, type) => out.filter(t => t[1] === type).map(t => t[0]).join("");
  const math = tokens(String.raw\`$ \\eqref{eq:ridge} + \\alpha $\`);
  const cite = tokens(String.raw\`\\citep*[see][p.~2]{Smith-2024,Jones2025}\`);
  const multiline = tokens("\\\\ref % note\\n [see]\\n {eq:ridge}");
  const range = tokens(String.raw\`\\crefrange{eq:first}{eq:last}\`);
  const ordinary = tokens(String.raw\`\\documentclass[UTF8]{ctexart} \\usepackage{amsmath} \\section{Title} body\`);
  const categories = modeFor("notes.md") === "markdown" && mode.helperType === "stex"
    && has(simple, "\\\\begin", "tag") && has(simple, "equation", "tex-environment")
    && styled(simple, "tex-reference") === "eq:ridge" && has(simple, "x", "tex-math-variable")
    && styled(math, "tex-reference") === "eq:ridge" && has(math, "\\\\alpha", "tex-math-symbol")
    && styled(cite, "tex-reference") === "Smith-2024,Jones2025"
    && styled(multiline, "tex-reference") === "eq:ridge"
    && styled(range, "tex-reference") === "eq:firsteq:last"
    && !ordinary.some(t => t[1] === "tex-environment" || t[1] === "tex-reference");
  const formula = tokens(String.raw\`With Section~\\ref{sec:model}: $A=\\{a_t=e^{ta_1}\\}$,
    $\\g=\\nn\\oplus\\mathfrak p^-$ and $\\pr_\\nn:\\g\\to\\nn$.
    \\begin{equation*}
    \\begin{gathered}
    \\text{ND (verified here)}+\\text{Shah's theorem (external)}\\ \\Longrightarrow\\ \\text{(1)};\\\\
    \\end{gathered}
    \\end{equation*}
    Outside \\nn and 42.\`);
  const boundary = tokens(String.raw\`\\[x_2+\\alpha=\\frac{1}{2}\\] Outside \\alpha
    \\(x+\\beta\\) and $$x+\\gamma$$.\`);
  const prose = tokens(String.raw\`$\\text{word_1=2 \\% \\alpha {nested_3} $\\beta+4$ after_5} + 6$ end\`);
  const continued = tokens(String.raw\`\\begin{align}
    x&=\\alpha+1 % \\end{align} $ fake comment

    y&=\\beta+2
    \\end{align}
    Outside \\alpha\`);
  const literal = tokens(String.raw\`\\verb|$\\alpha_2$| \\begin{verbatim}
    $\\alpha_2$
    \\end{verbatim}
    $\\beta+3$\`);
  const setting = tokens(String.raw\`Throughout, G.
    \\begin{equation*}
    G=\\SO(3,1)^\\circ,\\qquad T=a_1+Z+n_1,\\qquad H=\\{h_s=e^{sT}:s\\in\\R\\},\\qquad
    L=M\\cong\\SO(2),
    \\end{equation*}
    where the matrices $a_1,Z,n_1$ and the subgroups $A,M,P^-$ are defined.
    \\begin{equation*}
    \\supp\\varphi\\subseteq[\\tfrac14,\\tfrac34].
    \\end{equation*}\`);
  const punctuated = tokens(String.raw\`$J_H(t,v,\\varphi)=\\int_\\R\\varphi(s)\\,\\pi(h_sa_{-t})v\\,ds,$ Plain text\`);
  const first = CodeMirror.startState(mode), openGroup = new CodeMirror.StringStream(String.raw\`$\\text{\`);
  while (!openGroup.eol()) { openGroup.start = openGroup.pos; mode.token(openGroup, first); }
  const copy = CodeMirror.copyState(mode, first), closeGroup = new CodeMirror.StringStream('} $');
  while (!closeGroup.eol()) { closeGroup.start = closeGroup.pos; mode.token(closeGroup, copy); }
  return {
    categories,
    formula: has(formula, "\\\\nn", "tex-math-symbol") && has(formula, "\\\\oplus", "tex-math-operator")
      && has(formula, "\\\\mathfrak", "tag") && has(formula, "_", null)
      && has(formula, "1", "number") && has(formula, "\\\\Longrightarrow", "tex-math-operator")
      && has(formula, "\\\\nn", "tag") && has(formula, "equation*", "tex-environment"),
    delimiters: has(boundary, "\\\\alpha", "tex-math-symbol") && has(boundary, "\\\\alpha", "tag")
      && has(boundary, "\\\\frac", "tag") && has(boundary, "\\\\beta", "tex-math-symbol")
      && has(boundary, "\\\\gamma", "tex-math-symbol") && has(boundary, "2", "number"),
    text: styled(prose, "number") === "46" && has(prose, "\\\\alpha", "tag")
      && has(prose, "\\\\beta", "tex-math-symbol") && !prose.some(t => t[1] === "comment"),
    multiline: has(continued, "\\\\beta", "tex-math-symbol") && has(continued, "\\\\alpha", "tag")
      && continued.some(t => t[1] === "comment"),
    literal: !has(literal, "\\\\alpha", "tex-math-symbol") && has(literal, "\\\\beta", "tex-math-symbol"),
    variables: has(setting, "G", "tex-math-variable") && has(setting, "G", null)
      && has(setting, "\\\\SO", "tex-math-symbol") && has(setting, "\\\\cong", "tex-math-operator")
      && has(setting, "\\\\supp", "tex-math-operator") && has(setting, "\\\\qquad", "tex-spacing")
      && !styled(prose, "tex-math-variable").includes("word"),
    punctuation: ["t", "v", "s", "h", "ds"].every(name => has(punctuated, name, "tex-math-variable"))
      && !punctuated.some(t => /^[(,][a-z]/.test(t[0]))
      && punctuated.filter(t => t[1] === "tex-math-operator").every(t => /^\\\\[a-zA-Z]+$/.test(t[0]))
      && has(punctuated, "$", "bracket") && has(punctuated, "_", null) && has(punctuated, "=", null),
    copy: first.math.length === 1 && copy.math.length === 0 && first.groups.length === 1 && copy.groups.length === 0,
  };`);
for (const [key, name] of Object.entries({
  categories: "LaTeX distinguishes commands, environments and reference keys",
  formula: "real formulas separate symbol macros, operators, formatting and numbers",
  delimiters: "inline and display delimiters restore ordinary text colours",
  text: "text arguments stay neutral and can contain nested inline math",
  multiline: "math environments survive comments, nested environments and blank lines",
  literal: "verbatim content cannot change subsequent math highlighting",
  variables: "math identifiers have their own style while prose and spacing stay separate",
  punctuation: "variables stay separate and literal punctuation is not highlighted as a command",
  copy: "incremental parser state copies do not mutate their source",
})) check(name, syntax[key]);
check("editor palette follows explicit and system themes without losing category colours", await ed.ev(`
  const savedTheme = document.documentElement.dataset.theme || null;
  const sample = document.createElement("div");
  sample.className = "CodeMirror cm-s-default";
  sample.innerHTML = '<span class="cm-tag">command</span><span class="cm-tex-environment">environment</span>'
    + '<span class="cm-tex-reference">reference</span><span class="cm-tex-math-symbol">symbol</span>'
    + '<span class="cm-tex-math-operator">operator</span><span class="cm-number">1</span>'
    + '<span class="cm-tex-math-variable">variable</span><span class="cm-bracket">{}</span>'
    + '<span class="cm-agent-del">removed</span>';
  document.body.append(sample);
  try {
    const palettes = [];
    for (const theme of ["light", "dark"]) {
      applyTheme(theme);
      const colours = [...sample.querySelectorAll("span")].slice(0, 6).map(el => getComputedStyle(el).color);
      const hsv = colour => {
        const [r, g, b] = colour.match(/[\\d.]+/g).slice(0, 3).map(Number);
        const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
        let hue = !d ? 0 : max === r ? (g-b)/d : max === g ? (b-r)/d+2 : (r-g)/d+4;
        return { hue: (hue * 60 + 360) % 360, saturation: max ? d/max : 0 };
      };
      const command = hsv(colours[0]), symbol = hsv(colours[3]);
      const hueGap = Math.abs(command.hue-symbol.hue);
      if (Math.min(hueGap, 360-hueGap) < 100 || Math.min(command.saturation, symbol.saturation) < .5) return false;
      const syntaxWeight = parseInt(getComputedStyle(sample.querySelector(".cm-tag")).fontWeight, 10);
      const deleted = getComputedStyle(sample.querySelector(".cm-agent-del"));
      const styles = getComputedStyle(sample);
      if (new Set(colours).size < 5 || syntaxWeight !== 400 || styles.fontSize !== "16px" || parseFloat(styles.lineHeight) < 28
          || deleted.opacity !== "1" || deleted.textDecorationLine !== "none") return false;
      for (const cls of ["cm-tex-math-variable", "cm-bracket"]) {
        const neutral = getComputedStyle(sample.querySelector("."+cls));
        if (neutral.color !== styles.color || neutral.fontWeight !== "400") return false;
      }
      palettes.push(colours.join("|"));
    }
    applyTheme(null);
    const system = [...sample.querySelectorAll("span")].slice(0, 6).map(el => getComputedStyle(el).color).join("|");
    return palettes[0] !== palettes[1] && system === palettes[matchMedia("(prefers-color-scheme: dark)").matches ? 1 : 0];
  } finally { sample.remove(); applyTheme(savedTheme); }`));
check("sync menu drops a stale GitHub link", await ed.ev(`
  S.githubUrl = "https://github.com/owner/tool"; $("#sync-github").href = S.githubUrl;
  renderSync({ own: true, enabled: true, remote: true, state: "idle", target: null,
               bound_target: { repository_identity: "github.com/owner/paper", branch_ref: "refs/heads/main" },
               blocked_reason: "Fetch and push differ" });
  return S.githubUrl === "" && $("#sync-github").hidden && !$("#sync-github").hasAttribute("href")
    && $("#sync-label").textContent === "Sync paused" && $("#sync-detail").innerHTML.includes("github.com/owner/paper");`));
check("completion of a label", await ed.ev(`
  for (let i = 0; i < 50 && !S.tabs.length; i++) await new Promise((r) => setTimeout(r, 100));
  S.symbols = { ...S.symbols, labels: [{ label: "thm:main", kind: "theorem", file: "main.tex", line: 1 }], env_titles: { theorem: "Theorem" } };
  cm.setValue("see \\\\cref{thm"); cm.setCursor({ line: 0, ch: 13 });
  const h = computeHints(cm, false);
  return !!h && h.list.length === 1 && h.list[0].text === "thm:main" && h.from.ch === 10;`));
check("an argument or bracket never closed does not colour what follows", await ed.ev(`
  const mode = CodeMirror.getMode({ indentUnit: 2 }, "prism-stex"), state = CodeMirror.startState(mode);
  const kinds = (line) => {
    const stream = new CodeMirror.StringStream(line), out = [];
    while (!stream.eol()) { stream.start = stream.pos; const t = mode.token(stream, state); if (stream.current().trim()) out.push(t); }
    return out;
  };
  for (let i = 0; i < 200; i++) kinds("$x\\\\in[0,1)$ and $[a,b[$");
  kinds("see \\\\ref{eq:"); mode.blankLine(state);
  const after = kinds("Plain words");
  kinds("\\\\cite[see]{key} \\\\section{On $[0,1)$}");
  return state.groups.length === 0 && after.every((t) => t !== "tex-reference");`));
check("a line the agent changed gets its class on the line itself, not through :has()", await ed.ev(`
  const t = activeTab(); AH.set(t.path, "changed", 0, 0);
  const wrap = cm.getLineHandle(0).wrapClass || "";
  AH.clear();
  const css = [...document.styleSheets].flatMap((s) => { try { return [...s.cssRules]; } catch { return []; } }).map((r) => r.cssText).join("");
  return wrap.includes("cm-agent-changed-w") && !(cm.getLineHandle(0).wrapClass || "").includes("cm-agent")
    && !css.includes(":has(> .cm-agent");`));
// Typed through an input method (a Chinese one, say), each key comes as a composition.
await ed.ev(`SNIP_SET.on = true; cm.setValue(""); cm.focus(); cm.setCursor({ line: 0, ch: 0 });`);
for (const key of "mk@a") {
  await ed.send("Input.imeSetComposition", { text: key, selectionStart: 1, selectionEnd: 1 });
  await ed.send("Input.insertText", { text: key });
}
check("snippets expand when an input method commits the keys", await ed.ev(`
  await new Promise((r) => setTimeout(r, 100));
  return cm.getValue() === "$\\\\alpha$";`), await ed.ev(`return cm.getValue()`));
check("a reply's math is KaTeX, with the project's macros, whatever Markdown makes of it", await ed.ev(`
  S.symbols = { ...S.symbols, katex_macros: { "\\\\norm": "\\\\left\\\\|#1\\\\right\\\\|" } };
  const box = document.createElement("div");
  box.innerHTML = renderMd("Let $ \\\\norm{x_1} * y_2 $ be,\\n\\\\begin{align}\\na &= b \\\\\\\\\\nc &= d\\n\\\\end{align}\\n\\n| a | b |\\n|---|---|\\n| $|x|$ | c |");
  const ok = box.querySelectorAll(".katex").length === 3 && !box.innerHTML.includes("#cc0000")
    && box.querySelectorAll("td").length === 2;
  box.querySelectorAll(".katex").forEach((k) => k.remove());
  return ok && !/[$\\\\]/.test(box.textContent);`));
// client.js on the page: each question it asks the real server is one the server knows.
const asked = await ed.ev(`
  const rs = { tree: await Q.tree(), config: await Q.config(), symbols: await Q.symbols(), snippets: await Q.snippets(),
               file: await Q.file("main.tex"), log: await Q.log("main.tex"), diff: await Q.diff(), github: await Q.github(),
               publishInfo: await Q.publishInfo(), info: await Q.info(), commands: await Q.commands(C.provider),
               account: await Q.account(C.provider), uploadSync: await Q.uploadSync(["main.tex"]), pdfstat: await Q.pdfstat() };
  return Object.entries(rs).filter(([, r]) => r._status === 0 || r.error === "not found").map(([k, r]) => k + ": " + (r.error || r._status));`);
check("client.js asks routes the server answers", asked.length === 0, asked.join(" | "));
await ed.close();
const hm = await open(home);
check("home search lists the project", await hm.ev(`
  for (let i = 0; i < 300 && !(typeof H !== "undefined" && H.loaded); i++) await new Promise((r) => setTimeout(r, 100));
  const before = viewItems("").items.length;
  const found = viewItems("proj").items.map((i) => i.p && i.p.name);
  return before >= 1 && found.includes("proj");`));
check("home loads", hm.errors.length === 0, hm.errors.join(" | "));
check("home ago and plural", await hm.ev(`return ago(Date.now() / 1000 - 7200) === "2 h ago" && plural(2, "item") === "2 items"`));
// Snippets are set in Home → Settings, and the editors read them from their own server.
check("snippet settings in Home reach the editor", await hm.ev(`
  const wait = async (ok) => { for (let i = 0; i < 100 && !ok(); i++) await new Promise((r) => setTimeout(r, 50)); };
  await openSettings();
  const f = $("#form-settings");
  await wait(() => $("#snip-status").textContent);
  const shown = f.elements.snippets.checked && f.elements.snippets_fraction.checked && $("#snip-path").textContent.endsWith("snippets.js");
  $("#snip-list").click();
  const listed = $("#dlg-snippets").open && $("#snip-text").readOnly && $("#snip-text").value.includes("Built in");
  $("#dlg-snippets").close();
  f.elements.snippets_fraction.checked = false;
  f.requestSubmit();
  await wait(() => !$("#dlg-settings").open);
  return shown && listed && !$("#dlg-settings").open;`)
  && (await (await fetch(editor.replace(/\/?$/, "/") + "api/snippets")).json()).fraction === false);
await hm.ev(`await api("/api/settings", { snippets_fraction: true });`);
await hm.close();
const vw = await open(editor.replace(/\/$/, "") + "/viewer");
check("pdf viewer loads", vw.errors.length === 0, vw.errors.join(" | "));
await vw.close();
console.log(results.join("\n"));
process.exit(0);
