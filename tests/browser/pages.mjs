// Every page loads without an error, and what util.js now provides works on it.
// usage: node e2e_pages.mjs <editor url> <home url>
const [editor, home] = process.argv.slice(2);
const results = [];
const check = (name, ok, detail = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);

async function open(url) {
  const tab = await (await fetch("http://127.0.0.1:9333/json/new?" + encodeURIComponent(url), { method: "PUT" })).json();
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  let id = 0;
  const pending = new Map(), errors = [];
  ws.addEventListener("message", (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
    if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  });
  const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  await send("Runtime.enable");
  await send("Page.enable");
  await send("Page.reload");                     // errors thrown while loading are seen
  await new Promise((r) => setTimeout(r, 2500));
  const ev = async (expr) => {
    const r = await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
    if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || "evaluate failed");
    return r.result.result.value;
  };
  // Its page goes too when done: an open page holds connections the next one needs.
  return { ev, errors, close: () => { ws.close(); return fetch("http://127.0.0.1:9333/json/close/" + tab.id); } };
}

const ed = await open(editor);
check("editor loads", ed.errors.length === 0, ed.errors.join(" | "));
check("theme button cycles", await ed.ev(`
  const b = document.querySelector("#btn-theme"), seen = [];
  for (let i = 0; i < 3; i++) { b.click(); seen.push(document.documentElement.dataset.theme || "system"); }
  return seen.join(",") === "dark,light,system";`), "");
check("diff panel colours lines", await ed.ev(`
  $("#diff").innerHTML = diffHtml("+a\\n-b");
  return document.querySelectorAll("#diff .add, #diff .del").length === 2;`));
check("sync menu drops a stale GitHub link", await ed.ev(`
  S.githubUrl = "https://github.com/owner/tool"; $("#sync-github").href = S.githubUrl;
  renderSync({ own: true, enabled: true, remote: true, state: "idle", target: null,
               bound_target: { repository_identity: "github.com/owner/paper", branch_ref: "refs/heads/main" },
               blocked_reason: "Fetch and push differ" });
  return S.githubUrl === "" && $("#sync-github").hidden && !$("#sync-github").hasAttribute("href")
    && $("#sync-label").textContent === "Sync paused" && $("#sync-detail").innerHTML.includes("github.com/owner/paper");`));
check("completion of a label", await ed.ev(`
  for (let i = 0; i < 50 && !S.tabs.length; i++) await new Promise((r) => setTimeout(r, 100));
  S.symbols = { ...S.symbols, labels: [{ label: "thm:main", kind: "theorem", file: "main.tex", line: 1 }], env_titles: { theorem: "Theorem" } };
  cm.setValue("see \\\\cref{thm"); cm.setCursor({ line: 0, ch: 13 });
  const h = computeHints(cm, false);
  return !!h && h.list.length === 1 && h.list[0].text === "thm:main" && h.from.ch === 10;`));
await ed.close();
const hm = await open(home);
check("home search lists the project", await hm.ev(`
  for (let i = 0; i < 300 && !(typeof H !== "undefined" && H.loaded); i++) await new Promise((r) => setTimeout(r, 100));
  const before = viewItems("").items.length;
  const found = viewItems("proj").items.map((i) => i.p && i.p.name);
  return before >= 1 && found.includes("proj");`));
check("home loads", hm.errors.length === 0, hm.errors.join(" | "));
check("home ago and plural", await hm.ev(`return ago(Date.now() / 1000 - 7200) === "2 h ago" && plural(2, "item") === "2 items"`));
await hm.close();
const vw = await open(editor.replace(/\/$/, "") + "/viewer");
check("pdf viewer loads", vw.errors.length === 0, vw.errors.join(" | "));
await vw.close();
console.log(results.join("\n"));
process.exit(0);
