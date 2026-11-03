// Real input provenance in the isolated research fixture; no model or GitHub calls.
// PRISM_CDP_URL=http://127.0.0.1:<port> node research_incremental.mjs <editor URL>
import { closeSocket } from "./close.mjs";

const url = process.argv[2], cdp = process.env.PRISM_CDP_URL || "http://127.0.0.1:9333";
const tab = await (await fetch(cdp + "/json/new?" + encodeURIComponent(url), { method: "PUT" })).json();
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((resolve) => ws.addEventListener("open", resolve));
let next = 0;
const pending = new Map(), errors = [], results = [];
ws.addEventListener("message", (message) => {
  const data = JSON.parse(message.data);
  if (data.id && pending.has(data.id)) { pending.get(data.id)(data); pending.delete(data.id); }
  if (data.method === "Runtime.exceptionThrown") errors.push(data.params.exceptionDetails.exception?.description || data.params.exceptionDetails.text);
});
const send = (method, params = {}) => new Promise((resolve) => {
  const id = ++next; pending.set(id, resolve); ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (code) => {
  const response = await send("Runtime.evaluate", { expression: `(async () => { ${code} })()`, awaitPromise: true, returnByValue: true });
  if (response.result.exceptionDetails) throw new Error(response.result.exceptionDetails.exception?.description || JSON.stringify(response.result.exceptionDetails));
  return response.result.result.value;
};
const check = (name, ok, detail = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);
const wait = (condition) => evaluate(`for(let i=0;i<100&&!(${condition});i++)await new Promise(r=>setTimeout(r,50));return !!(${condition});`);
const endCursor = () => evaluate(`const doc=cm.getDoc();cm.setCursor(doc.posFromIndex(doc.getValue().length));cm.focus();return true;`);
const save = () => evaluate(`const r=await TABS.saveAll();await loadResearch();return r;`);
const key = async (name, code) => {
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: name, code: name, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: name, code: name, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code });
};

