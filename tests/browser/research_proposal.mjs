// Isolated FakeAPI acceptance. The fixture owns every project and API path.
// PRISM_CDP_URL=http://127.0.0.1:<port> node research_proposal.mjs <editor URL> <fixture work directory>
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { closeSocket } from "./close.mjs";

const url = process.argv[2], work = process.argv[3], cdp = process.env.PRISM_CDP_URL || "http://127.0.0.1:9333";
if (!url || !work) throw new Error("Supply the isolated editor URL and fixture work directory");
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
const wait = (condition) => evaluate(`for(let i=0;i<200&&!(${condition});i++)await new Promise(r=>setTimeout(r,50));return !!(${condition});`);
const script = (responses) => writeFile(join(work, "api-script.json"), JSON.stringify(responses), "utf8");
const content = (text) => ({ choices: [{ delta: { content: text } }] });
const call = (id, name, args) => {
  const encoded = JSON.stringify(args), cut = Math.floor(encoded.length / 2);
  return [
    { choices: [{ delta: { tool_calls: [{ index: 0, id, type: "function", function: { name, arguments: encoded.slice(0, cut) } }] } }] },
    { choices: [{ delta: { tool_calls: [{ index: 0, function: { arguments: encoded.slice(cut) } }] } }] },
    { choices: [], usage: { prompt_tokens: 100, completion_tokens: 7 } },
  ];
};
const chat = async (text, mode = "ask") => {
  await evaluate(`$('#chat-mode').value=${JSON.stringify(mode)};$('#chat-input').focus();return true;`);
  await send("Input.insertText", { text });
  await evaluate(`$('#chat-send').click();return true;`);
  const ended = await wait("!C.sending&&!C.job&&$('#chat-send').textContent==='Send'&&$('#chat-input').value===''");
  if (!ended)
    throw new Error("Turn did not finish and restore Send");
  await evaluate("await loadResearch();return true;");
};
const noPopup = () => evaluate(`return !document.querySelector('dialog[open],.permission-actions')&&$('#chat-send').textContent==='Send'&&!$('#chat-send').disabled;`);

