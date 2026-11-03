// Run against a fresh, isolated project server: node research_init.mjs <editor URL>.
import { closeSocket } from "./close.mjs";

const url = process.argv[2], cdp = process.env.PRISM_CDP_URL || "http://127.0.0.1:9333";
const tab = await (await fetch(cdp + "/json/new?" + encodeURIComponent(url), { method: "PUT" })).json();
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((resolve) => ws.addEventListener("open", resolve));
let nextId = 0;
const pending = new Map(), errors = [], results = [];
ws.addEventListener("message", (message) => {
  const data = JSON.parse(message.data);
  if (data.id && pending.has(data.id)) { pending.get(data.id)(data); pending.delete(data.id); }
  if (data.method === "Runtime.exceptionThrown") errors.push(data.params.exceptionDetails.exception?.description || data.params.exceptionDetails.text);
});
const send = (method, params = {}) => new Promise((resolve) => {
  const id = ++nextId; pending.set(id, resolve); ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expression) => {
  const response = await send("Runtime.evaluate", { expression: `(async () => { ${expression} })()`, awaitPromise: true, returnByValue: true });
  if (response.result.exceptionDetails) throw new Error(response.result.exceptionDetails.exception?.description || JSON.stringify(response.result.exceptionDetails));
  return response.result.result.value;
};
const check = (name, passed, detail = "") => results.push(`${passed ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);
const ready = () => evaluate(`
  for (let i = 0; i < 150 && (typeof RESEARCH === 'undefined' || !RESEARCH.state); i++) await new Promise((r) => setTimeout(r, 100));
  return typeof RESEARCH !== 'undefined' && !!RESEARCH.state;
`);

try {
  await send("Runtime.enable"); await send("Page.enable");
  check("research interface loads", await ready());
  const initial = await evaluate(`
    const config = await Q.config();
    window.researchOriginalSources = {};
    for (const f of S.files.filter((f) => f.path.endsWith('.tex'))) researchOriginalSources[f.path] = (await Q.file(f.path)).content;
    return { initialized: (await Q.research()).initialized, invitation: !$('#research-invitation').hidden,
      open: !$('#research-body').hidden, popup: !!document.querySelector('#research dialog'), main: config.main };
  `);
  check("initial invitation does not initialize silently", initial.initialized === false && initial.invitation && initial.open && !initial.popup, JSON.stringify(initial));
  const postponed = await evaluate(`
    $('#research-later').click();
    return { initialized: (await Q.research()).initialized, closed: $('#research-body').hidden,
      input: !$('#chat-input').disabled, editor: !cm.getOption('readOnly') };
  `);
  check("Later preserves reading, chat and manual editing", !postponed.initialized && postponed.closed && postponed.input && postponed.editor, JSON.stringify(postponed));
  await evaluate(`
    $('#research-toggle').click();
    $('#research-question').value = 'Which existing notation does this project use?';
    $('#research-notation').value = String.raw\`\\ell, \\ell_+\`;
    $('#research-initialize').click();
    for (let i = 0; i < 150 && (RESEARCH.busy || !RESEARCH.state.initialized); i++) await new Promise((r) => setTimeout(r, 100));
  `);
  const initialized = await evaluate(`
    const state = await Q.research();
    return { initialized: state.initialized, report: !$('#research-report').hidden,
      invitationHidden: $('#research-invitation').hidden, symbols: state.symbols.map((s) => s.notation),
      sources: state.sources.length, sourceVersions: state.sources.every((s) => !!s.sha256),
      conflicts: [...state.terms, ...state.symbols].filter((t) => t.status === 'conflicting').length,
      allConflictDefinitionsShown: [...state.terms, ...state.symbols].filter((t) => t.status === 'conflicting').every((t) =>
        t.definitions.every((definition) => [...document.querySelectorAll('.research-definitions li')].some((row) => row.textContent.startsWith(definition)))),
      mainUnchanged: (await Promise.all(Object.entries(researchOriginalSources).map(async ([path, content]) => (await Q.file(path)).content === content))).every(Boolean) };
  `);
  check("explicit action saves and displays the baseline", initialized.initialized && initialized.report && initialized.invitationHidden, JSON.stringify(initialized));
  check("complete notations stay distinct", initialized.symbols.includes(String.raw`\ell`) && initialized.symbols.includes(String.raw`\ell_+`), JSON.stringify(initialized.symbols));
  check("source versions are visible and manuscript stays unchanged", initialized.sourceVersions && initialized.mainUnchanged);
  check("all conflicting source definitions remain visible", initialized.allConflictDefinitionsShown && (!initialized.sources || initialized.conflicts > 0));
  const corrected = await evaluate(`
    let symbol = [...document.querySelectorAll('.research-symbol')].find((input) => input.dataset.notation === 'x');
    const symbolPresent = (await Q.research()).symbols.some((item) => item.notation === 'x');
    if (symbol) symbol.value = 'Symbol meaning corrected by the author in the browser.';
    await loadResearch();
    symbol = [...document.querySelectorAll('.research-symbol')].find((input) => input.dataset.notation === 'x');
    const unsavedSymbolPreserved = !symbolPresent || symbol?.value === 'Symbol meaning corrected by the author in the browser.';
    const term = document.querySelector('.research-term');
    if (term) term.value = 'Meaning corrected by the author in the browser.';
    const exclude = [...document.querySelectorAll('.research-symbol-exclude')].find((input) => input.value !== 'x');
    if (exclude) exclude.checked = true;
    const excluded = exclude ? exclude.value : null;
    $('#research-notes').value = 'Human correction from the browser check.';
    $('#research-save').click();
    for (let i = 0; i < 100 && RESEARCH.busy; i++) await new Promise((r) => setTimeout(r, 100));
    const state = await Q.research();
    return { notes: state.corrections.map((c) => c.notes), corrected: term ? state.terms.some((t) => t.meaning === 'Meaning corrected by the author in the browser.') : true,
      unsavedSymbolPreserved, symbolCorrected: !symbolPresent || state.symbols.some((item) => item.notation === 'x' && item.status === 'corrected' && item.meaning === 'Symbol meaning corrected by the author in the browser.'),
      removed: !excluded || !state.symbols.some((s) => s.notation === excluded) };
  `);
  check("human report corrections persist", corrected.corrected && corrected.removed && corrected.notes.includes("Human correction from the browser check."), JSON.stringify(corrected));
  check("symbol meaning correction survives refresh and saves", corrected.unsavedSymbolPreserved && corrected.symbolCorrected, JSON.stringify(corrected));
  const reloaded = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { ws.removeEventListener("message", loaded); reject(new Error("reload timed out")); }, 15000);
    const loaded = (message) => {
      if (JSON.parse(message.data).method !== "Page.loadEventFired") return;
      clearTimeout(timeout); ws.removeEventListener("message", loaded); resolve();
    };
    ws.addEventListener("message", loaded);
  });
  await send("Page.reload"); await reloaded;
  check("reloaded interface loads", await ready());
  const restored = await evaluate(`
    $('#research-toggle').click();
    return { initialized: RESEARCH.state.initialized, invitationHidden: $('#research-invitation').hidden,
      hasCorrection: $('#research-report').textContent.includes('Human correction from the browser check.'),
      newSetup: !$('#research-invitation').hidden };
  `);
  check("reload restores report without another setup", restored.initialized && restored.invitationHidden && restored.hasCorrection && !restored.newSetup, JSON.stringify(restored));
  check("no page errors", errors.length === 0, errors.join(" | "));
} finally {
  console.log(results.join("\n"));
  await closeSocket(ws);
  await fetch(cdp + "/json/close/" + tab.id);
}
process.exitCode = results.some((line) => line.startsWith("FAIL")) ? 1 : 0;
