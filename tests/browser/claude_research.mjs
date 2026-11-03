// Attach agent-browser to a fresh isolated Chrome before running this suite.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { writeFile, unlink } from 'node:fs/promises';
import { join } from 'node:path';

const [url, work, output] = process.argv.slice(2);
if (!url || !work || !output) throw new Error('Supply editor URL, fixture work, output');
const operations = [], results = [];
const cli = (...args) => {
  const text = execFileSync('agent-browser', args, { encoding:'utf8',timeout:25000,maxBuffer:1024*1024 });
  operations.push({args,output:text}); return text;
};
const evaluate = body => JSON.parse(cli('eval', `(async()=>{${body}})()`));
const check = (name, passed) => { results.push(`${passed ? 'PASS' : 'FAIL'} ${name}`); assert.ok(passed,name); };
const state = () => evaluate('return await Q.research();');
const mcp = (name, arguments_) => ({type:'mcp',name,arguments:arguments_});
const text = value => ({type:'text',text:value});
const candidate = notation => ({notation,definition:'A shorthand for the existing sum.',scope:'The current estimate.',reason:'Discuss a concise name before use.',source_expression:'x+x'});
const begin = async (human, script, mode='ask') => {
  await writeFile(join(work,'profile','cli-script.json'),JSON.stringify(script));
  cli('snapshot','-i'); cli('select','#chat-mode',mode); cli('fill','#chat-input',human); cli('click','#chat-send');
};
const finish = (failed=false) => evaluate(`for(let i=0;i<350&&(C.sending||C.job);i++)await new Promise(r=>setTimeout(r,30));await loadResearch();return !C.sending&&!C.job&&document.querySelector('#chat-send').textContent==='Send'&&${failed ? 'true' : "document.querySelector('#chat-input').value===''"};`);
const chat = async (human, script, mode='ask', failed=false) => { await begin(human,script,mode);check('Claude Turn restores Send',finish(failed)); };

