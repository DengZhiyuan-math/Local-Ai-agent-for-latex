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

// ---------------------------------------------------------------- tabs.js
const Tabs = require("../prism_local/static/tabs.js");
// A CodeMirror Doc's generations: every edit is one; isClean(g): no edit since g.
class FakeDoc {
  constructor(text) { this.text = text; this.edits = 0; }
  getValue() { return this.text; }
  setValue(t) { this.text = t; this.edits++; }
  type(s) { this.text += s; this.edits++; }
  changeGeneration() { return this.edits; }
  isClean(g) { return this.edits === g; }
}
// A server: answers in order (a function of the request, or a fixed answer); `hold()`
// makes the next save wait until the test lets it go.
function fakeServer(...answers) {
  const calls = [];
  let gate = null;
  const api = async (path, body) => {
    calls.push(body);
    if (gate) { const g = gate; gate = null; await g.promise; }
    const a = answers.length > 1 ? answers.shift() : answers[0];
    return typeof a === "function" ? a(body) : a;
  };
  const hold = () => { let release; gate = { promise: new Promise((r) => { release = r; }) }; return () => release(); };
  return { api, calls, hold };
}
const saved = (mtime) => ({ _status: 200, ok: true, mtime });
function store(server, extra = {}) {
  const changes = [], notes = [];
  const s = Tabs.createTabStore({ api: server.api, sleep: () => Promise.resolve(), notify: (m) => notes.push(m),
                                  onChange: (t, what) => changes.push(what), ...extra });
  return { s, changes, notes };
}

check("a save sends the text against the mtime it was loaded at", async () => {
  const srv = fakeServer(saved(2));
  const { s, changes } = store(srv);
  const t = s.add("main.tex", new FakeDoc("a"), 1);
  t.doc.type("b");
  assert.deepStrictEqual(await s.save(t), { ok: true });
  assert.deepStrictEqual(srv.calls, [{ path: "main.tex", content: "ab", base_mtime: 1, force: false }]);
  assert.deepStrictEqual([t.mtime, s.dirty(t), changes], [2, false, ["saved"]]);
  assert.deepStrictEqual(await s.save(t), { ok: true }, "nothing to save: no request");
  assert.strictEqual(srv.calls.length, 1);
});

check("one save per tab at a time", async () => {
  const order = [];
  const srv = fakeServer((b) => { order.push("answer " + b.content); return saved(order.length); });
  const { s } = store(srv);
  const t = s.add("main.tex", new FakeDoc(""), 0);
  t.doc.type("1");
  const release = srv.hold();
  const first = s.save(t);
  await new Promise((r) => setImmediate(r));           // its request is on its way
  t.doc.type("2");
  const second = s.save(t);
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(srv.calls.length, 1, "the second waits for the first");
  release();
  await Promise.all([first, second]);
  assert.deepStrictEqual(srv.calls.map((c) => c.content), ["1", "12"]);
  assert.strictEqual(srv.calls[1].base_mtime, 1, "against the first save's mtime");
});

check("git busy is waited out, said once", async () => {
  const srv = fakeServer({ _status: 503, busy: true, error: "git is busy" }, { _status: 503, busy: true }, saved(5));
  const { s, notes } = store(srv);
  const t = s.add("a.tex", new FakeDoc("x"), 1);
  t.doc.type("y");
  assert.deepStrictEqual(await s.save(t), { ok: true });
  assert.deepStrictEqual([srv.calls.length, notes], [3, ["a.tex: git is busy."]]);
});

check("during an agent's Edit turn nothing is written, and it says so once per turn", async () => {
  let held = true;
  const srv = fakeServer(saved(2));
  const { s, notes } = store(srv, { isHeld: () => held });
  const t = s.add("a.tex", new FakeDoc("x"), 1);
  t.doc.type("y");
  assert.deepStrictEqual(await s.save(t), { ok: false, reason: "held" });
  await s.save(t);
  assert.deepStrictEqual([srv.calls.length, notes.length], [0, 1]);
  s.turnStarted();
  await s.save(t);
  assert.strictEqual(notes.length, 2, "a new turn says it again");
  held = false;
  assert.deepStrictEqual(await s.save(t), { ok: true });
});

