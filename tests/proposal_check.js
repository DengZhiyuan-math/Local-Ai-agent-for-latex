"use strict";
const assert = require("assert"), fs = require("fs"), vm = require("vm");
const Turn = require("../prism_local/static/turn.js");
const Util = require("../prism_local/static/util.js");
const source = fs.readFileSync(require.resolve("../prism_local/static/app.js"), "utf8");
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const helpers = vm.runInNewContext(section("function notationGuardDescription(", "\nfunction researchOpen(")
  + ";({notationGuardDescription,notationProposalHtml,notationPoliciesHtml,researchSummary})", { esc: Util.escapeHtml });
const proposal = { id: "proposal-1", status: "pending", notation: "z_A", canonical: "z_A",
  definition: "A proposed bound", scope: "The next lemma", reason: "Keep the estimate readable",
  source_expression: "x+x", origin: { actor: "agent", channel: "api", provider: "fixture", path: "main.tex" }, needs_details: false };

// A saved candidate is its own event. It does not become message text or a native
// permission request, and the ordinary done event still completes the Turn.
const turn = Turn.create({ now: () => 0 });
const steps = turn.feed([{ t: "message_start" }, { t: "notation_proposal", proposal },
  { t: "done", outcome: "ok", changed: [] }]);
assert.deepStrictEqual(steps.map((step) => step.draw), ["notation_proposal", "done"]);
assert.strictEqual(steps[0].event.proposal, proposal);
assert.strictEqual(turn.changed.length, 0);
assert.deepStrictEqual(turn.end(), []);
const decidedTurn = Turn.create();
assert.deepStrictEqual(decidedTurn.feed([{ t: "notation_decision", decision: { decision: "approved" } }]).map(s => s.draw), ["notation_decision"]);
const html = helpers.notationProposalHtml(proposal);
for (const value of ["Notation candidate", "Pending", "not adopted", "A proposed bound", "The next lemma",
  "Keep the estimate readable", "x+x", "Agent · API proposal · fixture · main.tex", 'data-proposal-status="pending"']) assert.ok(html.includes(value), value);
assert.ok(!/<button|<dialog|onclick|approved/i.test(html), "a candidate has no approval controls or approved state");
const approved = helpers.notationProposalHtml({ ...proposal, status: "approved" });
assert.ok(approved.includes('data-proposal-status="approved"') && approved.includes("Approved — registered for use"), "saved approval is visible per item");
const rejected = helpers.notationProposalHtml({ ...proposal, status: "rejected" });
assert.ok(rejected.includes('data-proposal-status="rejected"') && rejected.includes("Rejected — keep the original expression"), "saved rejection is visible per item");
const policies = helpers.notationPoliciesHtml({
  adoptions: [{ id: "adopt-1", human_text: "Use h for x+x.", action: { kind: "adopt", notation: "h", definition: "x+x", scope: "project" } }],
  families: [{ id: "family-1", human_text: "Use a_i with i in {1,2,3}.", action: { kind: "family", notation: "a_i", rule: { base: "a", indices: ["1", "2", "3"] } } }],
  rejections: [{ id: "reject-1", human_text: "Keep x+x expanded.", action: { kind: "reject", target: "q", rejection_scope: "expression", source_expression: "x+x" } }],
});
for (const value of ["Directly adopted notation", "Notation families", "Rejection scope", "1, 2, 3", "expression", "x+x", "Use h for x+x.", "Keep x+x expanded."]) assert.ok(policies.includes(value), value);
const standaloneRefusal = helpers.notationPoliciesHtml({ rejections: [{ kind: "refuse_expression", source_expression: "x+x", human_text: "Keep x+x expanded." }] });
assert.ok(standaloneRefusal.includes("Rejection scope: expression"), "standalone expression refusal shows its actual scope");
const unsafePolicies = helpers.notationPoliciesHtml({ adoptions: [{ human_text: "<script>unsafe</script>", notation: "<img src=x>", definition: "<svg onload=x>" }] });
assert.ok(!/<script|<img|<svg/.test(unsafePolicies), "all policy and original message content is escaped");
const incomplete = helpers.notationProposalHtml({ id: "x", status: "pending", notation: "q", needs_details: true });
assert.strictEqual((incomplete.match(/Needs details/g) || []).length, 5, "missing metadata is named, never invented");
assert.ok(incomplete.includes("needs more details"));
const unsafe = helpers.notationProposalHtml({ ...proposal, id: '\"><script>id</script>', notation: "<img src=x onerror=x>",
  definition: "<script>definition</script>", source_expression: "</code><svg onload=x>" });
assert.ok(!/<script|<img|<svg/.test(unsafe), "all proposal content is escaped");
assert.ok(unsafe.includes("&lt;script&gt;definition&lt;/script&gt;"));
assert.strictEqual(helpers.researchSummary({ initialized: true, proposals: [proposal, { status: "approved" }] }), "Initialized · 1 pending candidate");
assert.strictEqual(helpers.researchSummary({ initialized: false, proposals: [proposal] }), "Not initialized");
const active = { label: "API", notation_protection: false,
  notation_guard: { application: true, display: true, proposals: true, decisions: false } };
assert.ok(helpers.notationGuardDescription(active).includes("file and chat checks are active"));
assert.ok(helpers.notationGuardDescription(active).includes("Text approval is not available yet"));
assert.ok(helpers.notationGuardDescription({ ...active, notation_guard: { ...active.notation_guard, decisions: true } }).includes("Text approval is available"));
for (const provider of [{ label: "Codex" }, { label: "Claude", notation_protection: true },
  { ...active, notation_guard: { application: true, display: false, proposals: true } }]) {
  assert.ok(helpers.notationGuardDescription(provider).includes("not available"), "incomplete capability cannot claim support");
}
console.log("ok");
