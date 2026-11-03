"use strict";
const assert = require("assert"), fs = require("fs"), vm = require("vm");
const Util = require("../prism_local/static/util.js");
const source = fs.readFileSync(require.resolve("../prism_local/static/app.js"), "utf8");
const start = source.indexOf("function githubDiscussionUrl("), end = source.indexOf("\nfunction showResearchRecord(", start);
const notationStart = source.indexOf("function notationGuardDescription("), notationEnd = source.indexOf("\nfunction researchOpen(", notationStart);
const helpers = vm.runInNewContext(source.slice(notationStart, notationEnd) + source.slice(start, end)
  + ";({githubProposalShareHtml,githubProposalBatchesHtml,githubDecisionEvidenceHtml,notationProposalHtml})", { esc: Util.escapeHtml, URL });
const proposal = { id: "proposal-1", revision: 2, content_hash: "a".repeat(64), status: "pending", needs_details: false, candidate_kind: "identifier",
  notation: "z_A", definition: "A bound", scope: "Lemma 2", reason: "Keep the estimate readable", source_expression: "x+x" };
const discussion = { mode: "github", bound_repository: "owner/paper", repository: { full_name: "owner/paper" },
  topics: [{ id: "topic-1", title: "Estimate <one>" }], proposal_batches: [] };
const state = { initialized: true, proposals: [proposal, { ...proposal, id: "incomplete", needs_details: true },
  { ...proposal, id: "decided", status: "approved" }], github_discussion: discussion };
const html = helpers.githubProposalShareHtml(state);
assert.ok(html.includes('id="research-github-topic"'));
assert.ok(html.includes('<option value="">Choose an existing research topic</option>'), "sharing never guesses a topic");
assert.ok(html.includes('value="topic-1"') && html.includes("Estimate &lt;one&gt;"));
assert.ok(html.includes('class="research-github-proposal"') && html.includes('value="proposal-1"'));
assert.ok(html.includes('data-revision="2"') && html.includes(`data-content-hash="${"a".repeat(64)}"`));
assert.ok(!html.includes('value="incomplete"') && !html.includes('value="decided"'), "only complete pending proposals are selectable");
assert.ok(html.includes("Share selected proposals") && html.includes("does not approve"));
assert.ok(helpers.githubProposalShareHtml({ ...state, github_discussion: { ...discussion, topics: [] } }).includes("Record a research topic first"));
assert.ok(helpers.githubProposalShareHtml({ ...state, proposals: [] }).includes("No complete pending proposals"));
assert.ok(helpers.githubProposalShareHtml({ ...state, github_discussion: { ...discussion, mode: "markdown" } }).includes("Save GitHub discussion mode"));
assert.ok(helpers.githubProposalShareHtml({ ...state, proposals: [{ ...proposal, source_expression: "" }] }).includes('value="proposal-1"'), "a complete new identifier does not require an abbreviation source expression");
for (const missing of [{ source_expression: null }, { definition: " " }, { scope: " " }, { reason: " " }, { candidate_kind: "unresolved_fragment" }]) {
  assert.ok(!helpers.githubProposalShareHtml({ ...state, proposals: [{ ...proposal, ...missing }] }).includes('value="proposal-1"'), "share only complete identifiers with a string source expression");
}
const batch = { id: "batch-1", topic_id: "topic-1", topic_title: "Estimate", subjects: [{ id: proposal.id, revision: 2, content_hash: proposal.content_hash }],
  proposals: [proposal], publication: "pending", issue_url: "https://github.com/owner/paper/issues/12",
  comment_url: "https://github.com/owner/paper/issues/12#issuecomment-51" };
