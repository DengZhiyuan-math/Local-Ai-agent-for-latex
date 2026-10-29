"use strict";
const assert = require("assert"), fs = require("fs"), vm = require("vm");
const Util = require("../prism_local/static/util.js");
const source = fs.readFileSync(require.resolve("../prism_local/static/app.js"), "utf8");
const start = source.indexOf("function githubDiscussionUrl(");
assert.ok(start >= 0, "GitHub discussion view exists");
const end = source.indexOf("\nfunction showResearchRecord(", start);
const helpers = vm.runInNewContext(source.slice(start, end) + ";({githubDiscussionHtml,githubDiscussionStatusHtml,githubDiscussionUrl})", {
  esc: Util.escapeHtml, URL,
});
const status = { mode: "github", bound_repository: "owner/paper",
  repository: { id: 11, full_name: "owner/paper", html_url: "https://github.com/owner/paper" },
  approvers: ["author"], state: "unknown", error: "The result is not known.",
  topics: [{ id: "topic-1", title: "Direct estimate", path: "research/topics/topic-1.md",
    local_revision: 2, published_revision: 0, publication: "unknown" }], comments: [] };
const html = helpers.githubDiscussionHtml(status);
assert.ok(html.includes("Publication outcome unknown"));
assert.ok(html.includes("Local record saved"));
assert.ok(!html.includes("Published on GitHub"), "unknown outcome must not become remote success");
assert.ok(html.includes("do not grant notation permission"));
for (const publication of ["local", "pending", "unknown"]) {
  const view = helpers.githubDiscussionStatusHtml({ ...status,
    topics: [{ ...status.topics[0], publication }] });
  assert.ok(!view.includes("Published on GitHub"), publication);
}
const published = { ...status.topics[0], publication: "published", published_revision: 2,
  issue_url: "https://github.com/owner/paper/issues/12" };
assert.ok(helpers.githubDiscussionStatusHtml({ ...status, state: "ok", topics: [published] })
  .includes("Published on GitHub (revision 2)."));
for (const issue_url of ["javascript:alert(1)", "https://github.com.evil.test/owner/paper/issues/12",
  "https://github.com/other/paper/issues/12", "https://user@github.com/owner/paper/issues/12",
  "https://github.com/owner/paper/issues/12?redirect=https://example.com"]) {
  assert.strictEqual(helpers.githubDiscussionUrl(issue_url, "owner/paper"), null);
  assert.ok(!helpers.githubDiscussionStatusHtml({ ...status, topics: [{ ...published, issue_url }] })
    .includes("Published on GitHub"), "unverified URL cannot establish publication");
}
const comments = [{ id: 51, author: { id: 9, login: "author<script>" },
  preview: "Yes, approved <img src=x>.", url: "https://github.com/owner/paper/issues/12#issuecomment-51",
  approval: true }];
const external = helpers.githubDiscussionStatusHtml({ ...status, comments });
assert.ok(external.includes("External GitHub discussion"));
assert.ok(external.includes("not verified notation approvals"));
assert.ok(external.includes("author&lt;script&gt;"));
assert.ok(!/<script|<img|<svg/.test(external), "external authors and checked previews remain escaped data");
assert.ok(!external.includes("registered for use"), "a mirror flag cannot grant notation authority");
const unbound = helpers.githubDiscussionHtml({ mode: "markdown", bound_repository: null });
assert.ok(unbound.includes('value="github" disabled'));
assert.ok(unbound.includes("Bind this project to a GitHub repository in the Git menu first."));
const hostile = helpers.githubDiscussionHtml({ ...status, bound_repository: '\"><img src=x>',
  approvers: ['\"><script>x</script>'], error: "<svg onload=x>", topics: [{ ...published, title: "<img src=x>" }] });
assert.ok(!/<script|<img|<svg/.test(hostile), "all configuration and status values are escaped");

const Client = require("../prism_local/static/client.js"), calls = [];
const Q = Client.create((path, body) => { calls.push({ path, body }); return Promise.resolve({ _status: 200 }); });
assert.strictEqual(typeof Q.configureDiscussion, "function");
Q.configureDiscussion({ project_key: "project", mode: "github", approvers: ["author"], expected_repository: "owner/paper" });
assert.strictEqual(calls[0].path, "/api/research/discussion");
assert.deepStrictEqual(calls[0].body, { project_key: "project", mode: "github", approvers: ["author"], expected_repository: "owner/paper" });