check("a file changed on disk is not overwritten: the conflict, then Keep mine", async () => {
  const srv = fakeServer({ _status: 409, conflict: true, mtime: 9 }, saved(10));
  const { s, changes } = store(srv);
  const t = s.add("a.tex", new FakeDoc("x"), 1);
  t.doc.type("y");
  assert.deepStrictEqual(await s.save(t), { ok: false, reason: "conflict" });
  assert.deepStrictEqual([t.conflict, changes], ["disk", ["conflict"]]);
  assert.deepStrictEqual(await s.save(t, true), { ok: true }, "force: the author keeps theirs");
  assert.deepStrictEqual([srv.calls[1].force, t.conflict], [true, null]);
});

check("the poll sees a save of ours in flight as no change on disk", async () => {
  const srv = fakeServer(saved(7));
  const { s } = store(srv);
  const t = s.add("a.tex", new FakeDoc("x"), 1);
  t.doc.type("y");
  const release = srv.hold();
  const saving = s.save(t);
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(s.diskSeen(t, 7), "same", "the file on disk is this save's");
  assert.strictEqual(t.conflict, null);
  t.doc.type("z");                                    // typed while the save was on its way
  release();
  await saving;
  assert.deepStrictEqual([t.mtime, s.dirty(t)], [7, true], "what was typed meanwhile is still to save");
});

check("the poll: reload a clean tab, flag an edited one", () => {
  const { s, changes } = store(fakeServer(saved(1)));
  const clean = s.add("a.tex", new FakeDoc("x"), 1), edited = s.add("b.tex", new FakeDoc("x"), 1);
  edited.doc.type("y");
  assert.deepStrictEqual([s.diskSeen(clean, 1), s.diskSeen(clean, 2), s.diskSeen(edited, 2)], ["same", "reload", "conflict"]);
  assert.deepStrictEqual([edited.conflict, changes], ["disk", ["conflict"]]);
});

check("a reload: kept when typed into meanwhile, else the disk's text", () => {
  const { s } = store(fakeServer(saved(1)));
  const t = s.add("a.tex", new FakeDoc("x"), 1);
  assert.strictEqual(s.loaded(t, "x"), "same");
  assert.strictEqual(s.loaded(t, "disk"), "replace");
  t.doc.type("y");
  assert.deepStrictEqual([s.loaded(t, "disk"), t.conflict], ["kept", "disk"]);
  assert.strictEqual(s.loaded(t, "disk", true), "replace", "Load disk version (discard mine)");
  t.doc.setValue("disk");
  s.synced(t, 4);
  assert.deepStrictEqual([t.mtime, t.conflict, s.dirty(t)], [4, null, false]);
});

check("saveAll says why, the reason needing you most", async () => {
  const srv = fakeServer((b) => (b.path === "a.tex" ? { _status: 500, error: "disk full" } : { _status: 409, conflict: true }));
  const { s } = store(srv);
  for (const p of ["a.tex", "b.tex", "c.tex"]) s.add(p, new FakeDoc(""), 0);
  s.tabs[0].doc.type("1"); s.tabs[1].doc.type("2");
  const r = await s.saveAll();
  assert.deepStrictEqual(r, { ok: false, reason: "conflict", paths: ["a.tex", "b.tex"], message: "Save failed: disk full" });
  assert.match(Tabs.unsavedWhy(r), /conflict/);
  assert.match(Tabs.unsavedWhy({ ok: false, reason: "failed", message: "Save failed: disk full" }), /disk full/);
  assert.deepStrictEqual(await store(fakeServer(saved(1))).s.saveAll(), { ok: true });
});

check("closing tabs", () => {
  const { s } = store(fakeServer(saved(1)));
  for (const p of ["a.tex", "sec/b.tex", "sec/c.tex"]) s.add(p, new FakeDoc(""), 0);
  const tabs = s.tabs;
  assert.deepStrictEqual(s.remove((t) => t.path.startsWith("sec/")).map((t) => t.path), ["sec/b.tex", "sec/c.tex"]);
  assert.strictEqual(s.tabs, tabs, "the same list: the page holds it");
  assert.deepStrictEqual([s.tabs.map((t) => t.path), s.get("sec/b.tex")], [["a.tex"], null]);
});

Promise.all(pending).then(() => {
  if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
  }
  console.log("ok");
});
