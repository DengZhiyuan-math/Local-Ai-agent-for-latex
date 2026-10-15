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

Promise.all(pending).then(() => {
  if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
  }
  console.log("ok");
});