async function settingsSave() {
  const saved = { initialized: true, github_discussion: { ...status, mode: "markdown", approvers: [] } };
  const RESEARCH = { state: saved, rendered: saved, busy: false }, messages = [], posts = [];
  const fields = { "#research-discussion-mode": { value: "github" },
    "#research-discussion-approvers": { value: " Author, author  Coauthor " } };
  let correctionsDirty = true;
  const context = { esc: Util.escapeHtml, URL, RESEARCH, C: { job: null }, P: { projectKey: "project" },
    $: (selector) => fields[selector], researchCorrectionsDirty: () => correctionsDirty,
    researchMessage: (text, error) => messages.push({ text, error }),
    researchBusy: (busy) => { RESEARCH.busy = busy; },
    Q: { configureDiscussion: async (body) => { posts.push(body); return { ...saved,
      github_discussion: { ...status, approvers: body.approvers } }; } },
    renderResearch: (value) => { RESEARCH.state = value; RESEARCH.rendered = value; },
  };
  const run = vm.runInNewContext(source.slice(start, end)
    + ";({researchDiscussionDirty,saveResearchDiscussion})", context);
  assert.strictEqual(run.researchDiscussionDirty(), true);
  await run.saveResearchDiscussion();
  assert.strictEqual(posts.length, 0, "settings cannot replace unsaved baseline corrections");
  assert.ok(messages.at(-1).text.includes("baseline corrections"));
  assert.strictEqual(fields["#research-discussion-mode"].value, "github");
  assert.strictEqual(fields["#research-discussion-approvers"].value, " Author, author  Coauthor ");
  correctionsDirty = false;
  context.C.job = 7;
  await run.saveResearchDiscussion();
  assert.strictEqual(posts.length, 0, "an active Turn blocks settings changes");
  context.C.job = null;
  await run.saveResearchDiscussion();
  assert.strictEqual(posts.length, 1);
  assert.strictEqual(posts[0].project_key, "project");
  assert.strictEqual(posts[0].mode, "github");
  assert.strictEqual(posts[0].expected_repository, "owner/paper");
  assert.deepStrictEqual(Array.from(posts[0].approvers), ["author", "coauthor"]);
  assert.strictEqual(RESEARCH.busy, false);
  assert.strictEqual(messages.at(-1).text, "Discussion settings saved.");
  assert.ok(!messages.at(-1).text.includes("published"));
  await run.saveResearchDiscussion();
  assert.strictEqual(posts.length, 1, "unchanged settings do not send another update");
  RESEARCH.rendered = saved;
  RESEARCH.state = { ...saved, github_discussion: { ...saved.github_discussion, bound_repository: "owner/changed" } };
  context.Q.configureDiscussion = async (body) => { posts.push(body); return { error: "Repository binding changed." }; };
  await run.saveResearchDiscussion();
  assert.strictEqual(posts[1].expected_repository, "owner/paper", "save confirms the visible repository, not a hidden background replacement");
  assert.strictEqual(fields["#research-discussion-mode"].value, "github");
  assert.strictEqual(fields["#research-discussion-approvers"].value, " Author, author  Coauthor ");
  assert.strictEqual(RESEARCH.rendered, saved, "a failed save keeps the unsaved form");
  assert.strictEqual(messages.at(-1).text, "Repository binding changed.");
  RESEARCH.rendered = { ...saved, github_discussion: { ...saved.github_discussion, bound_repository: null } };
  await run.saveResearchDiscussion();
  assert.strictEqual(posts.length, 2, "GitHub mode cannot silently choose an unbound repository");
  fields["#research-discussion-mode"].value = "markdown";
  fields["#research-discussion-approvers"].value = "future-author";
  context.Q.configureDiscussion = async (body) => { posts.push(body); return { ...saved,
    github_discussion: { ...saved.github_discussion, approvers: body.approvers } }; };
  await run.saveResearchDiscussion();
  assert.strictEqual(posts.length, 3);
  assert.strictEqual(posts[2].mode, "markdown");
  assert.strictEqual("expected_repository" in posts[2], false, "disabling needs no active repository confirmation");
  assert.deepStrictEqual(Array.from(posts[2].approvers), ["future-author"]);
}
async function backgroundRefresh() {
  const saved = { initialized: true, terms: [], symbols: [], github_discussion: { ...status, mode: "markdown", approvers: [] } };
  const RESEARCH = { state: saved, rendered: saved, request: 0, busy: false, seen: new Set() };
  const fields = { "#research-discussion-mode": { value: "github" }, "#research-discussion-approvers": { value: "author" },
    "#research-github-status": { innerHTML: "old status" }, "#research-summary": { textContent: "" } };
  let renderCount = 0, notices = 0;
  const updated = { ...saved, github_discussion: { ...saved.github_discussion, state: "waiting" } };
  const context = { esc: Util.escapeHtml, URL, RESEARCH, $: (id) => fields[id],
    researchCorrectionsDirty: () => false, bindProject: () => true, Q: { research: async () => updated },
    researchMessage: () => { ++notices; }, toast: () => { ++notices; }, researchSummary: () => "Initialized",
    renderResearch: (state) => { ++renderCount; RESEARCH.rendered = state;
      fields["#research-discussion-mode"].value = state.github_discussion.mode;
      fields["#research-discussion-approvers"].value = state.github_discussion.approvers.join(", "); },
  };
  const begin = source.indexOf("async function loadResearch(");
  const stop = source.indexOf('\n$("#research-toggle").onclick', begin);
  const run = vm.runInNewContext(source.slice(start, end) + source.slice(begin, stop) + ";({loadResearch})", context);
  await run.loadResearch();
  assert.strictEqual(fields["#research-discussion-mode"].value, "github", "background data cannot reset an unsaved mode selection");
  assert.strictEqual(fields["#research-discussion-approvers"].value, "author");
  assert.strictEqual(renderCount, 0);
  assert.ok(fields["#research-github-status"].innerHTML.includes("Waiting for a GitHub check."), "live publication status still updates");
  await run.loadResearch();
  assert.strictEqual(notices, 0, "unchanged publication polls do not create notices or save settings");
}
async function correctionsSave() {
  const saved = { initialized: true, terms: [{ name: "compact", meaning: "old" }], symbols: [],
    github_discussion: { ...status, mode: "markdown", approvers: [] } };
  const RESEARCH = { rendered: saved, state: saved, busy: false };
  const term = { value: "new", dataset: { name: "compact" } }, requests = [];
  const fields = { "#research-discussion-mode": { value: "github" }, "#research-discussion-approvers": { value: "coauthor" },
    "#research-notes": { value: "A corrected definition." }, "#research-report": {
      querySelectorAll: (selector) => selector === ".research-term" ? [term] : [],
    } };
  const context = { esc: Util.escapeHtml, URL, RESEARCH, $: (id) => fields[id], C: { job: null }, P: { projectKey: "project" },
    researchMessage: () => {}, researchBusy: (busy) => { RESEARCH.busy = busy; },
    Q: { correctResearch: async (body) => { requests.push(body); return saved; } },
    renderResearch: (state) => { RESEARCH.rendered = state;
      fields["#research-discussion-mode"].value = state.github_discussion.mode;
      fields["#research-discussion-approvers"].value = state.github_discussion.approvers.join(", "); },
  };
  const begin = source.indexOf('$("#research-report").onclick =');
  const stop = source.indexOf("\n// Settings saved before providers existed", begin);
  vm.runInNewContext(source.slice(start, end) + source.slice(begin, stop), context);
  await fields["#research-report"].onclick({ target: { closest: (selector) => selector === "#research-save" ? {} : null } });
  assert.strictEqual(requests.length, 1);
  assert.strictEqual(fields["#research-discussion-mode"].value, "github", "saving corrections keeps the other unsaved form");
  assert.strictEqual(fields["#research-discussion-approvers"].value, "coauthor");
  assert.strictEqual(RESEARCH.busy, false);
}
async function publicationTreePoll() {
  const Watch = require("../prism_local/static/watch.js");
  const tree = (version) => ({ _status: 200, project_key: "paper", root: "paper", files: [], folders: [], order: [],
    pdf_mtime: null, pdf_main: null, github_discussion_version: version });
  const saved = { initialized: true, terms: [], symbols: [], github_discussion: { ...status, mode: "markdown", approvers: [] } };
  const RESEARCH = { state: saved, rendered: saved, githubVersion: "v1", request: 0, busy: false, seen: new Set() };
  const fields = { "#build-status": { textContent: "", className: "status" }, "#btn-restart": { hidden: true },
    "#research-discussion-mode": { value: "github" }, "#research-discussion-approvers": { value: "author" },
    "#research-github-status": { innerHTML: "previous waiting state" }, "#research-summary": { textContent: "" } };
  const S = { tabs: [], watch: tree("v1"), building: false, pdfMtime: null, pdfMain: null };
  let reply = tree("v2"), reads = 0, renders = 0, notifications = 0;
  const context = { esc: Util.escapeHtml, URL, RESEARCH, S, Watch, foreignProject: false, UNREACHABLE: "server not reachable",
    C: { job: null }, BUILD: { loaded: true }, PS: { open: false },
    $: (selector) => fields[selector], TABS: { stamp: () => 1 }, SYNC_TOGGLE: { stamp: () => 1 },
    Q: { tree: async () => reply, research: async () => { ++reads; return saved; } },
    showProjectName: () => {}, bindProject: () => true, isDirty: () => false,
    researchCorrectionsDirty: () => false, researchSummary: () => "Initialized",
    renderResearch: () => { ++renders; }, researchMessage: () => { ++notifications; }, toast: () => { ++notifications; },
  };
  const loadStart = source.indexOf("async function loadResearch("), loadEnd = source.indexOf('\n$("#research-toggle").onclick', loadStart);
  const pollStart = source.indexOf("async function pollOnce()"), pollEnd = source.indexOf("\n/* ------------------------------------------------------------------ GitHub sync", pollStart);
  const run = vm.runInNewContext(source.slice(start, end) + source.slice(loadStart, loadEnd)
    + source.slice(pollStart, pollEnd) + ";({pollOnce})", context);
  await run.pollOnce();
  assert.strictEqual(reads, 1, "private publication change refreshes despite unchanged file metadata");
  assert.strictEqual(RESEARCH.githubVersion, "v2");
  assert.ok(fields["#research-github-status"].innerHTML.includes("Publication outcome unknown"));
  assert.strictEqual(fields["#research-discussion-mode"].value, "github", "publication refresh preserves the unsaved mode");
  assert.strictEqual(fields["#research-discussion-approvers"].value, "author");
  assert.strictEqual(renders, 0, "only publication status is refreshed while settings are dirty");
  await run.pollOnce();
  assert.strictEqual(reads, 1, "unchanged token starts no additional research read");
  context.C.job = 7; reply = tree("v3");
  await run.pollOnce();
  assert.strictEqual(reads, 1, "active Turn defers research refresh");
  assert.strictEqual(S.watch.github_discussion_version, "v3", "the ordinary file watcher may advance independently");
  assert.strictEqual(RESEARCH.githubVersion, "v2", "deferred publication edge is not acknowledged");
  context.C.job = null;
  await run.pollOnce();
  assert.strictEqual(reads, 2, "the deferred edge is consumed after the Turn with the same tree token");
  assert.strictEqual(RESEARCH.githubVersion, "v3");
  RESEARCH.busy = true; reply = tree("v4");
  await run.pollOnce();
  assert.strictEqual(reads, 2);
  assert.strictEqual(RESEARCH.githubVersion, "v3", "busy settings updates do not lose the edge");
  RESEARCH.busy = false;
  await run.pollOnce();
  assert.strictEqual(reads, 3);
  assert.strictEqual(RESEARCH.githubVersion, "v4");
  assert.strictEqual(notifications, 0, "publication polling creates no duplicate notices or saves");
}
settingsSave().then(backgroundRefresh).then(correctionsSave).then(publicationTreePoll).then(() => console.log("ok"), (error) => { console.error(error); process.exitCode = 1; });
