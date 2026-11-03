// Run after agent-browser connect, against the isolated research fixture.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { writeFile, unlink } from "node:fs/promises";
import { join } from "node:path";

const [url, work, output] = process.argv.slice(2);
if (!url || !work || !output) throw new Error("Supply editor URL, fixture work and output directory");
const operations = [], results = [];
const cli = (...args) => {
  const text = execFileSync("agent-browser", args, { encoding: "utf8", timeout: 25000, maxBuffer: 1024 * 1024 });
  operations.push({ args, output: text });
  return text;
};
const evaluate = (body) => JSON.parse(cli("eval", `(async()=>{${body}})()`));
const check = (name, ok) => { results.push(`${ok ? "PASS" : "FAIL"} ${name}`); assert.ok(ok, name); };
const state = () => evaluate("return await Q.research();");
const say = (text) => ({ choices: [{ delta: { content: text } }] });
const call = (name, args) => [{ choices: [{ delta: { tool_calls: [{ index: 0, id: name,
  function: { name, arguments: JSON.stringify(args) } }] } }] }];
const candidate = (notation) => ({ notation, definition: "A shorthand for the existing sum.",
  scope: "The current estimate.", reason: "Discuss a concise name before use.", source_expression: "x+x" });
const chat = async (text, responses, mode = "ask", failed = false) => {
  await writeFile(join(work, "api-script.json"), JSON.stringify(responses));
  cli("snapshot", "-i");
  cli("select", "#chat-mode", mode);
  cli("fill", "#chat-input", text);
  cli("click", "#chat-send");
  const finished = evaluate(`for(let i=0;i<200&&(C.sending||C.job);i++)await new Promise(r=>setTimeout(r,30));
    await loadResearch();return !C.sending&&!C.job&&document.querySelector('#chat-send').textContent==='Send'
      &&${failed ? "true" : "document.querySelector('#chat-input').value===''"};`);
  check("ordinary reply restores Send", finished);
};