for (const [publication, expected] of [["pending", "Waiting to publish"], ["unknown", "Publication outcome unknown"], ["stale", "Proposal version changed"]]) {
  const view = helpers.githubProposalBatchesHtml({ ...discussion, proposal_batches: [{ ...batch, publication }] });
  assert.ok(view.includes(expected), publication);
  assert.ok(!view.includes("Batch published on GitHub"), "pending, unknown and stale never imply publication");
  for (const value of ["batch-1", "z_A", "A bound", "Lemma 2", "Keep the estimate readable", "x+x", "version 2", "ordinary reply", "prism-batch:batch-1"]) assert.ok(view.includes(value), value);
}
assert.ok(helpers.githubProposalBatchesHtml({ ...discussion, proposal_batches: [{ ...batch, publication: "published" }] }).includes("Batch published on GitHub"));
for (const comment_url of ["javascript:alert(1)", "https://github.com/other/paper/issues/12#issuecomment-51", "https://github.com/owner/paper/issues/12"]) {
  const view = helpers.githubProposalBatchesHtml({ ...discussion, proposal_batches: [{ ...batch, publication: "published", comment_url }] });
  assert.ok(!view.includes("Batch published on GitHub"), "a published mirror needs the exact bound-repository comment URL");
  assert.ok(!view.includes('href="' + comment_url + '"'));
}
const unsafeBatch = helpers.githubProposalBatchesHtml({ ...discussion, proposal_batches: [{ ...batch,
  id: '<img src=x>', topic_title: '<script>x</script>', error: '<svg onload=x>', proposals: [{ ...proposal, definition: '<iframe src=x>' }] }] });
assert.ok(!/<img|<script|<svg|<iframe/.test(unsafeBatch), "batch snapshots are escaped data");
const decision = { id: "receipt-1", decision: "approved", kind: "decision", proposal_id: proposal.id, revision: 2,
  content_hash: proposal.content_hash, subjects: [{ id: proposal.id, revision: 2, content_hash: proposal.content_hash }],
  human_text: "I approve z_A. Keep <this> text.", author: { id: 9, login: "author", type: "User" },
  comment_url: "https://github.com/owner/paper/issues/12#issuecomment-61", comment_id: 61,
  updated_at: "2026-10-29T12:00:00Z", body_sha256: "c".repeat(64), repository: { id: 11, full_name: "owner/paper" },
  issue: { id: 12, number: 12 }, topic_id: "topic-1", batch_id: "batch-1", channel: "github_comment" };
const evidence = helpers.githubDecisionEvidenceHtml({ ...state, github_decisions: [decision] });
for (const value of ["Verified GitHub decision", "author", "proposal-1", "version 2", "I approve z_A. Keep &lt;this&gt; text.", decision.updated_at,
  'href="https://github.com/owner/paper/issues/12#issuecomment-61"']) assert.ok(evidence.includes(value), value);
const partialEvidence = helpers.githubDecisionEvidenceHtml({ ...state, proposals: [proposal, { ...proposal, id: "proposal-w", notation: "w_A" }],
  github_decisions: [{ ...decision, subjects: [...decision.subjects, { ...decision.subjects[0], id: "proposal-w" }] }] });
