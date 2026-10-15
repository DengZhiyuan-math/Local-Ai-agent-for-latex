// Checks of the pages' plain modules (prism_local/static) under Node, run by
// tests/test_frontend.py. Each module ends with module.exports; nothing here has a page.
"use strict";
const assert = require("assert");
const Util = require("../prism_local/static/util.js");

const failures = [];
const pending = [];
function check(name, fn) {
  try {
    const r = fn();
    if (r && typeof r.then === "function") pending.push(r.catch((e) => failures.push(`${name}: ${e.message}`)));
  } catch (e) { failures.push(`${name}: ${e.message}`); }
}

// ---------------------------------------------------------------- util.js
check("ago", () => {
  const now = 1_000_000;
  assert.strictEqual(Util.ago(0, now), "");
  assert.strictEqual(Util.ago(now - 30, now), "just now");
  assert.strictEqual(Util.ago(now - 125, now), "2 min ago");
  assert.strictEqual(Util.ago(now - 2 * 3600, now), "2 h ago");
  assert.strictEqual(Util.ago(now - 3 * 86400, now), "3 d ago");
});

check("plural", () => {
  assert.strictEqual(Util.plural(1, "file"), "1 file");
  assert.strictEqual(Util.plural(2, "file"), "2 files");
  assert.strictEqual(Util.plural(3, "pass", "passes"), "3 passes");
});

check("diffHtml marks lines and escapes them", () => {
  const html = Util.diffHtml("--- a\n+++ b\n@@ -1 +1 @@\n-old <x>\n+new & y\n same");
  assert.ok(html.includes('<span class="del">-old &lt;x&gt;</span>'), html);
  assert.ok(html.includes('<span class="add">+new &amp; y</span>'), html);
  assert.ok(html.includes('<span class="hunk">@@ -1 +1 @@</span>'), html);
  assert.ok(html.startsWith("--- a\n+++ b"), "file headers are not changes");
});

check("waitForRestart: until another server answers, or gives up", async () => {
  const fast = { ms: 0, sleep: () => Promise.resolve() };
  let n = 0;
  const probe = () => { n++; if (n < 3) throw new Error("not yet"); return { pid: n < 5 ? 1 : 2 }; };
  assert.strictEqual(await Util.waitForRestart(probe, (a) => a.pid !== 1, { tries: 10, ...fast }), true);
  assert.strictEqual(n, 5);
  assert.strictEqual(await Util.waitForRestart(() => ({ pid: 1 }), (a) => a.pid !== 1, { tries: 3, ...fast }), false);
});

check("publishState", () => {
  const logged = { logged_in: true };
  assert.deepStrictEqual(Util.publishState({ kind: "own", gh: logged, private_dirs: [{ dir: "notes" }, { dir: "x", ignored: true }] }),
    { why: "ok", blocked: false, gh: logged, leaveOut: [{ dir: "notes" }] });
  assert.strictEqual(Util.publishState({ kind: "nested", gh: logged }).why, "nested");
  assert.strictEqual(Util.publishState({ kind: "own", gh: {} }).why, "login");
  assert.strictEqual(Util.publishState({ kind: "shared", gh: logged }).blocked, false);
});

// ---------------------------------------------------------------- syncstate.js
const SyncState = require("../prism_local/static/syncstate.js");
check("describeSync: paused shows the bound and the current target", () => {
  const bound = { repository_identity: "github.com/owner/paper", branch_ref: "refs/heads/main" };
  const d = SyncState.describeSync({ own: true, enabled: true, remote: true, state: "idle", target: null,
                                     bound_target: bound, blocked_reason: "Fetch and push differ" }, () => "now");
  assert.strictEqual(d.text, "Sync paused");
  assert.strictEqual(d.cls, "warn");
  assert.ok(d.detail.includes("github.com/owner/paper, branch main"), d.detail);
  assert.ok(d.detail.includes("Current target: unavailable"), d.detail);
});
check("describeSync: what is due, and synced", () => {
  const base = { own: true, enabled: true, remote: true, state: "idle" };
  assert.match(SyncState.describeSync({ ...base, pending: true, next_autosave: 90 }, () => "").detail, /in about 2 min/);
  assert.strictEqual(SyncState.describeSync({ ...base, ahead: 2 }, () => "").text, "2 to push");
  assert.strictEqual(SyncState.describeSync({ ...base, state: "pulling" }, () => "").text, "Pulling…");
  const ok = SyncState.describeSync({ ...base, last_push: 5, target: { repository_identity: "github.com/o/p", branch_ref: "refs/heads/main" } },
                                    () => "3 min ago");
  assert.deepStrictEqual([ok.text, ok.detail], ["Saved to GitHub", "Synced to github.com/o/p, branch main, 3 min ago."]);
  assert.strictEqual(SyncState.describeSync({ own: false }, () => "").text, "Not on GitHub");
});