try {
  cli('set', 'viewport', '1440', '1000');
  cli('open', url); cli('snapshot', '-i');
  evaluate("for(let i=0;i<150&&(!RESEARCH.state||!P.projectKey);i++)await new Promise(r=>setTimeout(r,30));return true;");
  cli('click', '#research-initialize');
  check('manual initialization completes', evaluate("for(let i=0;i<100&&!RESEARCH.state.initialized;i++)await new Promise(r=>setTimeout(r,30));return RESEARCH.state.initialized;"));
  const original=evaluate("return (await Q.file('main.tex')).content;");
  const batch=['z_B','w_B','v_B'].map((n,i)=>({index:i,id:n,function:{name:'propose_notation',arguments:JSON.stringify(candidate(n))}}));
  await chat('Propose the three candidates together.', [[{choices:[{delta:{tool_calls:batch}}]}]]);
  check('one Turn saves and shows all three pending items', state().proposals.length===3&&state().proposals.every(p=>p.status==='pending')&&evaluate("return document.querySelectorAll('#chat-log .notation-proposal').length===3;"));
  await chat('Approve $z_B$; maybe approve $w_B$.', [[say('The other items remain pending.')]]);
  check('clear part approves only one and ambiguity is clarified', state().proposals[0].status==='approved'&&state().proposals.slice(1).every(p=>p.status==='pending')&&evaluate("return !!document.querySelector('.notation-clarification');"));
  await chat('Yes', [[say('Specify the remaining items separately.')]]);
  check('yes cannot approve the remaining batch', state().proposals.slice(1).every(p=>p.status==='pending'));
  await chat('Reject $w_B$; use $q_B$ instead of $v_B$.', [[say('The selected replacement is available.')]]);
  check('rejection and replacement remain independent', state().proposals[1].status==='rejected'&&state().proposals[2].status==='rejected'&&state().adoptions.some(a=>a.canonical==='q_B'));
  const changed=original.replace(String.raw`$\ell+\ell_+$`,'$z_B$');
  await chat('Use the adopted notation.', [call('write_file',{path:'main.tex',content:changed}),[say('The adopted $z_B$ and $q_B$ are available.')]], 'edit');
  check('only adopted names enter source and chat', evaluate(`return (await Q.file('main.tex')).content===${JSON.stringify(changed)}&&document.querySelector('#chat-log').textContent.includes('are available');`));
  await chat('用 h_B 表示 x+x', [[say('The direct instruction was registered.')]]);
  check('ordinary bare instruction adopts without a false proposal', state().adoptions.some(a=>a.canonical==='h_B')&&!state().proposals.some(p=>p.canonical==='h_B'));
  await chat('Use $a_i$ for $x+x$ with i in {1,2,3}.', [[say('The finite family is registered.')]]);
  await chat('Discuss an allowed member.', [[say('The existing $a_2$ can be reused.')]]);
  check('family rule permits its declared member', state().families[0].rule.indices.join(',')==='1,2,3'&&evaluate("return document.querySelector('#chat-log').textContent.includes('can be reused');"));
  await chat('Check an outside member.', [[say('The draft uses $a_9$.')]]);
  check('outside family remains unapproved', !state().symbols.some(s=>s.canonical==='a_9')&&state().proposals.some(p=>p.canonical==='a_9'&&p.status==='pending'));
  await chat('Restore the original expression for the next discussion.', [call('write_file',{path:'main.tex',content:original}),[say('The original source is retained.')]],'edit');
  await chat('Propose a shorter name for the lengths.', [call('propose_notation',{...candidate('z_E'),source_expression:String.raw`\ell+\ell_+`})]);
  await chat('Reject $z_E$ and keep its expression expanded.', [[say('Keep the expression expanded.')]]);
  check('expression rejection records the original human scope', state().rejections.some(r=>r.action?.rejection_scope==='expression'&&r.human_text.includes('expanded')));
  await chat('Continue the estimate.', [call('write_file',{path:'main.tex',content:original.replace(String.raw`$\ell+\ell_+$`,'$a_{2}$')})], 'edit');
  check('known braced member cannot evade expression refusal', evaluate(`return (await Q.file('main.tex')).content===${JSON.stringify(original)};`));
  const count=state().proposals.length;
  await chat('Discuss the same estimate.', [[say(String.raw`Use the abbreviation $\ell+\ell_+=a_2$.`)]]);
  check('reversed definition is withheld without repeated proposal', state().proposals.length===count&&evaluate("return !document.querySelector('#chat-log').textContent.includes('Use the abbreviation');"));
  await chat('Reopen $z_E$.', [[say('The candidate can be reconsidered.')]]);
  check('explicit reopen only returns to pending', state().proposals.find(p=>p.canonical==='z_E').status==='pending'&&!state().symbols.some(s=>s.canonical==='z_E'));
  await chat('Yes', [[say('The author adopted $z_E$.')]]);
  check('later ordinary reply can approve the reopened item', state().proposals.find(p=>p.canonical==='z_E').status==='approved');
  await writeFile(join(work,'fail-persistence'),'fail');
  const decisions=state().decisions.length;
  await chat('Use $b_i$ for $x+x$ with i in {4,5}.', [[say('Must not run.')]],'ask',true);
  check('failed family registration grants nothing', state().decisions.length===decisions&&!state().families.some(f=>f.rule.base==='b'));
  await unlink(join(work,'fail-persistence'));
  await chat('Use $b_i$ for $x+x$ with i in {4,5}.', [[say('The retry is saved.')]]);
  cli('reload');
  evaluate("for(let i=0;i<150&&(!RESEARCH.state?.initialized||!C.session);i++)await new Promise(r=>setTimeout(r,30));return true;");
  cli('snapshot','-i');cli('select','#chat-model','fixture-model-2');
  await chat('Discuss existing $b_4$.', [[say('The existing $b_4$ remains available.')]]);
  check('reload and model change preserve decisions and rules', state().families.length===2&&state().proposals.find(p=>p.canonical==='w_B').status==='rejected'&&state().adoptions.some(a=>a.canonical==='h_B'));
  if(evaluate("return document.querySelector('#research-body').hidden;"))cli('click','#research-toggle');
  check('report exposes statuses rules scopes and raw evidence', evaluate("return !!document.querySelector('[data-policy=families]')&&!!document.querySelector('[data-policy=adoptions]')&&!!document.querySelector('[data-policy=rejections]')&&document.querySelector('#research-report').textContent.includes('Human decision evidence');"));
  await chat('保持 $x+x+x$ 展开，不要简称。', [[say('The expression remains expanded.')]]);
  check('standalone refusal is never announced as approval', evaluate("return [...document.querySelectorAll('.notation-decision')].at(-1).textContent.includes('rejected');"));
  cli('snapshot','-i');cli('screenshot',join(output,'browser-batch.png'));
} finally {
  await writeFile(join(output,'browser-batch-operations.json'),JSON.stringify(operations,null,2));
  console.log(results.join('\n'));
}