try {
  await send("Runtime.enable"); await send("Page.enable");
  check("research interface loads", await wait("typeof RESEARCH!=='undefined'&&RESEARCH.state&&P.projectKey&&S.active"));
  await evaluate(`
    if(!RESEARCH.state.initialized){$('#research-initialize').click();for(let i=0;i<100&&(!RESEARCH.state.initialized||RESEARCH.busy);i++)await new Promise(r=>setTimeout(r,50));}
    await openFile('main.tex');await reloadFromDisk(activeTab());
    window.manualRequests=[];const originalFetch=window.fetch;
    window.fetch=(path,options)=>{if(path==='/api/file'&&options?.body){const body=JSON.parse(options.body);manualRequests.push({path:body.path,proof:!!body.manual_edit_token});}return originalFetch(path,options);};
    return true;
  `);
  check("default snippets remain enabled", await evaluate(`return SNIP_SET.on;`));
  await endCursor();
  const manualText = "\nHuman input $s_H$.\n";
  await send("Input.insertText", { text: manualText });
  check("trusted browser input reaches CodeMirror", await wait("cm.getValue().includes('Human input $s_H$')"));
  const saved = await save(); check("manual input saves", saved.ok, JSON.stringify(saved));
  const manual = await evaluate(`return {known:RESEARCH.state.symbols.some(s=>s.canonical==='s_H'),notice:(RESEARCH.state.maintenance_notices||[]).some(n=>n.kind==='manual_added'&&n.canonical==='s_H'),proof:manualRequests.some(r=>r.path==='main.tex'&&r.proof)};`);
  check("trusted input alone sends private save evidence", manual.proof, JSON.stringify(manual));
  check("human notation registers with its source notice", manual.known && manual.notice, JSON.stringify(manual));

  await endCursor(); await send("Input.insertText", { text: "\nSnippet $" });
  await wait("cm.getValue().endsWith('Snippet $')");
  await send("Input.insertText", { text: "@" }); await wait("cm.getValue().endsWith('@')");
  await send("Input.insertText", { text: "a" });
  check("default @a expands from real input", await wait(String.raw`cm.getValue().endsWith('\\alpha')`));
  await send("Input.insertText", { text: " + " }); await wait("cm.getValue().endsWith(' + ')");
  await send("Input.insertText", { text: "@" }); await wait("cm.getValue().endsWith('@')");
  await send("Input.insertText", { text: "r" });
  check("default @r expands from real input", await wait(String.raw`cm.getValue().endsWith('\\rho')`));
  await send("Input.insertText", { text: "$.\n" }); await save();
  check("controlled snippet notation registers as human input", await evaluate(String.raw`return ['\\alpha','\\rho'].every(canonical=>RESEARCH.state.symbols.some(s=>s.canonical===canonical)&&(RESEARCH.state.maintenance_notices||[]).some(n=>n.kind==='manual_added'&&n.canonical===canonical));`));

  await evaluate(String.raw`const doc=cm.getDoc();doc.replaceRange('\nDeletion $d_{XY}$ and $e_{MN}$.\n% $f_T$\n',doc.posFromIndex(doc.getValue().length),null,'+find');return true;`);
  await save();
  await evaluate(`const doc=cm.getDoc();cm.setCursor(doc.posFromIndex(doc.getValue().indexOf('d_{XY}')+'d_{XY'.length));cm.focus();return true;`);
  await key("Backspace", 8); await wait("cm.getValue().includes('d_{X}')"); await save();
  check("native Backspace supplies evidence for the complete notation it creates", await evaluate(`return RESEARCH.state.symbols.some(s=>s.canonical==='d_X');`));
  await evaluate(`const doc=cm.getDoc();cm.setCursor(doc.posFromIndex(doc.getValue().indexOf('e_{MN}')+'e_{'.length));cm.focus();return true;`);
  await key("Delete", 46); await wait("cm.getValue().includes('e_{N}')"); await save();
  check("native Delete supplies evidence for the complete notation it creates", await evaluate(`return RESEARCH.state.symbols.some(s=>s.canonical==='e_N');`));
  await evaluate(`const doc=cm.getDoc();cm.setCursor(doc.posFromIndex(doc.getValue().indexOf('% $f_T$')+2));cm.focus();return true;`);
  await key("Enter", 13); await save();
  check("native Enter retains evidence when it exposes previously commented mathematics", await evaluate(`return RESEARCH.state.symbols.some(s=>s.canonical==='f_T');`));

  await evaluate(String.raw`const doc=cm.getDoc();doc.replaceRange('\nProgrammatic input $r_U$.\n',doc.posFromIndex(doc.getValue().length),null,'+input');return true;`);
  await save();
  const unknown = await evaluate(`return {known:RESEARCH.state.symbols.some(s=>s.canonical==='r_U'),unverified:(RESEARCH.state.unverified_symbols||[]).some(s=>s.canonical==='r_U'),proof:manualRequests.at(-1)?.proof};`);
  check("programmatic same-origin edit remains unverified", !unknown.known && unknown.unverified && !unknown.proof, JSON.stringify(unknown));
  await evaluate(String.raw`const doc=cm.getDoc();doc.replaceRange('\nForged snippet $\\gamma_P$.\n',doc.posFromIndex(doc.getValue().length),null,'+snippet');return true;`);
  await save();
  check("a programmatic +snippet origin cannot grant human provenance", await evaluate(String.raw`return !RESEARCH.state.symbols.some(s=>s.canonical==='\\gamma_P')&&(RESEARCH.state.unverified_symbols||[]).some(s=>s.canonical==='\\gamma_P');`));
  await endCursor(); await send("Input.insertText", { text: " " });
  await wait("cm.getValue().endsWith(' ')"); await save();
  const untouched = await evaluate(`return {known:RESEARCH.state.symbols.some(s=>s.canonical==='r_U'),unverified:(RESEARCH.state.unverified_symbols||[]).some(s=>s.canonical==='r_U')};`);
  check("a later human whitespace edit does not adopt an old unknown symbol", !untouched.known && untouched.unverified, JSON.stringify(untouched));

  await evaluate(String.raw`
    researchOpen(true);const term=document.querySelector('.research-term');window.pendingCorrection=term?.dataset.name;
    if(term)term.value='Keep this unsaved correction.';
    const doc=cm.getDoc();doc.replaceRange('\nAnother unknown $q_V$.\n',doc.posFromIndex(doc.getValue().length),null,'+find');return true;
  `);
  await save();
  check("maintenance does not overwrite unsaved report corrections", await evaluate(`return !pendingCorrection||[...document.querySelectorAll('.research-term')].some(t=>t.dataset.name===pendingCorrection&&t.value==='Keep this unsaved correction.');`));
  await evaluate(`for(const input of document.querySelectorAll('.research-term'))input.value=(RESEARCH.rendered.terms.find(t=>t.name===input.dataset.name)?.meaning||'');await loadResearch();return true;`);

  const oldNotices = await evaluate(`return (RESEARCH.state.maintenance_notices||[]).map(n=>n.id);`);
  const repeated = await evaluate(`await loadResearch();await loadResearch();return (RESEARCH.state.maintenance_notices||[]).map(n=>n.id);`);
  check("unchanged maintenance has no duplicate notices", JSON.stringify(oldNotices) === JSON.stringify(repeated));
  check("unverified status and source locations are visible", await evaluate(`researchOpen(true);return !!document.querySelector('#research-unverified')&&document.querySelector('#research-report').textContent.includes('r_U')&&document.querySelector('#research-report').textContent.includes('main.tex');`));

  await evaluate(String.raw`
    const result=await Q.saveFile({path:'reference.tex',content:'Reference text.\n',base_mtime:null,force:false});
    if(result._status!==200)throw new Error(result.error||'reference setup failed');
    await poll();await openFile('reference.tex');return true;
  `);
  await endCursor(); await send("Input.insertText", { text: "\nReference input $p_R$.\n" });
  await wait("cm.getValue().includes('$p_R$')"); await save();
  check("human edits of reference material do not adopt manuscript notation", await evaluate(`return !RESEARCH.state.symbols.some(s=>s.canonical==='p_R');`));
  await evaluate(String.raw`
    const source=await Q.file('main.tex');const saved=await Q.saveFile({path:'main.tex',content:source.content+'\nExternal input $t_W$.\n',base_mtime:source.mtime,force:false});
    if(saved._status!==200)throw new Error(saved.error||'external save failed');await poll();return true;
  `);
  check("existing project polling refreshes externally changed notation automatically", await wait("(RESEARCH.state.unverified_symbols||[]).some(s=>s.canonical==='t_W')"));
  const reloaded = new Promise((resolve, reject) => {
    const timer = setTimeout(() => { ws.removeEventListener("message", loaded); reject(new Error("reload timed out")); }, 15000);
    const loaded = (message) => {
      if (JSON.parse(message.data).method !== "Page.loadEventFired") return;
      clearTimeout(timer); ws.removeEventListener("message", loaded); resolve();
    };
    ws.addEventListener("message", loaded);
  });
  await send("Page.reload"); await reloaded;
  check("reload restores incremental research state", await wait("typeof RESEARCH!=='undefined'&&RESEARCH.state?.initialized&&RESEARCH.state.symbols.some(s=>s.canonical==='s_H')&&(RESEARCH.state.unverified_symbols||[]).some(s=>s.canonical==='r_U')"));
  check("no page errors", errors.length === 0, errors.join(" | "));
} finally {
  console.log(results.join("\n"));
  await closeSocket(ws);
  await fetch(cdp + "/json/close/" + tab.id);
}
process.exitCode = results.some((line) => line.startsWith("FAIL")) ? 1 : 0;