const subjectLabel = /<b>(.*?)<\/b>/.exec(partialEvidence)?.[1] || "";
assert.ok(subjectLabel.includes("approved") && subjectLabel.includes("z_A") && subjectLabel.includes("version 2"));
assert.ok(!subjectLabel.includes("w_A") && !subjectLabel.includes("proposal-w") && !subjectLabel.includes(proposal.content_hash), "a partial decision labels only its exact target");
assert.ok(partialEvidence.includes('<details><summary>Published proposal context</summary>'), "full batch IDs and hashes remain in a collapsed context");
const instructionEvidence = helpers.githubDecisionEvidenceHtml({ ...state, github_decisions: [{ ...decision, kind: "adopt" }] });
const instructionLabel = /<b>(.*?)<\/b>/.exec(instructionEvidence)?.[1] || "";
assert.ok(instructionLabel.includes("Direct notation instruction: adopt") && !instructionLabel.includes("approved") && !instructionLabel.includes("z_A"), "a replacement instruction does not approve its original target label");
assert.strictEqual(helpers.githubDecisionEvidenceHtml({ ...state, decisions: [decision] }), "", "raw receipts are not a verified provenance projection");
assert.strictEqual(helpers.githubDecisionEvidenceHtml({ ...state, github_discussion: { ...discussion, comments: [{ ...decision, approval: true }] } }), "");
assert.ok(!helpers.githubDecisionEvidenceHtml({ ...state, github_decisions: [{ ...decision, comment_url: "https://github.com.evil.test/owner/paper/issues/12#issuecomment-61" }] }).includes("href="));
const approvedFromGitHub = helpers.notationProposalHtml({ ...proposal, status: "approved" }, [decision]);
assert.ok(approvedFromGitHub.includes("Verified GitHub decision was saved"));
assert.ok(!approvedFromGitHub.includes("Your chat decision was saved"));
assert.ok(!helpers.notationProposalHtml({ ...proposal, status: "approved", revision: 3 }, [decision]).includes("Verified GitHub decision was saved"), "an older version source cannot label the current version");
assert.ok(!helpers.notationProposalHtml({ ...proposal, id: "another", status: "approved" }, [{ ...decision,
  subjects: [...decision.subjects, { ...decision.subjects[0], id: "another" }] }]).includes("Verified GitHub decision was saved"), "batch membership alone does not attribute a decision to every item");
assert.ok(!/<script|<img/.test(helpers.githubDecisionEvidenceHtml({ ...state, github_decisions: [{ ...decision,
  human_text: "<script>x</script>", author: { ...decision.author, login: "<img src=x>" } }] })), "verified original text remains escaped data");
const locationsStart = source.indexOf("function researchLocations("), locationsEnd = source.indexOf("\nfunction researchCoverage(", locationsStart);
const locations = vm.runInNewContext(source.slice(start, end) + source.slice(locationsStart, locationsEnd) + ";researchLocations", { esc: Util.escapeHtml, URL });
const githubSource = { path: "GitHub @author", evidence: "github_decision", url: decision.comment_url, sha256: "d".repeat(64) };
const sourceLink = locations([githubSource]);
assert.ok(sourceLink.includes('href="' + decision.comment_url + '"') && sourceLink.includes("GitHub @author"));
assert.ok(!sourceLink.includes("data-research-source"), "a GitHub comment cannot be opened as a local manuscript file");
assert.ok(!locations([{ ...githubSource, url: "javascript:alert(1)" }]).includes("href="));
assert.ok(!locations([{ ...githubSource, url: "javascript:alert(1)" }]).includes("data-research-source"));
const Client = require("../prism_local/static/client.js"), calls = [];
const Q = Client.create((path, body) => { calls.push({ path, body }); return Promise.resolve({ _status: 200 }); });
assert.strictEqual(typeof Q.shareGitHubProposals, "function");
const shareBody = { project_key: "paper", topic_id: "topic-1", subjects: [{ id: proposal.id, revision: 2, content_hash: proposal.content_hash }] };
Q.shareGitHubProposals(shareBody);
assert.deepStrictEqual(calls[0], { path: "/api/research/github/proposals", body: shareBody });