try {
  await send("Runtime.enable"); await send("Page.enable");
  check("research interface loads", await wait("typeof RESEARCH!=='undefined'&&RESEARCH.state&&P.projectKey"));
  const originalSource = await evaluate("return (await Q.file('main.tex')).content;");
  await evaluate(`
    window.proposalOriginal=(await Q.file('main.tex')).content;
    if(!RESEARCH.state.initialized){$('#research-initialize').click();for(let i=0;i<200&&(!RESEARCH.state.initialized||RESEARCH.busy);i++)await new Promise(r=>setTimeout(r,50));}
    return true;
  `);
  check("initialization saves the baseline", await evaluate("return RESEARCH.state.initialized&&!!document.querySelector('#research-report details');"));
  check("API capability states active checks and ordinary text decisions", await evaluate(`return !$('#notation-guard-status').hidden&&$('#notation-guard-status').textContent.includes('file and chat checks are active')&&$('#notation-guard-status').textContent.includes('Text approval is available');`));

  const details = { notation: "z_P", definition: "A proposed shorthand for the repeated sum", scope: "The next estimate",
    reason: "Keep the sum readable", source_expression: "x+x" };
  // A deliberate candidate is emitted only after its complete record is durable.
  await script([call("candidate-one", "propose_notation", details)]);
  await chat("Suggest one notation candidate for the repeated expression, then stop.");
  const explicit = await evaluate(`
    const state=await Q.research(), p=(state.proposals||[]).find(p=>p.canonical==='z_P');
    const card=[...document.querySelectorAll('#chat-log .notation-proposal')].find(c=>c.dataset.proposalId===p?.id);
    return {id:p?.id,status:p?.status,visible:!!card,metadata:card?.textContent,count:state.proposals.length,
      notKnown:!state.symbols.some(s=>s.canonical==='z_P'),unchanged:(await Q.file('main.tex')).content===proposalOriginal};
  `);
  check("ordinary chat shows the saved pending candidate", explicit.status === "pending" && explicit.visible && explicit.id, JSON.stringify(explicit));
  check("candidate metadata is readable and complete", [details.definition, details.scope, details.reason, details.source_expression, "Origin", "not adopted"].every((v) => explicit.metadata?.includes(v)), explicit.metadata || "no card");
  check("candidate is neither known notation nor a manuscript edit", explicit.notKnown && explicit.unchanged);
  check("proposal completes the Turn with Send and no approval popup", await noPopup());

  // Existing complete notation can be discussed normally, without another proposal.
  await script([[content("Existing $\\ell+\\ell_+$ and $x$ can be discussed.")]]);
  await chat("Discuss the existing manuscript notation.");
  const known = await evaluate(`return {count:(await Q.research()).proposals.length,text:$('#chat-log').textContent};`);
  check("known notation remains visible without another application", known.count === explicit.count && known.text.includes("can be discussed"), JSON.stringify({ count: known.count }));

  // Observe the visible chat during a split mathematical expression. Raw draft text
  // must not appear outside the controlled candidate, even before the stream ends.
  await evaluate(`
    window.rawLeaks=[];window.rawObserver=new MutationObserver(()=>{
      const copy=$('#chat-log').cloneNode(true);copy.querySelectorAll('.notation-proposal').forEach(e=>e.remove());
      if(copy.textContent.includes('r_Q')||copy.textContent.includes('$r'))rawLeaks.push(copy.textContent);
    });rawObserver.observe($('#chat-log'),{subtree:true,childList:true,characterData:true});return true;
  `);
  await script([[content("The draft uses $"), 0.3, content("r"), 0.3, content("_Q"), 0.3, content("=x$ in this argument.")]]);
  await chat("Explain the next step using the existing material.");
  const held = await evaluate(`
    rawObserver.disconnect();const state=await Q.research(),p=(state.proposals||[]).find(p=>p.canonical==='r_Q');
    const card=[...document.querySelectorAll('#chat-log .notation-proposal')].find(c=>c.dataset.proposalId===p?.id);
    return {leaks:rawLeaks.length,pending:p?.status==='pending',needsDetails:p?.needs_details,visible:!!card,
      missing:card?.textContent.includes('needs more details'),notKnown:!state.symbols.some(s=>s.canonical==='r_Q'),unchanged:(await Q.file('main.tex')).content===proposalOriginal};
  `);
  check("unknown split draft never appears as ordinary chat", held.leaks === 0, JSON.stringify(held));
  check("held draft becomes a clearly incomplete candidate", held.pending && held.needsDetails && held.visible && held.missing && held.notKnown, JSON.stringify(held));
  check("held chat leaves the manuscript unchanged and returns Send", held.unchanged && await noPopup());

  // Native file tools also preserve the source. Their candidate is not approval.
  await script([call("blocked-edit", "edit_file", { path: "main.tex", old_string: "Let $x$ be a real number.", new_string: "Let $v_B$ be a real number." })]);
  await chat("Rewrite @main.tex using a concise notation.", "edit");
  const protectedFile = await evaluate(`
    const state=await Q.research(),p=(state.proposals||[]).find(p=>p.canonical==='v_B');
    return {pending:p?.status==='pending',visible:[...document.querySelectorAll('#chat-log .notation-proposal')].some(c=>c.dataset.proposalId===p?.id),unchanged:(await Q.file('main.tex')).content===proposalOriginal,
      notKnown:!state.symbols.some(s=>s.canonical==='v_B')};
  `);
  check("unknown file-tool notation is held before application", protectedFile.pending && protectedFile.visible && protectedFile.unchanged && protectedFile.notKnown, JSON.stringify(protectedFile));
  check("file protection preserves normal Turn completion", await noPopup());

  // Nested text math must use the same gate as ordinary mathematical drafts.
  const nestedDraft = String.raw`\[\text{words $n_T$}\]`;
  await evaluate(`
    window.nestedLeaks=[];window.nestedObserver=new MutationObserver(()=>{
      const copy=$('#chat-log').cloneNode(true);copy.querySelectorAll('.notation-proposal').forEach(e=>e.remove());
      if(copy.textContent.includes('n_T'))nestedLeaks.push(copy.textContent);
    });nestedObserver.observe($('#chat-log'),{subtree:true,childList:true,characterData:true});return true;
  `);
  await script([[content(nestedDraft.slice(0, 10)), 0.3, content(nestedDraft.slice(10))]]);
  await chat("Discuss the existing estimate without adopting additional notation.");
  const nestedChat = await evaluate(`
    nestedObserver.disconnect();const state=await Q.research(),p=state.proposals.find(p=>p.canonical==='n_T');
    return {leaks:nestedLeaks.length,pending:p?.status==='pending',notKnown:!state.symbols.some(s=>s.canonical==='n_T')};
  `);
  check("nested mathematical chat is held before display", nestedChat.leaks === 0 && nestedChat.pending && nestedChat.notKnown, JSON.stringify(nestedChat));
  await script([call("nested-edit", "edit_file", {path:"main.tex", old_string:"Let $x$ be a real number.", new_string:String.raw`Let \[\mbox{$m_T$}\] be a real number.`})]);
  await chat("Revise @main.tex using its existing notation.", "edit");
  const nestedFile = await evaluate(`
    const state=await Q.research(),p=state.proposals.find(p=>p.canonical==='m_T');
    return {pending:p?.status==='pending',unchanged:(await Q.file('main.tex')).content===proposalOriginal,notKnown:!state.symbols.some(s=>s.canonical==='m_T')};
  `);
  check("nested file mathematics is held before application", nestedFile.pending && nestedFile.unchanged && nestedFile.notKnown && await noPopup(), JSON.stringify(nestedFile));

  // A background candidate refresh must not erase an unsaved report correction.
  const dirty = await evaluate(`
    researchOpen(true);const input=document.querySelector('.research-term');if(input)input.value='Keep this pending human correction.';
    await loadResearch();return !input||[...document.querySelectorAll('.research-term')].some(t=>t.value==='Keep this pending human correction.');
  `);
  check("candidate refresh preserves dirty report corrections", dirty);

  const reloaded = new Promise((resolve, reject) => {
    const timer = setTimeout(() => { ws.removeEventListener("message", loaded); reject(new Error("reload timed out")); }, 15000);
    const loaded = (message) => {
      if (JSON.parse(message.data).method !== "Page.loadEventFired") return;
      clearTimeout(timer); ws.removeEventListener("message", loaded); resolve();
    };
    ws.addEventListener("message", loaded);
  });
  await send("Page.reload"); await reloaded;
  check("reload restores the research state", await wait("typeof RESEARCH!=='undefined'&&RESEARCH.state?.initialized"));
  const restored = await evaluate(`
    researchOpen(true);const state=await Q.research(),pending=state.proposals.filter(p=>p.status==='pending');
    return {count:pending.length,report:!$('#research-report').hidden,
      visible:pending.every(p=>[...document.querySelectorAll('#research-proposals .notation-proposal')].some(c=>c.dataset.proposalId===p.id&&c.dataset.proposalStatus==='pending'&&c.textContent.includes('not adopted'))),
      notKnown:['z_P','r_Q','v_B','n_T','m_T'].every(n=>!state.symbols.some(s=>s.canonical===n)),unchanged:(await Q.file('main.tex')).content===${JSON.stringify(originalSource)}};
  `);
  check("pending candidates restore in the report without becoming adopted", restored.count === 5 && restored.report && restored.visible && restored.notKnown && restored.unchanged, JSON.stringify(restored));
  check("no page errors", errors.length === 0, errors.join(" | "));
} finally {
  console.log(results.join("\n"));
  await closeSocket(ws);
  await fetch(cdp + "/json/close/" + tab.id);
}
process.exitCode = results.some((line) => line.startsWith("FAIL")) ? 1 : 0;
