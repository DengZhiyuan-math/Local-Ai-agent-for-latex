"use strict";
const assert = require("assert"), fs = require("fs"), vm = require("vm");
const Util = require("../prism_local/static/util.js");
const source = fs.readFileSync(require.resolve("../prism_local/static/app.js"), "utf8");
const section = (from, to) => source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from)));
const helpersCode = section("function receivedSourceLocations(", "\nfunction researchTopicsHtml(");
const helpers = vm.runInNewContext(helpersCode + ";({receivedResearchHtml})", { esc: Util.escapeHtml,
  researchLocations: (sources) => sources.map((s) => `<button data-research-source="${Util.escapeHtml(s.path)}">${Util.escapeHtml(s.path)}:${s.line || 1}</button><code>${Util.escapeHtml(s.sha256)}</code>`).join(""),
});
const question = { id: "received-1", notation: "z_A", canonical: "z_A", question: "The received source uses z_A. May I adopt it for this project?",
  sources: [{ path: "main.tex", line: 3, sha256: "a".repeat(64) }] };
const state = { initialized: true, provenance: { record_state: "received", baseline_verified: false,
  warning: "Received record is unverified.", received_sha256: "b".repeat(64), received_project_key: "foreign-project",
  received_symbols: [{ notation: "z_A", meaning: "", sources: question.sources }], received_references: { question: "received baseline", symbols: "received library" } },
  received_confirmations: [question] };
const html = helpers.receivedResearchHtml(state);
for (const value of ["Unverified received research material", "z_A", "Meaning unresolved", "received baseline", "received library", question.question,
  'id="research-received-confirmations"', 'data-received-id="received-1"', 'data-research-source="main.tex"', "main.tex:3", "a".repeat(64)]) assert.ok(html.includes(value), value);
assert.ok(!html.includes("registered without another approval") && !html.includes("verified manual edit"));
assert.ok(!/<button[^>]*>.*(?:Approve|Adopt|Confirm)/.test(html), "received material asks an ordinary question without an adoption button");
const unsafe = helpers.receivedResearchHtml({ ...state, received_confirmations: [{ ...question, notation: "<img src=x>", question: "<script>x</script>",
  sources: [{ path: "/private/checkpoints/received.json", line: 1, sha256: "a".repeat(64) }] }] });
assert.ok(!/<img|<script/.test(unsafe));
assert.ok(!unsafe.includes('data-research-source="/private/'), "private checkpoint paths never become project file links");
for (const record_state of ["malformed", "foreign", "pending"]) assert.ok(helpers.receivedResearchHtml({ ...state,
  provenance: { ...state.provenance, record_state, project_record_pending: true } }).includes("Unverified"));
