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
  cli("open", url); cli("snapshot", "-i");
  evaluate("for(let i=0;i<100&&(!RESEARCH.state||!P.projectKey);i++)await new Promise(r=>setTimeout(r,30));return true;");
  cli("click", "#research-initialize");
  check("manual initialization completes", evaluate("for(let i=0;i<100&&!RESEARCH.state.initialized;i++)await new Promise(r=>setTimeout(r,30));return RESEARCH.state.initialized;"));
  const original = evaluate("return (await Q.file('main.tex')).content;");
  await chat("Propose one complete candidate.", [call("propose_notation", candidate("z_D"))]);
  check("saved candidate is pending and not adopted", state().proposals[0].status === "pending" && !state().symbols.some(s => s.canonical === "z_D"));
  check("there is no special approval popup", evaluate("return !document.querySelector('dialog[open],.permission-actions');"));

  await chat("Maybe", [[say("Keep the original expression while deciding.")]]);
  check("ambiguous ordinary reply does not approve", state().decisions.length === 0 && state().proposals[0].status === "pending");
  await chat("Show the complete candidate for review again.", [call("propose_notation", candidate("z_D"))]);
  const changed = original.replace(String.raw`$\ell+\ell_+$`, "$z_D$");
  await chat("Yes, I approve.", [call("write_file", { path: "main.tex", content: changed }), [say("The adopted notation is $z_D$.")]], "edit");
  const approved = state();
  check("ordinary approval persists the original human message", approved.proposals[0].status === "approved" && approved.decisions[0].human_text === "Yes, I approve." && approved.decisions[0].actor === "human");
  check("approval releases subsequent source and chat use", evaluate(`return (await Q.file('main.tex')).content===${JSON.stringify(changed)}&&document.querySelector('#chat-log').textContent.includes('The adopted notation');`));
  check("candidate card updates its saved decision", evaluate("return !!document.querySelector('.notation-proposal[data-proposal-status=approved]')&&!!document.querySelector('.notation-decision');"));

  cli("reload");
  evaluate("for(let i=0;i<150&&(!RESEARCH.state?.initialized||!C.session);i++)await new Promise(r=>setTimeout(r,30));return true;");
  check("reload preserves decision and Conversation", state().proposals[0].status === "approved" && evaluate("return !!C.session;"));
  cli("snapshot", "-i"); cli("select", "#chat-model", "fixture-model-2");
  await chat("Discuss the adopted notation with this model.", [[say("The existing $z_D$ remains available.")]]);
  check("model switch keeps known notation and its decision", state().symbols.some(s => s.canonical === "z_D") && state().decisions.length === 1);

  await chat("Propose a different complete candidate.", [call("propose_notation", candidate("w_D"))]);
  await chat("No, I reject.", [[say("Keep the original expression.")]]);
  check("ordinary rejection persists", state().proposals.find(p => p.canonical === "w_D").status === "rejected" && state().decisions.at(-1).decision === "rejected");
  await chat("Continue discussing the original expression.", [call("write_file", { path: "main.tex", content: changed.replace("$z_D$", "$w_D$") })], "edit");
  check("rejected notation does not enter the source or reopen", evaluate(`return (await Q.file('main.tex')).content===${JSON.stringify(changed)};`) && state().proposals.find(p => p.canonical === "w_D").status === "rejected" && !state().symbols.some(s => s.canonical === "w_D"));

  await chat("Propose another complete candidate.", [call("propose_notation", candidate("v_D"))]);
  await writeFile(join(work, "fail-persistence"), "fail");
  await chat("Yes", [[say("Must not run.")]], "ask", true);
  check("failed registration remains pending and reports failure", state().proposals.find(p => p.canonical === "v_D").status === "pending" && state().decisions.length === 2 && evaluate("return document.querySelector('#chat-log').textContent.includes('could not be saved');"));
  await unlink(join(work, "fail-persistence"));
  await chat("不同意", [[say("Keep its original expression.")]]);
  check("ordinary retry can save rejection", state().proposals.find(p => p.canonical === "v_D").status === "rejected" && state().decisions.length === 3);
  cli("reload");
  evaluate("for(let i=0;i<150&&!RESEARCH.state?.initialized;i++)await new Promise(r=>setTimeout(r,30));return true;");
  check("reload restores approval and both rejections", state().proposals.filter(p => p.status === "approved").length === 1 && state().proposals.filter(p => p.status === "rejected").length === 2);
  cli("snapshot", "-i");cli("screenshot", join(output, "browser-decisions.png"));
} finally {
  await writeFile(join(output, "browser-decisions-operations.json"), JSON.stringify(operations, null, 2));
  console.log(results.join("\n"));
}
