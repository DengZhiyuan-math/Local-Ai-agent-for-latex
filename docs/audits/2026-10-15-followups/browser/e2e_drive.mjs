// Drives the editor's Files menu in headless Chrome over the DevTools protocol.
// usage: node e2e_drive.mjs <editor url>
const url = process.argv[2];
const tab = await (await fetch("http://127.0.0.1:9333/json/new?" + encodeURIComponent(url), { method: "PUT" })).json();
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let id = 0;
const pending = new Map();
const errors = [];
ws.addEventListener("message", (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
  if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  if (d.method === "Runtime.consoleAPICalled" && d.params.type === "error") errors.push(d.params.args.map((a) => a.value ?? a.description).join(" "));
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || JSON.stringify(r.result.exceptionDetails));
  return r.result.result.value;
};
await send("Runtime.enable");
const results = [];
const check = (name, ok, detail = "") => { results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`); };

// Helpers in the page: right-click an item, pick a menu entry, wait for a condition.
await ev(`
  for (let i = 0; i < 100 && !(typeof projectKey !== "undefined" && projectKey && S.files.length); i++) await new Promise((r) => setTimeout(r, 100));
  window.confirm = () => true;
  window.T = {
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    async until(fn, ms = 5000) { for (let t = 0; t < ms; t += 100) { if (fn()) return true; await T.sleep(100); } return false; },
    menu(sel) {
      const el = sel ? document.querySelector(sel) : document.querySelector("#tree").closest("section").querySelector("h3");
      const r = el.getBoundingClientRect();
      el.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: r.left + 5, clientY: r.top + 5 }));
      return [...document.querySelectorAll("#menu button")].map((b) => b.textContent);
    },
    pick(label) { const b = [...document.querySelectorAll("#menu button")].find((x) => x.textContent === label); b.click(); },
    async type(name) { const i = document.querySelector("#tree input"); i.value = name; i.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); },
    files: () => S.files.map((f) => f.path),
  };
  return true;`);

const ready = await ev(`return { key: typeof projectKey !== "undefined" && !!projectKey, files: T.files(), folders: S.folders }`);
check("page ready with folders listed", ready.key && ready.folders.includes("figs"), JSON.stringify(ready));
check("empty folder shown in tree", await ev(`return !!document.querySelector('#tree li[data-dir="figs"]')`));

const fileMenu = await ev(`return T.menu('#tree li[data-path="main.tex"]')`);
check("file menu", JSON.stringify(fileMenu) === JSON.stringify(["Open", "Show in File Explorer", "Copy", "Paste", "Rename…", "Delete (to Recycle Bin)"]), JSON.stringify(fileMenu));
await ev(`T.pick("Copy"); await T.sleep(800); return true`);
check("copy toast", (await ev(`return $("#build-status").textContent`)).startsWith("Copied"), await ev(`return $("#build-status").textContent`));

const folderMenu = await ev(`return T.menu('#tree li[data-dir="figs"]')`);
check("folder menu", folderMenu[0] === "New file…" && folderMenu.includes("Paste"), JSON.stringify(folderMenu));
await ev(`T.pick("Paste"); return true`);
check("paste into empty folder", await ev(`return T.until(() => T.files().includes("figs/main.tex"))`), JSON.stringify(await ev(`return T.files()`)));
await ev(`T.menu('#tree li[data-dir="figs"]'); T.pick("Paste"); return true`);
check("second paste gets -copy name", await ev(`return T.until(() => T.files().includes("figs/main-copy.tex"))`));

const bgMenu = await ev(`return T.menu(null)`);
check("background menu", bgMenu.includes("Open project folder in File Explorer"), JSON.stringify(bgMenu));
await ev(`T.pick("New file…"); await T.sleep(100); return true`);
check("new-file box shown", await ev(`return !!document.querySelector("#tree input")`));
await ev(`await T.type("intro.tex"); return true`);
check("new file created and opened", await ev(`return T.until(() => T.files().includes("intro.tex") && S.active === "intro.tex")`), await ev(`return S.active`));

await ev(`T.menu('#tree li[data-path="sec/a.tex"]'); T.pick("Open"); await T.until(() => S.active === "sec/a.tex"); return true`);
await ev(`T.menu('#tree li[data-dir="sec"]'); T.pick("Rename…"); await T.sleep(100); await T.type("chapters"); return true`);
check("folder renamed, open tab follows", await ev(`return T.until(() => T.files().includes("chapters/a.tex") && S.active === "chapters/a.tex" && S.tabs.some((t) => t.path === "chapters/a.tex"))`),
  JSON.stringify(await ev(`return { files: T.files(), active: S.active, tabs: S.tabs.map((t) => t.path) }`)));
await ev(`cm.setValue("edited after rename\\n"); await T.sleep(2500); return true`);
const disk = await ev(`return (await api("/api/file?path=chapters/a.tex")).content`);
check("autosave goes to the new path", disk === "edited after rename\n", JSON.stringify(disk));

await ev(`T.menu('#tree li[data-path="intro.tex"]'); T.pick("Delete (to Recycle Bin)"); return true`);
check("deleted file leaves tree and tabs", await ev(`return T.until(() => !T.files().includes("intro.tex") && !S.tabs.some((t) => t.path === "intro.tex"))`));

await ev(`T.menu('#tree li[data-path="main.tex"]'); T.pick("Rename…"); await T.sleep(100); await T.type("bad:name.tex"); await T.sleep(600); return true`);
check("bad name refused with message", (await ev(`return $("#build-status").textContent`)).includes("cannot contain"), await ev(`return $("#build-status").textContent`));
check("tree intact after refusal", await ev(`return T.until(() => !!document.querySelector('#tree li[data-path="main.tex"]'))`));

check("no page errors", errors.length === 0, errors.join(" | "));
console.log(results.join("\n"));
ws.close();
process.exit(0);