try {
  cli('set','viewport','1440','1000'); cli('open',url); cli('snapshot','-i');
  evaluate("for(let i=0;i<200&&(!RESEARCH.state||!P.projectKey);i++)await new Promise(r=>setTimeout(r,30));return true;");
  check('Claude adapter exposes guarded capability',evaluate("return (P.byId.claude?.notation_guard||{}).application===true&&(P.byId.claude?.notation_guard||{}).display===true;"));
  cli('click','#research-initialize');
  check('manual initialization saves the Claude research report',evaluate("for(let i=0;i<150&&!RESEARCH.state.initialized;i++)await new Promise(r=>setTimeout(r,30));return RESEARCH.state.initialized;"));
  cli('click','#research-toggle');
  const original=evaluate("return (await Q.file('main.tex')).content;");
  await chat('Discuss the manuscript.',[{type:'delta',text:'Unchecked draft uses $u_'},{type:'delta',text:'C$.'},text('Unchecked draft uses $u_C$.')]);
  check('split unapproved chat is withheld before display',!evaluate("return document.querySelector('#chat-log').textContent.includes('Unchecked draft uses');")&&state().proposals.some(p=>p.canonical==='u_C'&&p.status==='pending'));
  await chat('Present the complete candidate for review.',[mcp('propose_notation',candidate('u_C'))]);
  check('ordinary proposal ends Turn with a complete saved candidate',state().proposals.find(p=>p.canonical==='u_C').needs_details===false&&evaluate("return !!document.querySelector('#chat-log .notation-proposal');"));
  const changed=original.replace(String.raw`$\ell+\ell_+$`,'$u_C$');
  await chat('Yes, I approve.',[mcp('write_file',{path:'main.tex',content:changed}),text('The author adopted $u_C$.')],'edit');
  check('plain approval precedes source and chat use',state().proposals.find(p=>p.canonical==='u_C').status==='approved'&&evaluate(`return (await Q.file('main.tex')).content===${JSON.stringify(changed)}&&document.querySelector('#chat-log').textContent.includes('The author adopted');`));
  await chat('Check the reasoning preview.',[
    {type:'emit',frame:{type:'stream_event',event:{type:'content_block_start',index:0,content_block:{type:'thinking'}}}},
    {type:'emit',frame:{type:'stream_event',event:{type:'content_block_delta',index:0,delta:{type:'thinking_delta',thinking:'Unsafe reasoning $thought_C$.'}}}},
    {type:'emit',frame:{type:'stream_event',event:{type:'content_block_stop',index:0}}}
  ]);
  check('unreviewed mathematical reasoning is withheld',!evaluate("return document.querySelector('#chat-log').textContent.includes('Unsafe reasoning');"));
  await chat('Check the tool preview.',[
    {type:'emit',frame:{type:'assistant',message:{content:[{type:'tool_use',id:'unsafe-preview',name:'Read',input:{file_path:'main.tex'}}]}}},
    {type:'emit',frame:{type:'user',message:{content:[{type:'tool_result',tool_use_id:'unsafe-preview',content:'Unsafe tool preview $preview_C$.'}]}}}
  ]);
  check('related mathematical tool preview is checked before display',!evaluate("return document.querySelector('#chat-log').textContent.includes('Unsafe tool preview')||[...document.querySelectorAll('#chat-log .tool')].some(el=>el.title.includes('Unsafe tool preview'));"));
  await writeFile(join(work,'compile-stub'),'diagnostic only');
  await chat('Check the guarded compile route.',[mcp('compile',{clean:true})],'edit');
  cli('snapshot','-i');
  cli('hover','.tool.err[title*="fixture guarded compile diagnostic"]');
  check('Edit compile exposes guarded fixture diagnostic tooltip',evaluate("return [...document.querySelectorAll('#chat-log .tool.err')].some(el=>el.textContent.includes('Compile')&&el.title.includes('fixture guarded compile diagnostic'));"));
  await unlink(join(work,'compile-stub'));
  await chat('Propose two separate candidates.',[mcp('propose_notations',{proposals:[candidate('v_C'),candidate('w_C')]})]);
  await chat('Approve $v_C$; reject $w_C$.',[text('The items have separate decisions.')]);
  check('shared batch decisions remain independent',state().proposals.find(p=>p.canonical==='v_C').status==='approved'&&state().proposals.find(p=>p.canonical==='w_C').status==='rejected');
  await chat('用 h_C 表示 x+x',[text('The direct instruction was registered.')]);
  check('direct human adoption does not fabricate a proposal',state().adoptions.some(a=>a.canonical==='h_C')&&!state().proposals.some(p=>p.canonical==='h_C'));
  await chat('Use $a_i$ for $x+x$ with i in {1,2}.',[text('The finite family was registered.')]);
  await chat('Discuss a permitted member.',[text('The existing $a_2$ remains available.')]);
  check('Claude reuses the shared finite family',state().families.some(f=>f.rule.base==='a')&&evaluate("return document.querySelector('#chat-log').textContent.includes('remains available');"));
  await chat('Reopen $w_C$.',[text('The item can be reconsidered.')]);
  check('explicit reopen only returns to pending',state().proposals.find(p=>p.canonical==='w_C').status==='pending'&&!state().symbols.some(s=>s.canonical==='w_C'));
  await chat('Yes',[text('The author adopted $w_C$.')]);
  check('ordinary follow-up adopts the reopened item',state().proposals.find(p=>p.canonical==='w_C').status==='approved');
  await chat('Keep $x+x+x$ expanded; do not abbreviate it.',[text('The expression remains expanded.')]);
  await chat('Discuss the same expression.',[text('An unchecked abbreviation is $x+x+x=a_1$.')]);
  check('persistent refusal protects visible mathematical chat',!evaluate("return document.querySelector('#chat-log').textContent.includes('An unchecked abbreviation');")&&state().rejections.some(r=>r.kind==='refuse_expression'));
  const decisions=state().decisions.length;
  await writeFile(join(work,'fail-persistence'),'fail');
  await chat('Use f_C for x+x',[text('Must not run.')],'ask',true);
  check('failed registration does not grant a Claude notation',state().decisions.length===decisions&&!state().adoptions.some(a=>a.canonical==='f_C'));
  await unlink(join(work,'fail-persistence'));
  await chat('Use f_C for x+x',[text('The retry was saved.')]);
  cli('reload'); evaluate("for(let i=0;i<200&&(!RESEARCH.state?.initialized||!C.session);i++)await new Promise(r=>setTimeout(r,30));return true;");
  cli('snapshot','-i');cli('select','#chat-model','fast');
  await chat('Discuss existing $a_1$.',[text('The existing $a_1$ remains available after reload.')]);
  check('reload and model switch preserve Conversation and rules',state().families.length===1&&state().adoptions.some(a=>a.canonical==='f_C')&&evaluate('return !!C.session;'));
  await begin('Read an existing reference.',[{type:'web',name:'WebSearch',input:{query:'Existing notation reference'}},text('Continued after Web permission.')]);
  check('Web permission still waits in the same Turn',evaluate("for(let i=0;i<150&&!document.querySelector('.permission:not([data-resolved]) .permission-actions');i++)await new Promise(r=>setTimeout(r,30));return !!C.job&&!!document.querySelector('.permission:not([data-resolved]) .permission-actions');"));
  cli('snapshot','-i');cli('click','.permission:not([data-resolved]) button[data-decision=allow][data-scope=session]');
  check('Web approval resumes the original Claude process',finish()&&evaluate("return document.querySelector('#chat-log').textContent.includes('Continued after Web permission');"));
  const count=state().proposals.length;
  await begin('Continue this stopped draft.',[{type:'delta',text:'Never publish $stopped_'},{type:'sleep',seconds:30}]);
  check('running Claude exposes Stop',evaluate("for(let i=0;i<150&&!C.job;i++)await new Promise(r=>setTimeout(r,30));return !!C.job&&document.querySelector('#chat-send').textContent==='Stop';"));
  cli('snapshot','-i');cli('click','#chat-send');
  check('Stop restores Send without adopting a partial draft',finish()&&state().proposals.length===count&&evaluate("return !document.querySelector('#chat-log').textContent.includes('Never publish');"));
  check('guarded CLI kept the final approved source',evaluate(`return (await Q.file('main.tex')).content===${JSON.stringify(changed)};`));
  if(evaluate("return document.querySelector('#research-body').hidden;"))cli('click','#research-toggle');
  check('report shows scoped human evidence and saved rules',evaluate("return !!document.querySelector('[data-policy=adoptions]')&&!!document.querySelector('[data-policy=families]')&&!!document.querySelector('[data-policy=rejections]');"));
  cli('snapshot','-i');cli('screenshot',join(output,'browser-claude.png'));
} finally {
  await writeFile(join(output,'browser-claude-operations.json'),JSON.stringify(operations,null,2));
  console.log(results.join('\n'));
}
