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
check("describeSync: paused says so in plain words, the bound and current target under details", () => {
  const bound = { repository_identity: "github.com/owner/paper", branch_ref: "refs/heads/main" };
  const d = SyncState.describeSync({ own: true, enabled: true, remote: true, state: "idle", target: null,
                                     bound_target: bound, blocked_reason: "Fetch and push differ" }, () => "now");
  assert.strictEqual(d.text, "Upload paused");
  assert.strictEqual(d.cls, "warn");
  assert.ok(d.detail.includes("your edits are saved on this computer"), d.detail);
  assert.ok(!/target|branch|Fetch/.test(d.detail), d.detail);
  assert.ok(d.tech.includes("github.com/owner/paper, branch main"), d.tech);
  assert.ok(d.tech.includes("Current target: unavailable"), d.tech);
  const said = SyncState.describeSync({ own: true, enabled: true, remote: true, state: "idle", blocked_reason: "x",
                                         blocked_plain: "Uploading to GitHub is paused; because." }, () => "");
  assert.strictEqual(said.detail, "Uploading to GitHub is paused; because.");
});
check("describeSync: what is due, and synced", () => {
  const base = { own: true, enabled: true, remote: true, state: "idle" };
  assert.match(SyncState.describeSync({ ...base, pending: true, next_autosave: 90 }, () => "").detail, /in about 2 min/);
  assert.strictEqual(SyncState.describeSync({ ...base, ahead: 2 }, () => "").text, "Not uploaded yet");
  assert.strictEqual(SyncState.describeSync({ ...base, state: "pulling" }, () => "").text, "Getting changes…");
  const ok = SyncState.describeSync({ ...base, last_push: 5, target: { repository_identity: "github.com/o/p", branch_ref: "refs/heads/main" } },
                                    () => "3 min ago");
  assert.deepStrictEqual([ok.text, ok.detail, ok.tech], ["Saved to GitHub", "Everything is on GitHub, 3 min ago.",
                                                       "Synced to github.com/o/p, branch main."]);
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
  const saveFile = async (body) => {
    calls.push(body);
    if (gate) { const g = gate; gate = null; await g.promise; }
    const a = answers.length > 1 ? answers.shift() : answers[0];
    return typeof a === "function" ? a(body) : a;
  };
  const hold = () => { let release; gate = { promise: new Promise((r) => { release = r; }) }; return () => release(); };
  return { saveFile, calls, hold };
}
const saved = (mtime) => ({ _status: 200, ok: true, mtime });
function store(server, extra = {}) {
  const changes = [], notes = [];
  const s = Tabs.createTabStore({ saveFile: server.saveFile, sleep: () => Promise.resolve(), notify: (m) => notes.push(m),
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
  assert.deepStrictEqual(await s.save(t), { ok: false, reason: "conflict", message: "a.tex changed on disk — not saved. Resolve in the banner." });
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
  const { s, notes } = store(srv);
  for (const p of ["a.tex", "b.tex", "c.tex"]) s.add(p, new FakeDoc(""), 0);
  s.tabs[0].doc.type("1"); s.tabs[1].doc.type("2");
  const r = await s.saveAll();
  const conflict = "b.tex changed on disk — not saved. Resolve in the banner.";
  assert.deepStrictEqual(r, { ok: false, reason: "conflict", paths: ["a.tex", "b.tex"], message: conflict });
  // What the page then says is what saving said already: one notice, not two.
  assert.ok(notes.includes(Tabs.unsavedWhy(r)), Tabs.unsavedWhy(r));
  const failed = { ok: false, reason: "failed", message: "Save failed: disk full" };
  assert.strictEqual(Tabs.unsavedWhy(failed), "Save failed: disk full");
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

// ---------------------------------------------------------------- turn.js
const Turn = require("../prism_local/static/turn.js");
// A clock the test moves; steps as short strings, to compare whole turns at once.
function clock() { const c = { ms: 0, now: () => c.ms }; return c; }
const brief = (steps) => steps.map((s) => {
  if (s.mark) return `mark ${s.mark}${s.path ? " " + s.path : ""}${s.mark === "read" ? ` ${s.from}-${s.to}` : ""}`;
  if (s.view) return `view ${s.view}${s.view === "feed" ? "" : " " + s.id}`;
  if (s.save) return `session ${s.id}`;
  const x = { message: `${s.id} ${JSON.stringify(s.text)}`, tool: `${s.id} ${s.name} ${s.summary}`,
    "tool-result": `${s.id} ${s.ok ? "ok" : "failed"}`, live: `${s.id} ${s.name}`, "live-path": `${s.id} ${s.path}`,
    "live-text": `${s.id} ${JSON.stringify(s.text)}`, "live-close": s.id, "thinking-text": JSON.stringify(s.text),
    "thinking-end": `${s.secs}s ${s.shown ? "shown" : "not shown"}` }[s.draw];
  return x === undefined ? s.draw : `${s.draw} ${x}`;
});

check("a Claude Code turn: thinking, a streamed reply, an Edit written live, then the reply", () => {
  const c = clock(), t = Turn.create({ now: c.now });
  assert.deepStrictEqual(brief(t.feed([{ t: "init", session_id: "s1" }, { t: "message_start" }, { t: "thinking_start" }])),
    ["session s1", "thinking"]);
  c.ms = 3400;
  assert.strictEqual(t.status, "thinking… 3s");
  assert.strictEqual(t.thinking(), 3);
  assert.deepStrictEqual(brief(t.feed([{ t: "thinking", text: "hmm" }, { t: "delta", text: "I will " }, { t: "delta", text: "edit." },
    { t: "tool_start", id: "u1", name: "Edit" }])),
    ['thinking-text "hmm"', "thinking-end 3s shown", 'message 1 "I will edit."', "mark clear-read", "tool u1 Edit "]);
  assert.strictEqual(t.status, "Edit…");
  assert.strictEqual(t.thinking(), null);
  assert.deepStrictEqual(brief(t.feed([{ t: "tool_live", id: "u1", path: "a.tex" }, { t: "tool_live", id: "u1", old: "x", old_done: true },
    { t: "tool_live", id: "u1", text: "one\ntwo\n" }])),
    ["live u1 Edit", "live-path u1 a.tex", "view begin u1", "mark on a.tex", "view feed", "view feed", 'live-text u1 "one\\ntwo\\n"', "view feed"]);
  assert.strictEqual(t.status, "writing a.tex… (3 lines)");
  assert.deepStrictEqual(brief(t.feed([{ t: "tool", id: "u1", name: "Edit", summary: "a.tex", path: "a.tex" },
    { t: "tool_result", id: "u1", error: false, preview: "" }])),
    ["tool u1 Edit a.tex", "mark clear-read", "mark on a.tex", "tool-result u1 ok", "live-close u1", "view end u1"]);
  assert.strictEqual(t.status, "working…");
  // The reply streams, then comes whole (text): drawn once, as one message.
  assert.deepStrictEqual(brief(t.feed([{ t: "message_start" }, { t: "delta", text: "Done" }])), ['message 2 "Done"']);
  assert.strictEqual(t.status, "writing a reply…");
  assert.deepStrictEqual(brief(t.feed([{ t: "delta", text: "." }, { t: "text", text: "Done." }, { t: "context", used: 9 },
    { t: "rate", rate: {} }, { t: "done", changed: [{ path: "a.tex" }] }])),
    ['message 2 "Done."', "context", "done"]);
  assert.deepStrictEqual([t.session, t.changed, brief(t.end())], ["s1", ["a.tex"], []]);
});

check("a Codex turn: whole messages, and tools announced only when they ran", () => {
  const t = Turn.create({ now: clock().now });
  assert.deepStrictEqual(brief(t.feed([{ t: "init", session_id: "th" }, { t: "message_start" }, { t: "text", text: "Looking." },
    { t: "tool", id: "c1", name: "Read", summary: "a.tex", path: "a.tex", lines: [3, 5] },
    { t: "tool_result", id: "c1", error: true, preview: "denied" },
    { t: "message_start" }, { t: "text", text: "Could not." }, { t: "error", message: "x" }, { t: "done", changed: [] }])),
    ["session th", 'message 1 "Looking."', "tool c1 Read a.tex", "mark clear-read", "mark on a.tex", "mark read a.tex 2-4",
      "tool-result c1 failed", "view end c1", 'message 2 "Could not."', "error", "done"]);
  assert.strictEqual(t.status, "working…");
});

check("an OpenAI-compatible turn: reasoning and reply both streamed, across batches", () => {
  const c = clock(), t = Turn.create({ now: c.now });
  assert.deepStrictEqual(brief(t.feed([{ t: "init", session_id: "o" }, { t: "message_start" }, { t: "thinking", text: "a" }])),
    ["session o", "thinking", 'thinking-text "a"']);
  c.ms = 1600;
  assert.deepStrictEqual(brief(t.feed([{ t: "thinking", text: "b" }, { t: "delta", text: "Hi" }])),
    ['thinking-text "b"', "thinking-end 2s shown", 'message 1 "Hi"']);
  assert.deepStrictEqual(brief(t.feed([{ t: "delta", text: " there" }])), ['message 1 "Hi there"']);
  assert.deepStrictEqual(brief(t.feed([])), [], "nothing new: nothing drawn");
});

check("a model that thinks without showing it (Deep Code, Opus) counts the seconds", () => {
  const c = clock(), t = Turn.create({ now: c.now });
  t.feed([{ t: "thinking_start" }]);
  c.ms = 12000;
  assert.strictEqual(t.status, "thinking… 12s");
  assert.deepStrictEqual(brief(t.feed([{ t: "init", session_id: "d" }, { t: "message_start" }, { t: "text", text: "Ok" }])),
    ["thinking-end 12s not shown", "session d", 'message 1 "Ok"']);
});

check("a turn stopped or cut off closes what is still open", () => {
  const c = clock(), t = Turn.create({ now: c.now });
  t.feed([{ t: "tool_live", id: "w", text: "abc" }]);
  assert.strictEqual(t.status, "writing a file… (1 lines)");
  t.feed([{ t: "thinking_start" }]);
  c.ms = 2000;
  assert.deepStrictEqual(brief(t.end()), ["thinking-end 2s not shown", "live-close w"]);
  assert.deepStrictEqual(brief(t.end()), [], "once");
});

check("needsCompile: a changed file newer than the PDF, or not listed yet", () => {
  const files = [{ path: "a.tex", mtime: 10 }, { path: "b.tex", mtime: 30 }];
  assert.strictEqual(Turn.needsCompile([], files, 20), false);
  assert.strictEqual(Turn.needsCompile(["a.tex"], files, 20), false, "its own build has it");
  assert.strictEqual(Turn.needsCompile(["b.tex"], files, 20), true);
  assert.strictEqual(Turn.needsCompile(["new.tex"], files, 20), true);
  assert.strictEqual(Turn.needsCompile(["a.tex"], files, null), true, "no PDF yet");
});

check("environment candidates appear inside begin braces and filter by prefix", () => {
  const symbols = { labels: [], bibkeys: [], macros: [] };
  const all = Chat.completions("\\begin{", symbols, false);
  for (const name of ["equation", "align", "itemize", "enumerate"])
    assert.ok(all.items.some((i) => i.text === name && i.kind === "environment"), name);
  const filtered = Chat.completions("\\begin{ali", symbols, false);
  assert.ok(filtered.items.some((i) => i.text === "align"));
  assert.ok(filtered.items.every((i) => i.text.startsWith("ali")));
  assert.ok(Chat.completions("\\begin{align", symbols, false).items.some((i) => i.text === "align"));
  assert.strictEqual(Chat.completions("% \\begin{ali", symbols, false), null);
  assert.strictEqual(Chat.completions("\\\\begin{ali", symbols, false), null);
});

check("accepting an environment inserts its end and puts the cursor in the indented body", () => {
  const source = "  \\begin{ali}";
  const edit = Chat.environmentEdit(source, source.length - 1, "align", "    ");
  const result = source.slice(0, edit.from) + edit.text + source.slice(edit.to);
  assert.strictEqual(result, "  \\begin{align}\n      \n  \\end{align}");
  assert.strictEqual(edit.cursor, result.indexOf("\n") + 7);
});

check("existing environment ends are reused without taking a nested parent's end", () => {
  const accept = (source, indent = "\t") => {
    const cursor = source.indexOf("|");
    source = source.replace("|", "");
    const edit = Chat.environmentEdit(source, cursor, "align", indent);
    return { text: source.slice(0, edit.from) + edit.text + source.slice(edit.to), cursor: edit.cursor };
  };
  assert.deepStrictEqual(accept("\\begin{ali|}\n  x &= y\n\\end{align}"),
    { text: "\\begin{align}\n  x &= y\n\\end{align}", cursor: 16 });
  assert.strictEqual(accept("\\begin{align}\n\t\\begin{ali|}\n\\end{align}").text,
    "\\begin{align}\n\t\\begin{align}\n\t\t\n\t\\end{align}\n\\end{align}");
  assert.strictEqual(accept("\\begin{ali|}\n\\begin{align}\nx\n\\end{align}\n\\end{align}").text,
    "\\begin{align}\n\\begin{align}\nx\n\\end{align}\n\\end{align}");
  assert.strictEqual(accept("\\begin{ali|} % \\end{align}").text,
    "\\begin{align}\n\t\n\\end{align} % \\end{align}");
});

check("message composition keeps commands first and scopes only mentioned files", () => {
  assert.deepStrictEqual(Chat.compose({ text: "/review", refs: "REF", attached: "FILE", mode: "edit",
    mentions: [{ file: "a.tex" }, { file: "a.tex" }, { file: "b.tex" }] }),
    { prompt: "/review\n\nREF\n\nFILE", scope: ["a.tex", "b.tex"] });
  assert.deepStrictEqual(Chat.compose({ text: "explain", refs: "REF", attached: "FILE", mode: "ask", mentions: [{ file: "a.tex" }] }),
    { prompt: "FILE\n\nREF\n\nexplain", scope: null });
  for (const text of ["/usr/bin/latex fails", "/ what does this do"])           // a path, a lone slash
    assert.strictEqual(Chat.compose({ text, refs: "REF" }).prompt, "REF\n\n" + text, text);
});
check("build descriptions distinguish a stopped build from a PDF with errors", () => {
  assert.strictEqual(Util.describeBuild({ cancelled: true, exit: 1, seconds: 2 }, []).cls, "warn");
  const pdf = Util.describeBuild({ pdf_updated: true, exit: 1, seconds: 3 }, [{ severity: "error" }]);
  assert.strictEqual(pdf.cls, "err"); assert.ok(pdf.text.includes("PDF built with 1 error"));
  assert.strictEqual(Util.describeBuild({ exit: 0, seconds: 1 }, []).text, "OK · 1s");
});

// ---------------------------------------------------------------- mathtext.js
const MathText = require("../prism_local/static/mathtext.js");
const katex = require("../prism_local/static/vendor/katex/katex.min.js");
const marked = require("../prism_local/static/vendor/marked.min.js");
// As the page has it: the reply's HTML, each formula as [display: tex] or [inline: tex].
const MDJS = new marked.Marked({ gfm: true, breaks: true });
const fakeKatex = { renderToString: (tex, o) => `[${o.displayMode ? "display" : "inline"}: ${tex.trim()}]` };
const reply = (text, macros) => MathText.render(text, { markdown: (s) => MDJS.parse(s), katex: fakeKatex, macros });

check("math: what LaTeX writes, Markdown does not touch", () => {
  const cases = [
    ["Let $x_1 * y_2$ and $a^*$, $b^*$.", ["[inline: x_1 * y_2]", "[inline: a^*]", "[inline: b^*]"]],
    ["spaces $ x^2 $ inside", ["[inline: x^2]"]],
    ["over a\nline $a +\n b$ break", ["[inline: a +\n b]"]],
    ["\\(x\\) and \\[y\\]", ["[inline: x]", "[display: y]"]],
    ["$$\n\\begin{aligned} a &= b \\\\ c \\end{aligned}\n$$", ["[display: \\begin{aligned} a &= b \\\\ c \\end{aligned}]"]],
    ["\\begin{align}\na &= b \\\\\nc\n\\end{align}", ["[display: \\begin{align}\na &= b \\\\\nc\n\\end{align}]"]],
    ["| a | b |\n|---|---|\n| $|x|$ | $\\|y\\|$ |", ["[inline: |x|]", "[inline: \\|y\\|]"]],
    ["the $n$th and $x$2", ["[inline: n]", "[inline: x]"]],
    ["由于$x>0$，所以$\\sqrt{x}$有定义。", ["[inline: x>0]", "[inline: \\sqrt{x}]"]],
    ["1. one\n   \\[\n   a = b\n   \\]\n2. two", ["[display: a = b]"]],
  ];
  for (const [text, want] of cases) {
    const html = reply(text);
    for (const w of want) assert.ok(html.includes(w), `${JSON.stringify(text)} → ${html}`);
    assert.ok(!/\$|\\\(|\\\[/.test(html.replace(/\[(inline|display): [\s\S]*?\]/g, "")), `nothing left as source: ${html}`);
  }
  assert.ok(reply("| a | b |\n|---|---|\n| $|x|$ | c |").includes("<td>c</td>"), "the | in math is not a cell");
});

check("math: what is not math", () => {
  const plain = (text) => MathText.split(text).math.length;
  assert.strictEqual(plain("costs $5 and $6"), 0, "money");
  assert.strictEqual(plain("price \\$5 and \\$6"), 0, "escaped");
  assert.strictEqual(plain("`$x$` and ``a $y$ b``"), 0, "code spans");
  assert.strictEqual(plain("```latex\n$x$ \\[y\\]\n```\n"), 0, "a fence");
  assert.strictEqual(plain("~~~\n$x$\n"), 0, "a fence not closed yet");
  assert.strictEqual(plain("a $x\n\ny$ b"), 0, "not over a blank line");
  assert.strictEqual(plain("still coming: $x^2 + "), 0, "not closed (streaming)");
  assert.strictEqual(plain("$$ $$ and $ $"), 0, "empty");
  assert.strictEqual(plain("\\begin{theorem} $x$ \\end{theorem}"), 1, "a theorem is text, its math is math");
  assert.ok(reply("`$x$`").includes("<code>$x$</code>"));
  assert.ok(reply("    $x$ indented code").includes("<pre><code>$x$ indented code"), "in a code block it goes back as source");
  assert.ok(!MathText.split("a \uE000 b").text.includes("\uE000"), "a reply's own placeholder characters go");
});

check("math: KaTeX gets the project's macros and what it lacks, mapped", () => {
  const html = MathText.render("$\\norm{x}$ $\\eqref{eq:a}$\n\\begin{equation}\\label{eq:a} a \\end{equation}\n"
    + "\\begin{multline} a \\\\ b \\end{multline}\n\\begin{eqnarray} a &=& b \\end{eqnarray}",
  { markdown: (s) => MDJS.parse(s), katex, macros: { "\\norm": "\\left\\|#1\\right\\|" } });
  assert.ok(!html.includes("katex-error") && !html.includes("#cc0000"), html.slice(0, 600));
  assert.ok(html.includes("eq:a"), "\\eqref shows its key");
  assert.strictEqual(MathText.prepare("\\begin{eqnarray*} a \\end{eqnarray*}"), "\\begin{array}{rcl} a \\end{array}");
  // An unknown macro is marked, and the rest of the formula is still drawn.
  const unknown = MathText.render("$\\nosuch + x$", { markdown: (s) => s, katex });
  assert.ok(unknown.includes('class="katex"') && unknown.includes("#cc0000"), unknown.slice(0, 300));
});

// ---------------------------------------------------------------- findlogic.js
const FindLogic = require("../prism_local/static/findlogic.js");
const spans = (text, opts) => FindLogic.matches(text, opts).list.map((x) => text.slice(x.from, x.to));
check("find: match case, whole word and regular expressions", () => {
  const text = "Cat cat, concat; cat_s. Ünter ünterm";
  assert.deepStrictEqual(spans(text, { query: "cat" }), ["Cat", "cat", "cat", "cat"]);
  assert.deepStrictEqual(spans(text, { query: "Cat", caseSensitive: true }), ["Cat"]);
  assert.deepStrictEqual(spans(text, { query: "cat", wholeWord: true }), ["Cat", "cat"]);
  assert.deepStrictEqual(spans(text, { query: "ünter", wholeWord: true }), ["Ünter"], "a letter of any script is a word's");
  assert.deepStrictEqual(spans(text, { query: "c[a-z]+t", regex: true }), ["Cat", "cat", "concat", "cat"]);
  assert.deepStrictEqual(spans("aab ab", { query: "ab", wholeWord: true }), ["ab"]);
});
check("find: typed text is literal; LaTeX's escapes are not errors", () => {
  assert.deepStrictEqual(spans("a $x^2$ and \\frac{1}{2}", { query: "$x^2$" }), ["$x^2$"]);
  assert.deepStrictEqual(spans("\\frac{1}{2}", { query: "\\frac{" }), ["\\frac{"]);
  assert.deepStrictEqual(spans("50\\% off", { query: "\\%", regex: true }), ["%"]);
  assert.deepStrictEqual(spans("a\\{b", { query: "\\\\\\{", regex: true }), ["\\{"]);
});
check("find: an invalid expression is said, not thrown; empty matches are left out", () => {
  const r = FindLogic.matches("abc", { query: "(a", regex: true });
  assert.deepStrictEqual([r.list.length, r.error], [0, "Invalid pattern"]);
  assert.deepStrictEqual(FindLogic.matches("abc", { query: "" }).list, []);
  assert.deepStrictEqual(spans("ab\nab", { query: "^|x*", regex: true }), []);
  assert.deepStrictEqual(spans("ab\nab", { query: "^a", regex: true }), ["a", "a"], "^ is each line's start");
  const capped = FindLogic.matches("aaaa", { query: "a" }, 3);
  assert.deepStrictEqual([capped.list.length, capped.more], [3, true]);
});
check("replace: $1, $&, $$ in a regular expression; as typed otherwise", () => {
  const opts = { query: "(\\w+)@(\\w+)", regex: true };
  const text = "x a@b y c@d";
  const { list } = FindLogic.matches(text, opts);
  assert.strictEqual(FindLogic.replaceAll(text, list, "$2 at $1 [$&] $$ $3 $0", opts), "x b at a [a@b] $ $3 $0 y d at c [c@d] $ $3 $0");
  assert.strictEqual(FindLogic.replaceAll(text, list, "$10", opts), "x a0 y c0", "$10 with one digit's group, as String.replace");
  const lit = { query: "a@b" };
  assert.strictEqual(FindLogic.replaceAll(text, FindLogic.matches(text, lit).list, "$1$&", lit), "x $1$& y c@d");
  // the same as String.prototype.replace
  const re = /(\w+)@(\w+)/g;
  assert.strictEqual(FindLogic.replaceAll(text, list, "<$2|$1|$&>", opts), text.replace(re, "<$2|$1|$&>"));
});
check("find: lines and columns of the matches, and a long line's excerpt", () => {
  const text = "one\r\ntwo cat\n\ncat";
  const where = FindLogic.lines(text, FindLogic.matches(text, { query: "cat" }).list);
  assert.deepStrictEqual(where, [{ line: 1, ch: 4, len: 3, text: "two cat" }, { line: 3, ch: 0, len: 3, text: "cat" }]);
  const long = "x".repeat(100) + "HIT" + "y".repeat(100);
  const e = FindLogic.excerpt(long, 100, 3);
  assert.strictEqual(e.hit, "HIT");
  assert.ok(e.before.startsWith("…") && e.after.endsWith("…") && e.before.length + e.after.length < 90, JSON.stringify(e));
  assert.deepStrictEqual(FindLogic.excerpt("  short cat", 8, 3), { before: "short ", hit: "cat", after: "" });
});
check("regions: formulas of every kind, comments, and what only looks like them", () => {
  const text = "Let $x_i$ be \\(y_i\\), cost \\$5 and \\$6; 50\\% i.\n"
    + "$$a_i$$ \\[b_i\\] % c_i here\n"
    + "\\begin{align*}\n d_i &= 1 % e_i\n\\end{align*}\n"
    + "\\begin{theorem} f_i \\end{theorem} line\\\\[2pt] g_i $ lone\n\nnot $ math";
  const r = FindLogic.regions(text);
  const spansOf = (list) => list.map(([a, b]) => text.slice(a, b));
  assert.deepStrictEqual(spansOf(r.math), ["$x_i$", "\\(y_i\\)", "$$a_i$$", "\\[b_i\\]",
    "\\begin{align*}\n d_i &= 1 % e_i\n\\end{align*}"]);
  assert.deepStrictEqual(spansOf(r.comments), ["% c_i here", "% e_i"]);
  const hits = (where) => FindLogic.scoped(FindLogic.matches(text, { query: "[a-gxy]_i", regex: true }).list, r, where)
    .map((x) => text.slice(x.from, x.to));
  assert.deepStrictEqual(hits("math"), ["x_i", "y_i", "a_i", "b_i", "d_i"], "not in a comment inside a display");
  assert.deepStrictEqual(hits("text"), ["f_i", "g_i"]);
  assert.strictEqual(hits("anywhere").length, 9);
  // a match that runs out of its formula is not "in mathematics"
  assert.deepStrictEqual(FindLogic.scoped(FindLogic.matches("$a$ b", { query: "a\\$ b", regex: true }).list,
    FindLogic.regions("$a$ b"), "math"), []);
});
// ---------------------------------------------------------------- agentsearch.js
const AgentSearch = require("../prism_local/static/agentsearch.js");
const block = (o) => "Here is my proposal.\n```prism-search\n" + JSON.stringify(o) + "\n```\n";
check("agent search: the prompt names the request, the files and the answer's form", () => {
  const p = AgentSearch.prompt("rename the index i to j", { main: "main.tex", documentFiles: ["main.tex", "sec/a.tex"] });
  assert.ok(p.startsWith("[prism-search]") && p.includes("Request: rename the index i to j") && p.includes("main.tex, sec/a.tex"));
  assert.ok(p.includes("```prism-search\n{") && p.includes('"\\\\\\\\sum"') && p.includes("Do not change, create or delete any file"), p);
});
check("agent search: the answer's last block is read; what cannot be used is said", () => {
  const ok = AgentSearch.parse("```json\n{\"rules\": []}\n```\n" + block({ summary: "S", rules: [
    { find: "_i\\b", replace: "_j", where: "math", note: "n" },
    { find: "(a", replace: "b" },
    { find: "x", replace: "y", where: "nowhere", caseSensitive: false },
    { find: "z" }], edits: [{ file: "./a.tex", line: 2, old: "q", new: "r" }, { file: "a.tex", line: 0, old: "q", new: "r" }] }));
  assert.strictEqual(ok.summary, "S");
  assert.deepStrictEqual(ok.rules, [{ find: "_i\\b", replace: "_j", where: "math", caseSensitive: true, note: "n" },
    { find: "x", replace: "y", where: "anywhere", caseSensitive: false, note: "" }]);
  assert.deepStrictEqual(ok.edits, [{ file: "a.tex", line: 2, old: "q", new: "r", note: "" }]);
  assert.strictEqual(ok.problems.length, 4, ok.problems.join(" | "));
  assert.ok(ok.problems[0].startsWith("Rule 2 is not a valid regular expression"), ok.problems[0]);
  assert.ok(AgentSearch.parse("no block at all").error.includes("no prism-search block"));
  assert.ok(AgentSearch.parse("```prism-search\n{rules: []}\n```").error.startsWith("The agent's proposal is not valid JSON"));
  assert.deepStrictEqual(AgentSearch.parse('{"rules": [], "edits": []}').rules, [], "bare JSON is read too");
});
check("agent search: rules where they may be, edits where they say, one change per place", () => {
  const files = [["main.tex", "Let $x_i$ and \\(a_{i}\\) for i in I.\n% x_i\n$\\sum_{i=1}^n y_i$ in $\\int$\n"],
    ["refs.bib", "x_i\n"], ["sec/b.tex", "nothing here\n"]];
  const p = AgentSearch.parse(block({ rules: [{ find: "_\\{?i\\b", replace: "_j", where: "math" },
    { find: "(_\\{)i(\\})", replace: "$1j$2", where: "math" }],
    edits: [{ file: "main.tex", line: 3, old: "\\sum_{i=1}", new: "\\sum_{j=1}" },
      { file: "main.tex", line: 1, old: "i", new: "j" }, { file: "main.tex", line: 9, old: "a", new: "b" },
      { file: "gone.tex", line: 1, old: "a", new: "b" }, { file: "main.tex", line: 1, old: "zzz", new: "b" }] }));
  const r = AgentSearch.changes(files, p);
  assert.deepStrictEqual(r.files.map((f) => f.path), ["main.tex"], "math only: not the .bib, not a comment");
  const c = r.files[0].changes;
  assert.deepStrictEqual(c.map((x) => [x.by, x.line, files[0][1].slice(x.from, x.to), x.text]),
    [["rule", 0, "_i", "_j"], ["rule", 0, "_{i", "_j"], ["edit", 2, "\\sum_{i=1}", "\\sum_{j=1}"], ["rule", 2, "_i", "_j"]]);
  assert.deepStrictEqual(r.counts, { rules: [3, 0], edits: 1 }, "rule 2's matches overlap rule 1's: the first is kept");
  assert.strictEqual(c[2].after, "$\\sum_{j=1}^n y_i$ in $\\int$");
  assert.strictEqual(r.problems.length, 4, r.problems.join(" | "));
  assert.ok(r.problems.some((x) => x.includes("more than once on line 1")) && r.problems.some((x) => x.includes("no line 9"))
    && r.problems.some((x) => x.includes("gone.tex is not")) && r.problems.some((x) => x.includes("does not have “zzz”")));
  assert.strictEqual(AgentSearch.apply(files[0][1], c.filter((x) => x.by === "edit")), files[0][1].replace("\\sum_{i=1}", "\\sum_{j=1}"));
});
check("go to line: a line, or a line and a column, from 1", () => {
  assert.deepStrictEqual(FindLogic.lineSpec("120"), { line: 119, ch: 0 });
  assert.deepStrictEqual(FindLogic.lineSpec(" 12:5 "), { line: 11, ch: 4 });
  assert.deepStrictEqual(FindLogic.lineSpec("12,5"), { line: 11, ch: 4 });
  assert.deepStrictEqual(FindLogic.lineSpec("0"), { line: 0, ch: 0 });
  for (const bad of ["", "x", "12:", "-3", "1.5"]) assert.strictEqual(FindLogic.lineSpec(bad), null, bad);
});

Promise.all(pending).then(() => {
  if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
  }
  console.log("ok");
});