async function shareSelection() {
  const selected = { value: proposal.id, checked: true, dataset: { revision: "2", contentHash: proposal.content_hash } };
  const fields = { "#research-discussion-mode": { value: "github" }, "#research-discussion-approvers": { value: "" },
    "#research-github-topic": { value: "" }, "#research-report": {
    querySelectorAll: () => selected.checked ? [selected] : [],
  } };
  const RESEARCH = { state, rendered: state, busy: false }, messages = [], posts = [];
  let corrections = false, renders = 0;
  const context = { esc: Util.escapeHtml, URL, RESEARCH, P: { projectKey: "paper" }, C: { job: null },
    $: (id) => fields[id], researchCorrectionsDirty: () => corrections,
    researchMessage: (message) => messages.push(message), researchBusy: (busy) => { RESEARCH.busy = busy; },
    Q: { shareGitHubProposals: async (body) => { posts.push(body); return { ...state, github_discussion: { ...discussion,
      proposal_batches: [{ id: "batch-1", publication: "pending", subjects: body.subjects }] } }; } },
    renderResearch: (saved) => { ++renders; RESEARCH.state = saved; RESEARCH.rendered = saved; selected.checked = false; },
  };
  const run = vm.runInNewContext(source.slice(start, end) + ";({shareGitHubProposals})", context);
  await run.shareGitHubProposals();
  assert.strictEqual(posts.length, 0, "an existing topic must be explicitly selected");
  assert.ok(messages.at(-1).includes("Choose an existing research topic"));
  fields["#research-github-topic"].value = "topic-1";
  corrections = true; await run.shareGitHubProposals();
  assert.strictEqual(posts.length, 0); assert.ok(messages.at(-1).includes("baseline corrections"));
  corrections = false; fields["#research-discussion-mode"].value = "markdown"; await run.shareGitHubProposals();
  assert.strictEqual(posts.length, 0); assert.ok(messages.at(-1).includes("discussion settings"));
  fields["#research-discussion-mode"].value = "github"; context.C.job = 7; await run.shareGitHubProposals();
  assert.strictEqual(posts.length, 0); context.C.job = null;
  RESEARCH.state = { ...state, proposals: [{ ...proposal, revision: 3, content_hash: "b".repeat(64) }] };
  await run.shareGitHubProposals();
  assert.strictEqual(posts.length, 0, "the author-selected version cannot silently become a newer proposal");
  assert.ok(messages.at(-1).includes("changed"));
  assert.strictEqual(selected.checked, true);
  RESEARCH.state = state;
  await run.shareGitHubProposals();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(posts)), [shareBody]);
  assert.strictEqual(renders, 1); assert.strictEqual(RESEARCH.busy, false);
  assert.ok(messages.at(-1).includes("queued") && !messages.at(-1).includes("approved"), "local queue success is not publication or adoption");
  selected.checked = true;
  context.Q.shareGitHubProposals = async () => ({ error: "Repository binding changed." });
  await run.shareGitHubProposals();
  assert.strictEqual(selected.checked, true, "failed sharing preserves the selected proposal and topic");
  assert.strictEqual(fields["#research-github-topic"].value, "topic-1");
  assert.strictEqual(renders, 1);
}
async function approvalTreePoll() {
  const Watch = require("../prism_local/static/watch.js");
  const tree = (version) => ({ _status: 200, project_key: "paper", root: "paper", files: [], folders: [], order: [],
    pdf_mtime: null, pdf_main: null, github_discussion_version: version });
  const selected = { value: proposal.id, checked: true, dataset: { revision: "2", contentHash: proposal.content_hash } };
  const fields = { "#build-status": { textContent: "", className: "status" }, "#btn-restart": { hidden: true },
    "#research-discussion-mode": { value: "github" }, "#research-discussion-approvers": { value: "" },
    "#research-github-topic": { value: "topic-1" }, "#research-report": { querySelectorAll: () => [selected] },
    "#research-github-status": { innerHTML: "old" }, "#research-github-decisions": { innerHTML: "" },
    "#research-proposals": { innerHTML: "old candidate" }, "#research-summary": { textContent: "" } };
  const RESEARCH = { state, rendered: state, githubVersion: "v1", request: 0, busy: false, seen: new Set() };
  const S = { tabs: [], watch: tree("v1"), building: false, pdfMtime: null, pdfMain: null };
  let reply = tree("v2"), reads = 0, renders = 0, notices = 0;
  let updated = { ...state, proposals: [{ ...proposal, status: "approved" }], github_decisions: [decision],
    github_discussion: { ...discussion, proposal_batches: [{ ...batch, publication: "published" }] } };
  const context = { esc: Util.escapeHtml, URL, RESEARCH, S, Watch, foreignProject: false, UNREACHABLE: "unreachable",
    C: { job: null }, BUILD: { loaded: true }, PS: { open: false }, $: (id) => fields[id],
    TABS: { stamp: () => 1 }, SYNC_TOGGLE: { stamp: () => 1 }, Q: { tree: async () => reply, research: async () => { ++reads; return updated; } },
    showProjectName: () => {}, bindProject: () => true, isDirty: () => false, researchCorrectionsDirty: () => false,
    renderResearch: () => { ++renders; fields["#research-github-topic"].value = ""; selected.checked = false; },
    researchMessage: () => { ++notices; }, toast: () => { ++notices; },
  };
  const loadStart = source.indexOf("async function loadResearch("), loadEnd = source.indexOf('\n$("#research-toggle").onclick', loadStart);
  const pollStart = source.indexOf("async function pollOnce()"), pollEnd = source.indexOf("\n/* ------------------------------------------------------------------ GitHub sync", pollStart);
  const run = vm.runInNewContext(source.slice(notationStart, notationEnd) + source.slice(start, end) + source.slice(loadStart, loadEnd)
    + source.slice(pollStart, pollEnd) + ";({pollOnce})", context);
  await run.pollOnce();
  assert.strictEqual(reads, 1); assert.strictEqual(RESEARCH.githubVersion, "v2");
  assert.strictEqual(renders, 0, "a background decision preserves the author's topic and proposal selections");
  assert.strictEqual(selected.checked, true); assert.strictEqual(fields["#research-github-topic"].value, "topic-1");
  assert.ok(fields["#research-github-decisions"].innerHTML.includes("Original comment by author"));
  assert.ok(fields["#research-proposals"].innerHTML.includes("Verified GitHub decision was saved"));
  assert.ok(fields["#research-github-status"].innerHTML.includes("Batch published on GitHub"));
  await run.pollOnce(); assert.strictEqual(reads, 1, "unchanged decision version starts no duplicate read");
  context.C.job = 7; reply = tree("v3");
  updated = { ...updated, github_discussion: { ...updated.github_discussion, bound_repository: "owner/changed", state: "paused" } };
  await run.pollOnce(); assert.strictEqual(reads, 1); assert.strictEqual(RESEARCH.githubVersion, "v2");
  context.C.job = null; await run.pollOnce();
  assert.strictEqual(reads, 2); assert.strictEqual(RESEARCH.githubVersion, "v3");
  assert.ok(fields["#research-github-status"].innerHTML.includes("owner/changed"), "binding changes remain visible while the share draft is preserved");
  assert.strictEqual(selected.checked, true); assert.strictEqual(fields["#research-github-topic"].value, "topic-1");
  assert.strictEqual(notices, 0, "decision and binding polls create no repeated notices");
}
function refreshChoices() {
  const target = { outerHTML: "old choices" }, notes = { value: "Keep this correction draft." }, mode = { value: "markdown" };
  const context = { esc: Util.escapeHtml, URL, RESEARCH: { busy: false, state: { ...state,
    proposals: [{ ...proposal, revision: 3 }], github_discussion: { ...discussion, bound_repository: "owner/changed" } } },
    $: (id) => id === "#research-github-proposals" ? target : id === "#research-notes" ? notes : mode,
    researchMessage: () => {},
  };
  const refresh = vm.runInNewContext(source.slice(start, end) + ";refreshGitHubProposalChoices", context);
  refresh();
  assert.ok(target.outerHTML.includes('data-revision="3"') && target.outerHTML.includes('data-repository="owner/changed"'));
  assert.ok(!target.outerHTML.includes(" checked"), "refresh requires the author to select current proposal versions again");
  assert.strictEqual(notes.value, "Keep this correction draft."); assert.strictEqual(mode.value, "markdown");
}
shareSelection().then(approvalTreePoll).then(refreshChoices).then(() => console.log("ok"), (error) => { console.error(error); process.exitCode = 1; });