function chatHarness() {
  const log = { children: [], querySelectorAll: (selector) => log.children.filter((card) => card.className.split(" ").includes(selector.slice(1))) };
  let saved = 0;
  return { log, get saved() { return saved; }, saveChatLog: () => { ++saved; }, chatAppend: (html, cls) => {
    const card = { innerHTML: html, className: cls || "", dataset: {}, textContent: html.replace(/<[^>]*>/g, "") };
    log.children.push(card); return card;
  } };
}
function visibleQuestionContext() {
  const input = { value: "" }, RECEIVED = { current: { scope: null, ids: [], ambiguous: false }, draft: null };
  const base = { esc: Util.escapeHtml, RECEIVED, P: { projectKey: "paper" }, C: { provider: "api", session: "session-1" },
    $: (id) => id === "#chat-input" ? input : undefined, researchLocations: () => "" };
  const hidden = vm.runInNewContext(helpersCode + ";({presentReceivedQuestions,receivedDraftInput,receivedReplyContext})", base);
  hidden.presentReceivedQuestions(state); input.value = "Yes"; hidden.receivedDraftInput();
  assert.deepStrictEqual(Array.from(hidden.receivedReplyContext().received_subjects), [], "hidden Research state cannot authorize an unseen received question");
  input.value = ""; hidden.receivedDraftInput();
  const chat = chatHarness(); base.$ = (id) => id === "#chat-input" ? input : id === "#chat-log" ? chat.log : undefined;
  base.chatAppend = chat.chatAppend; base.saveChatLog = chat.saveChatLog;
  const run = vm.runInNewContext(helpersCode + ";({presentReceivedQuestions,receivedDraftInput,receivedReplyContext,markReceivedAiQuestion})", base);
  run.presentReceivedQuestions(state);
  assert.strictEqual(chat.log.children.length, 1); assert.ok(chat.log.children[0].innerHTML.includes(question.question));
  input.value = "Yes"; run.receivedDraftInput();
  assert.deepStrictEqual(Array.from(run.receivedReplyContext().received_subjects), [question.id], "an actual editor chat question supplies its exact ID");
  run.presentReceivedQuestions(state); assert.strictEqual(chat.log.children.length, 1); assert.strictEqual(chat.saved, 1);
  input.value = ""; run.receivedDraftInput(); RECEIVED.current = { scope: null, ids: [], ambiguous: false };
  run.presentReceivedQuestions(state); assert.strictEqual(chat.log.children.length, 1, "restored conversation cards are not repeated after reload");
  run.markReceivedAiQuestion("Should I change the proof first?"); input.value = "Yes"; run.receivedDraftInput();
  assert.strictEqual(run.receivedReplyContext().received_context_ambiguous, true, "a later ordinary AI question cannot inherit a source-question Yes");
  input.value = ""; run.receivedDraftInput();
  chat.chatAppend("May I use w_A instead?", "msg assistant");
  run.presentReceivedQuestions({ ...state, received_confirmations: [{ ...question, id: "received-2" }] });
  input.value = "Yes"; run.receivedDraftInput();
  assert.strictEqual(run.receivedReplyContext().received_context_ambiguous, true, "a new source question cannot capture Yes from an older AI question");
  input.value = ""; run.receivedDraftInput(); run.presentReceivedQuestions({ ...state, received_confirmations: [] });
  input.value = "Yes"; run.receivedDraftInput();
  run.presentReceivedQuestions({ ...state, received_confirmations: [{ ...question, id: "received-3" }] });
  assert.deepStrictEqual(Array.from(run.receivedReplyContext().received_subjects), [], "new question cards cannot retarget a pre-existing draft");
  assert.strictEqual(run.receivedReplyContext().received_context_ambiguous, true);
  input.value = ""; run.receivedDraftInput();
  run.presentReceivedQuestions({ ...state, received_confirmations: [], proposals: [{ id: "ordinary-w", status: "pending" }] });
  input.value = "Yes"; run.receivedDraftInput();
  assert.deepStrictEqual(Array.from(run.receivedReplyContext().received_subjects), []);
  assert.strictEqual(run.receivedReplyContext().received_context_ambiguous, false, "ordinary Agent proposal approval is not intercepted when no received questions exist");
}
async function replyBinding() {
  const RECEIVED = { current: { scope: null, ids: [], ambiguous: false }, draft: null };
  const chat = chatHarness();
  const input = { value: "" }, P = { projectKey: "paper" }, C = { provider: "api", session: "session-1" };
  const context = { esc: Util.escapeHtml, RECEIVED, P, C, $: (id) => id === "#chat-input" ? input : id === "#chat-log" ? chat.log : undefined,
    chatAppend: chat.chatAppend, saveChatLog: chat.saveChatLog, researchLocations: () => "" };
  const run = vm.runInNewContext(helpersCode + ";({presentReceivedQuestions,captureReceivedDraft,receivedReplyContext,receivedDraftInput,markReceivedAmbiguous})", context);
  run.presentReceivedQuestions(state);
  input.value = "  Yes\n"; run.receivedDraftInput();
  const newer = { ...state, received_confirmations: [{ ...question, id: "received-2" }] };
  run.presentReceivedQuestions(newer);
  assert.deepStrictEqual(Array.from(run.receivedReplyContext().received_subjects), ["received-1"], "polling cannot retarget an existing human draft");
  run.markReceivedAmbiguous();
  assert.strictEqual(run.receivedReplyContext().received_context_ambiguous, true, "a later AI proposal makes both reply contexts ambiguous");
  C.provider = "other";
  assert.strictEqual(run.receivedReplyContext().received_context_ambiguous, true, "provider changes cannot reuse an old draft as a new received reply");
  input.value = ""; run.receivedDraftInput();
  run.presentReceivedQuestions(newer); input.value = "Use z_A."; run.receivedDraftInput();
  assert.deepStrictEqual(Array.from(run.receivedReplyContext().received_subjects), ["received-2"]);
  assert.strictEqual(run.receivedReplyContext().received_context_ambiguous, false);
  RECEIVED.draft.stale = true;
  run.presentReceivedQuestions({ ...newer, received_confirmations: [{ ...question, id: "received-3" }] });
  assert.deepStrictEqual(Array.from(run.receivedReplyContext().received_subjects), ["received-2"]);
  input.value = "Use z_A after reviewing this source."; run.receivedDraftInput();
  assert.deepStrictEqual(Array.from(run.receivedReplyContext().received_subjects), ["received-3"], "editing after a stale response starts an explicit reply to the updated visible question");
}
async function realHumanRequest() {
  const chat = chatHarness();
  const input = { value: "" }, fields = { "#chat-input": input, "#slash-menu": { hidden: false }, "#chat-mode": { value: "edit" } };
  fields["#chat-log"] = chat.log;
  const RECEIVED = { current: { scope: null, ids: [], ambiguous: false }, draft: null };
  const P = { projectKey: "paper" }, C = { provider: "api", session: "session-1", model: "fixture", effort: null };
  let bodies = [], refreshes = 0, resolveSave;
  const context = { esc: Util.escapeHtml, RECEIVED, P, C, $: (id) => fields[id], S: { active: "main.tex" }, LOCAL: {},
    prov: () => ({ input_types: ["text"] }), provLabel: () => "Fixture", TABS: { saveAll: () => new Promise((resolve) => { resolveSave = resolve; }) },
    ATT: { files: [], block: () => "", render: () => {}, showSync: () => {} }, parseMentions: () => [], referenceBlock: async () => "",
    ChatLogic: { compose: ({ text }) => ({ prompt: text, scope: null }) },
    Q: { agent: async (body) => { bodies.push(body); return { _status: 409, error: "The received source version changed." }; } },
    chatAppend: chat.chatAppend, saveChatLog: chat.saveChatLog, researchLocations: () => "", toast: () => {}, updateScope: () => {},
    loadResearch: async () => { ++refreshes; run.presentReceivedQuestions({ ...state, received_confirmations: [{ ...question, id: "received-3" }] }); },
    researchMessage: () => {},
  };
  const sendCode = section("async function sendMessage()", "\n// A turn's events into the panel");
  const run = vm.runInNewContext(helpersCode + sendCode + ";({sendMessage,presentReceivedQuestions,receivedDraftInput,receivedReplyContext})", context);
  run.presentReceivedQuestions(state);
  input.value = "  Yes\n"; run.receivedDraftInput();
  const sending = run.sendMessage();
  run.presentReceivedQuestions({ ...state, received_confirmations: [{ ...question, id: "received-2" }] });
  resolveSave({ ok: true }); await sending;
  assert.strictEqual(bodies[0].human_text, "  Yes\n", "the actual editor human text is sent without rewriting");
  assert.deepStrictEqual(Array.from(bodies[0].received_subjects), ["received-1"], "the request freezes its displayed question before asynchronous saves");
  assert.strictEqual(input.value, "  Yes\n", "stale-source refusal preserves the user's draft");
  assert.strictEqual(refreshes, 1, "a stale source refreshes the editor question without starting an AI retry");
  assert.deepStrictEqual(Array.from(run.receivedReplyContext().received_subjects), ["received-1"]);
  input.value = "Yes, use z_A after reviewing the new source."; run.receivedDraftInput();
  assert.deepStrictEqual(Array.from(run.receivedReplyContext().received_subjects), ["received-3"]);
  const switching = run.sendMessage();
  C.provider = "other-provider";
  resolveSave({ ok: true }); await switching;
  assert.strictEqual(bodies.length, 1, "a provider/conversation change during save cannot send a captured reply in a different conversation");
  assert.strictEqual(input.value, "Yes, use z_A after reviewing the new source.");
}
function realResearchRender() {
  const chat = chatHarness();
  const elements = {}, RECEIVED = { current: { scope: null, ids: [], ambiguous: false }, draft: null };
  const element = (id) => id === "#chat-log" ? chat.log : elements[id] ||= { innerHTML: "", textContent: "", hidden: false, value: "", setAttribute() {}, querySelectorAll: () => [] };
  const context = { esc: Util.escapeHtml, URL, RESEARCH: { state: null, rendered: null, deferred: false }, RECEIVED,
    P: { projectKey: "paper" }, C: { provider: "api", session: "session-1" }, $: element, chatAppend: chat.chatAppend, saveChatLog: chat.saveChatLog };
  const code = section("function notationGuardDescription(", "\nfunction researchCorrectionsDirty(");
  const run = vm.runInNewContext(code + ";({renderResearch,researchLocations,receivedSourceLocations})", context);
  run.renderResearch({ ...state, initialized: false, received_confirmations: [] });
  assert.strictEqual(elements["#research-report"].hidden, true, "uninitialized records do not appear as an initialized baseline");
  assert.ok(elements["#research-received"].innerHTML.includes("Unverified received research material"), "received references remain visible outside the initialized report");
  assert.ok(!elements["#research-received"].innerHTML.includes("registered without another approval"));
  run.renderResearch({ ...state, symbols: [{ notation: "known", meaning: "Trusted local meaning", sources: [] }],
    rejections: [{ notation: "refused", human_text: "Do not abbreviate this." }] });
  const report = elements["#research-report"].innerHTML;
  assert.ok(elements["#research-received"].innerHTML.includes(question.question));
  assert.strictEqual(chat.log.children.filter((card) => card.className.includes("received-question")).length, 1);
  assert.ok(report.includes("Trusted local meaning") && report.includes("Do not abbreviate this."), "received material does not hide trusted local knowledge or refusals");
  assert.ok(!report.includes("Existing manuscript notation was registered without another approval"));
  const adoption = run.researchLocations([{ path: "main.tex", line: 3, sha256: "a".repeat(64), evidence: "author_confirmation" }]);
  assert.ok(adoption.includes("Author-confirmed adoption") && !adoption.includes("verified manual edit"));
  for (const sources of [[{ path: "main.tex", line: 2, sha256: 17 }], [{ path: { text: "<img src=x>" }, line: 2, sha256: "a".repeat(64) }],
    [{ path: "main.tex", line: { invalid: true }, sha256: "a".repeat(64) }], [{ path: "main.tex", line: -2, sha256: "a".repeat(64) }],
    null, "<script>bad source</script>", { path: "main.tex", line: 2, sha256: "a".repeat(64) }, [null, 17, "<img src=x>"]]) {
    run.renderResearch({ ...state, received_confirmations: [], symbols: [{ notation: "known", meaning: "Trusted local meaning", sources: [] }],
      provenance: { ...state.provenance, received_symbols: [{ notation: "raw", sources }] } });
    const received = elements["#research-received"].innerHTML;
    assert.ok(received.includes("Unverified") && !/<script|<img/.test(received));
    assert.ok(!received.includes("data-research-source"), "malformed source fields remain literal references without a false project link");
    assert.ok(elements["#research-report"].innerHTML.includes("Trusted local meaning"), "malformed received source fields cannot abort the trusted report");
  }
  run.renderResearch({ ...state, received_confirmations: [], provenance: { ...state.provenance, received_symbols: [null, 17, [],
    { notation: { raw: "<script>" }, meaning: { invalid: true }, sources: { raw: "x".repeat(10000) } }] } });
  assert.ok(elements["#research-received"].innerHTML.length < 7000, "invalid references are bounded escaped literals");
  assert.ok(!/<script/.test(elements["#research-received"].innerHTML));
  const library = { path: "research/knowledge.json", sha256: "a".repeat(64), kind: "received_library",
    json_pointer: "/symbols/0", entry_sha256: "b".repeat(64) };
  const librarySource = run.receivedSourceLocations([library]);
  assert.ok(librarySource.includes('data-research-source="research/knowledge.json"') && librarySource.includes("/symbols/0"), "a typed library source links its actual file and displays its exact JSON entry");
  assert.ok(!librarySource.includes("data-line=") && !librarySource.includes("knowledge.json:1"), "a JSON entry does not invent a TeX line");
  const libraryAdoption = run.researchLocations([{ ...library, evidence: "author_confirmation" }]);
  assert.ok(libraryAdoption.includes("/symbols/0") && libraryAdoption.includes("Author-confirmed adoption"));
  assert.ok(!libraryAdoption.includes("data-line=") && !libraryAdoption.includes("knowledge.json:1"));
  for (const invalid of [{ sha256: 17 }, { path: "../knowledge.json" }, { kind: "other" }, { json_pointer: "/symbols/0<script>" },
    { json_pointer: "/symbols/-1" }, { entry_sha256: null }, { line: 1 }]) {
    for (const view of [run.receivedSourceLocations([{ ...library, ...invalid }]), run.researchLocations([{ ...library, ...invalid, evidence: "author_confirmation" }])]) {
      assert.ok(!view.includes("data-research-source") && !/<script/.test(view), "malformed library bindings remain escaped literals");
    }
  }
}
async function receivedTreePoll() {
  const chat = chatHarness();
  const Watch = require("../prism_local/static/watch.js");
  const tree = (version) => ({ _status: 200, project_key: "paper", root: "paper", files: [], folders: [], order: [],
    pdf_mtime: null, pdf_main: null, github_discussion_version: "gh1", research_version: version });
  const elements = {}, RECEIVED = { current: { scope: null, ids: [], ambiguous: false }, draft: null };
  const element = (id) => id === "#chat-log" ? chat.log : elements[id] ||= { innerHTML: "", textContent: "", hidden: false, value: "", setAttribute() {}, querySelectorAll: () => [] };
  const RESEARCH = { state, rendered: state, githubVersion: "gh1", researchVersion: "v1", request: 0, busy: false, seen: new Set() };
  const S = { tabs: [], watch: tree("v1"), building: false, pdfMtime: null, pdfMain: null };
  let reply = tree("v2"), reads = 0, notices = 0, renders = 0;
  let updated = { ...state, received_confirmations: [{ ...question, id: "received-2", question: "May I adopt the updated z_A source?" }] };
  const context = { esc: Util.escapeHtml, URL, RESEARCH, RECEIVED, S, Watch, foreignProject: false, UNREACHABLE: "unreachable",
    P: { projectKey: "paper" }, C: { provider: "api", session: "session-1", job: null }, BUILD: { loaded: true }, PS: { open: false }, $: element,
    TABS: { stamp: () => 1 }, SYNC_TOGGLE: { stamp: () => 1 }, Q: { tree: async () => reply, research: async () => { ++reads; return updated; } },
    showProjectName: () => {}, bindProject: () => true, isDirty: () => false, researchCorrectionsDirty: () => true,
    renderResearch: () => { ++renders; element("#research-notes").value = ""; },
    researchMessage: () => { ++notices; }, toast: () => { ++notices; },
    chatAppend: chat.chatAppend, saveChatLog: chat.saveChatLog,
  };
  const code = section("function notationGuardDescription(", "\nfunction renderResearch(")
    + section("async function loadResearch(", '\n$("#research-toggle").onclick')
    + section("async function pollOnce()", "\n/* ------------------------------------------------------------------ GitHub sync");
  const run = vm.runInNewContext(code + ";({pollOnce,presentReceivedQuestions,receivedDraftInput,receivedReplyContext})", context);
  run.presentReceivedQuestions(state); element("#chat-input").value = "Yes"; run.receivedDraftInput();
  element("#research-notes").value = "Keep this correction.";
  await run.pollOnce();
  assert.strictEqual(reads, 1); assert.strictEqual(renders, 0); assert.strictEqual(RESEARCH.researchVersion, "v2");
  assert.ok(element("#research-received").innerHTML.includes("May I adopt the updated z_A source?"));
  assert.strictEqual(element("#research-notes").value, "Keep this correction.");
  assert.deepStrictEqual(Array.from(run.receivedReplyContext().received_subjects), ["received-1"]);
  await run.pollOnce(); assert.strictEqual(reads, 1); assert.strictEqual(notices, 0);
  reply = { ...tree("v3"), github_discussion_version: "gh2" }; context.C.job = 7;
  await run.pollOnce(); assert.strictEqual(reads, 1); assert.strictEqual(RESEARCH.researchVersion, "v2");
  context.C.job = null; await run.pollOnce();
  assert.strictEqual(reads, 2, "combined provenance/GitHub changes use one research read after the Turn");
  assert.strictEqual(RESEARCH.researchVersion, "v3"); assert.strictEqual(RESEARCH.githubVersion, "gh2");
  RESEARCH.state = { ...updated, initialized: false }; reply = { ...tree("v4"), github_discussion_version: "gh2" };
  await run.pollOnce(); assert.strictEqual(reads, 3, "received questions still refresh before a verified local baseline exists");
  assert.strictEqual((element("#research-received").innerHTML.match(/data-received-id="received-2"/g) || []).length, 1, "unchanged questions do not duplicate cards");
}
Promise.resolve().then(visibleQuestionContext).then(replyBinding).then(realHumanRequest).then(realResearchRender).then(receivedTreePoll).then(() => console.log("ok"), (error) => { console.error(error); process.exitCode = 1; });