// ---------------------------------------------------------------- chatlogic.js
const Chat = require("../prism_local/static/chatlogic.js");
check("a conversation belongs to its project and provider", () => {
  assert.strictEqual(Chat.storageKey("session", "paper", "codex"), "chat.paper.codex.session");
  assert.notStrictEqual(Chat.storageKey("log", "paper", "codex"), Chat.storageKey("log", "tool", "codex"));
  assert.notStrictEqual(Chat.storageKey("context", "paper", "codex"), Chat.storageKey("context", "paper", "deepcode"));
  assert.strictEqual(Chat.storageKey("session", null, "codex"), null, "no project known yet: nowhere");
  assert.strictEqual(Chat.storageKey("model", "paper", "codex"), "chat.model.codex", "settings are per provider");
});
check("parseMentions: editable project files, once, with their lines", () => {
  const files = [{ path: "main.tex" }, { path: "sec/a.tex" }, { path: "fig.png", editable: false }];
  const m = Chat.parseMentions("see @main.tex:3-5, @sec/a.tex. and @main.tex:3-5 again; @fig.png @missing.tex", files);
  assert.deepStrictEqual(m, [{ token: "@main.tex:3-5", file: "main.tex", from: 3, to: 5 },
                             { token: "@sec/a.tex", file: "sec/a.tex", from: null, to: null }]);
});
check("an unknown /command keeps the selected text", () => {
  assert.strictEqual(Chat.slashPrompt("/review @main.tex:1-2", "SELECTED_TEXT"), "/review @main.tex:1-2\n\nSELECTED_TEXT");
  assert.strictEqual(Chat.slashPrompt("/review", ""), "/review");
});
check("completions: labels, citations and commands", () => {
  const symbols = { labels: [{ label: "thm:main", kind: "theorem", file: "sec/a.tex", line: 4 }, { label: "eq:one", kind: "", file: "main.tex", line: 9 }],
                    bibkeys: [{ key: "knuth84", type: "book" }], macros: [{ name: "Rbb" }], env_titles: { theorem: "Theorem" } };
  const ref = Chat.completions("see \\cref{thm", symbols, false);
  assert.deepStrictEqual([ref.word, ref.items.map((i) => i.text)], ["thm", ["thm:main"]]);
  assert.strictEqual(ref.items[0].kind, "Theorem · a.tex:4");
  assert.deepStrictEqual(Chat.completions("\\cite[p.~3]{kn", symbols, false).items, [{ text: "knuth84", kind: "book" }]);
  assert.strictEqual(Chat.completions("\\R", symbols, false), null, "one letter: only when asked");
  assert.deepStrictEqual(Chat.completions("\\R", symbols, true).items.find((i) => i.text === "Rbb"),
                         { text: "Rbb", kind: "macros.tex" });
  assert.strictEqual(Chat.completions("plain text", symbols, true), null);
});

// ---------------------------------------------------------------- homeview.js
const HV = require("../prism_local/static/homeview.js");
function library() {
  return {
    folders: [{ id: "t", name: "Topic" }, { id: "s", name: "Sub", parent: "t" }, { id: "e", name: "Empty" }],
    projects: [{ id: "a", name: "alpha", folder_id: "s", edited: 30, opened: 5, tags: ["draft"] },
               { id: "b", name: "beta", folder_id: "t", edited: 10, opened: 50, pinned: true },
               { id: "c", name: "gamma", edited: 20, tags: ["draft", "old"] }],
    tagColors: { done: { color: "#000" } }, view: { kind: "all" }, sortKey: "name", sortDir: 1,
  };
}
check("folders, their trees and times", () => {
  const H = library();
  assert.deepStrictEqual(HV.folderTree(H).map(({ f, depth }) => `${f.name}:${depth}`), ["Empty:0", "Topic:0", "Sub:1"]);
  assert.deepStrictEqual(HV.folderPath(H, "s").map((f) => f.id), ["t", "s"]);
  assert.strictEqual(HV.folderTime(H, H.folders[0], "edited"), 30, "a subfolder's project counts");
  assert.deepStrictEqual(HV.sortFolders(H, [...H.folders], "opened").map((f) => f.id), ["t", "s", "e"], "empty ones last");
});
check("a view's items: a folder's own, a search through it, the pinned", () => {
  const H = library();
  assert.deepStrictEqual(HV.viewItems(H, "").items.map((i) => i.key), ["f:e", "f:t", "p:c"]);
  H.view = { kind: "folder", id: "t" };
  assert.deepStrictEqual(HV.viewItems(H, "").items.map((i) => i.key), ["f:s", "p:b"]);
  const found = HV.viewItems(H, "alpha");
  assert.deepStrictEqual([found.flat, found.items.map((i) => i.key)], [true, ["p:a"]], "the search looks in subfolders");
  H.view = { kind: "pinned" };
  assert.deepStrictEqual(HV.viewItems(H, "").items.map((i) => i.key), ["p:b"]);
});
check("order: folders first, then the chosen column", () => {
  const H = library();
  const items = [HV.pItem(H.projects[0]), HV.fItem(H.folders[2]), HV.pItem(H.projects[2])];
  H.sortKey = "edited"; H.sortDir = -1;
  assert.deepStrictEqual(HV.sortItems(H, items, false).map((i) => i.key), ["f:e", "p:a", "p:c"]);
});
check("tags by name or by count", () => {
  const H = library();
  assert.deepStrictEqual(HV.allTags(H).map((t) => t.name), ["done", "draft", "old"]);
  H.tagSort = "count";
  assert.deepStrictEqual(HV.allTags(H).map((t) => `${t.name}:${t.n}`), ["draft:2", "old:1", "done:0"]);
});

Promise.all(pending).then(() => {
  if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
  }
  console.log("ok");
});
