"use strict";
const assert = require("assert"), fs = require("fs"), vm = require("vm");
const Util = require("../prism_local/static/util.js");
const source = fs.readFileSync(require.resolve("../prism_local/static/app.js"), "utf8");
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const cards = [];
const log = { querySelectorAll: (selector) => cards.filter((card) => selector === ".research-record"
  ? card.kind === "saved" : card.kind === "error") };
const helpers = vm.runInNewContext(section("function researchLocations(", "\nfunction renderResearch(")
  + ";({researchTopicsHtml,showResearchRecord})", {
  esc: Util.escapeHtml,
  $: (selector) => { assert.strictEqual(selector, "#chat-log"); return log; },
  chatAppend: (html, cls) => {
    assert.strictEqual(cls, "card");
    const saved = html.match(/data-research-record="([^"]*)"/);
    const error = html.match(/data-research-record-error="([^"]*)"/);
    const unescape = (text) => text.replace(/&quot;/g, '"').replace(/&#39;/g, "'")
      .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
    const card = { html, kind: saved ? "saved" : "error", dataset: saved
      ? { researchRecord: unescape(saved[1]) } : { researchRecordError: unescape(error[1]) } };
    cards.push(card);
    return card;
  },
});
const topic = { id: "topic-1", title: "Direct estimate", path: "research/topics/topic-1.md",
  sha256: "a".repeat(64), revision: 1, related_topics: [] };
const html = helpers.researchTopicsHtml({ topics: [topic], pending_count: 1,
  errors: ["Disk is full <retry>"] });
for (const text of ["Research topics (1)", "Direct estimate", 'data-research-source="research/topics/topic-1.md"',
  "1 topic update pending", "not saved yet", "Disk is full &lt;retry&gt;"]) assert.ok(html.includes(text), text);
assert.strictEqual(helpers.researchTopicsHtml(), "", "no empty section before a topic exists");
const receivedTopic = { ...topic, source_state: "unverified_received_reference",
  source_warning: "The retained received prefix is a reference, not local author decisions or notation adoption." };
for (const revision of [1, 2]) {
  const received = helpers.researchTopicsHtml({ topics: [{ ...receivedTopic, revision }] });
  for (const value of ["Unverified received reference", receivedTopic.source_warning,
    'data-research-source="research/topics/topic-1.md"']) assert.ok(received.includes(value), value);
  assert.ok(!received.includes("Verified local"), "retaining and continuing a received topic does not verify its earlier prefix");
}
assert.ok(!helpers.researchTopicsHtml({ topics: [topic] }).includes("Unverified received reference"), "ordinary local topics retain their existing presentation");
const maliciousReceived = helpers.researchTopicsHtml({ topics: [{ ...receivedTopic,
  source_warning: '<img src=x><script>imported decision</script>' }] });
assert.ok(maliciousReceived.includes("&lt;img src=x&gt;") && !/<img|<script/.test(maliciousReceived), "received source warnings are escaped reference text");
assert.ok(!/<script|<img|<svg/.test(helpers.researchTopicsHtml({ topics: [{ ...topic,
  title: "<script>title</script>", path: '\"><img src=x>', sha256: '<svg onload="x">' }],
  errors: [{ message: "<img src=x>" }] })), "all server topic fields are escaped");

const event = { t: "research_record", topic_id: topic.id, ...topic };
helpers.showResearchRecord(event);
assert.strictEqual(cards.length, 1);
assert.ok(cards[0].html.includes('data-file="research/topics/topic-1.md"'));
assert.ok(cards[0].html.includes("Research topic saved"));
helpers.showResearchRecord(event);
assert.strictEqual(cards.length, 1, "a replay uses the card already present in the restored log");
helpers.showResearchRecord({ ...event, revision: 2 });
assert.strictEqual(cards.length, 2, "a new revision has its own saved-location notice");
helpers.showResearchRecord({ ...event, topic_id: "unsafe", title: "<script>x</script>", path: '\"><img src=x>', revision: 3 });
assert.ok(!/<script|<img/.test(cards.at(-1).html), "saved notices escape title and file attributes");
const failure = { t: "research_record_error", topic_id: topic.id, message: "Save failed <retry>", retryable: true };
helpers.showResearchRecord(failure);
assert.ok(cards.at(-1).html.includes("not saved"));
assert.ok(cards.at(-1).html.includes("&lt;retry&gt;"));
assert.ok(!cards.at(-1).html.includes("data-file"), "a failed save does not advertise a saved file");
const count = cards.length;
helpers.showResearchRecord(failure);
assert.strictEqual(cards.length, count, "error event replay does not duplicate its notice");
helpers.showResearchRecord({ t: "research_record", topic_id: "incomplete", title: "Unverified" });
assert.strictEqual(cards.length, count, "an incomplete success event cannot claim a saved location");
console.log("ok");
