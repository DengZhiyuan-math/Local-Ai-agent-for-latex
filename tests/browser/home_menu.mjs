import { closeSocket } from "./close.mjs";
// Drives Home's project menu in headless Chrome. usage: node e2e_home.mjs <home url> <proj> <proj2>
const [url, proj, proj2] = process.argv.slice(2);
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
const ev = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || JSON.stringify(r.result.exceptionDetails));
  return r.result.result.value;
};
await send("Runtime.enable");
const results = [];
const check = (name, ok, detail = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ": " + detail : ""}`);

await ev(`
  for (let i = 0; i < 300 && !(typeof H !== "undefined" && H.loaded); i++) await new Promise((r) => setTimeout(r, 100));   // scripts and the list
  window.confirm = () => true;
  window.T = {
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    async until(fn, ms = 6000) { for (let t = 0; t < ms; t += 100) { if (fn()) return true; await T.sleep(100); } return false; },
    async menu(sel) {
      const el = document.querySelector(sel); const r = el.getBoundingClientRect();
      el.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: r.left + 5, clientY: r.top + 5 }));
      await T.until(() => !$("#menu").hidden);
      return [...document.querySelectorAll("#menu button")].map((b) => b.textContent);
    },
    pick(label) { [...document.querySelectorAll("#menu button")].find((x) => x.textContent === label).click(); },
    toast: () => $("#toast").textContent,
  };
  return true;`);
const ids = await ev(`
  const a = await api("/api/projects/add", { path: ${JSON.stringify(proj)} });
  const b = await api("/api/projects/add", { path: ${JSON.stringify(proj2)} });
  await load(); return [a.id, b.id];`);
await ev(`return T.until(() => document.querySelector('#view .it[data-id="${ids[1]}"]'))`);

const pm = await ev(`return T.menu('#view .it[data-id="${ids[1]}"]')`);
check("project menu", pm.includes("Open folder in File Explorer") && pm.includes("Copy folder") && pm.includes("Move folder to Recycle Bin…"), JSON.stringify(pm));
await ev(`T.pick("Copy folder"); await T.sleep(800); return true`);
check("copy folder", (await ev(`return T.toast()`)).startsWith("Copied the folder"), await ev(`return T.toast()`));

const bm = await ev(`return T.menu('#view-wrap')`);
check("background menu has paste", bm.includes("Paste project folders"), JSON.stringify(bm));
await ev(`await api("/api/projects/remove", { id: "${ids[1]}" }); await load(); return true`);
await ev(`T.menu('#view-wrap'); T.pick("Paste project folders"); return true`);
check("paste adds the copied folder back as a project", await ev(`return T.until(() => H.projects.some((p) => p.id === "${ids[1]}"))`), await ev(`return T.toast()`));

await ev(`await T.menu('#view .it[data-id="${ids[0]}"]'); T.pick("Move folder to Recycle Bin…"); await T.sleep(1500); return true`);
check("refused while its editor runs", (await ev(`return T.toast()`)).includes("editor is open"), await ev(`return T.toast()`));
check("still listed", await ev(`return H.projects.some((p) => p.id === "${ids[0]}")`));

await ev(`await T.menu('#view .it[data-id="${ids[1]}"]'); T.pick("Move folder to Recycle Bin…"); return true`);
check("project without editor goes to Recycle Bin", await ev(`return T.until(() => !H.projects.some((p) => p.id === "${ids[1]}"))`), await ev(`return T.toast()`));
const damaged = await ev(`
  const real = api, ask = window.confirm; let restored = null;
  api = async (path, body) => path === "/api/projects"
    ? { _status: 409, error: "projects.json is damaged; nothing was changed.", damaged_state: "projects.json", backup: true }
    : path === "/api/state/restore" ? (restored = body.file, { _status: 200, ok: true }) : real(path, body);
  window.confirm = () => true;
  await loadOnce();
  const bar = document.querySelector("#state-damaged"), said = !!bar && bar.textContent.includes("damaged");
  bar?.querySelector("button")?.click(); await T.sleep(50);
  api = real; window.confirm = ask; await load();
  return { said, restored, gone: !document.querySelector("#state-damaged") };`);
check("a damaged project list says so and can be put back", damaged.said && damaged.restored === "projects.json" && damaged.gone, JSON.stringify(damaged));
const pin = await ev(`
  const real = api, id = "${ids[0]}", before = !!byId(id).pinned;
  api = async (path, body) => path === "/api/projects/update"
    ? { _status: 409, error: "fixture: another window is writing the list" } : real(path, body);
  await setPinned(id, !before);
  api = real;
  const back = await T.until(() => !!byId(id).pinned === before);
  return { back, toast: T.toast() };`);
check("a pin that could not be saved says so and goes back", pin.back && pin.toast.includes("another window"), JSON.stringify(pin));
const setup = await ev(`
  // Getting started, from nothing: git and the GitHub CLI installed, the login's code shown.
  const real = api, seen = {}, st = { git: null, gh: null, can_install: true, installing: {}, how: { git: "by hand", gh: "by hand" },
                                      github: { logged_in: false }, login: { state: null } };
  api = async (path, body) => {
    if (path === "/api/tools") return { _status: 200, ...JSON.parse(JSON.stringify(st)) };
    if (path === "/api/tools/install") { st.installing[body.name] = { state: "running" }; return { _status: 200, state: "running" }; }
    if (path === "/api/github/login") { st.login = { state: "waiting", code: "AB12-CD34", url: "https://github.com/login/device" }; return { _status: 200 }; }
    return real(path, body);
  };
  const panel = () => document.querySelector("#setup");
  await loadSetup();
  seen.shown = !!panel() && panel().querySelectorAll("li").length === 3 && panel().querySelectorAll('[data-setup^="install-"]').length === 2;
  panel().querySelector('[data-setup="install-git"]').click();
  seen.installing = await T.until(() => (panel()?.textContent || "").includes("Installing"));
  st.installing.git = { state: "done" }; st.git = "C:/git.exe"; await loadSetup();
  seen.gitDone = panel().querySelector("li").classList.contains("done");
  st.installing.gh = { state: "failed", error: "The GitHub CLI could not be installed. Try again, or: by hand", details: "exit 1602" };
  await loadSetup();
  seen.failure = panel().textContent.includes("could not be installed") && !!panel().querySelector("details");
  st.installing.gh = { state: "done" }; st.gh = "C:/gh.exe"; await loadSetup();
  panel().querySelector('[data-setup="login"]').click();
  seen.code = await T.until(() => panel()?.querySelector(".setup-code")?.textContent === "AB12-CD34");
  seen.link = panel().querySelector('a[href="https://github.com/login/device"]') !== null;
  st.login = { state: "done" }; st.github = { logged_in: true, account: "me" }; await loadSetup();
  seen.gone = !panel();
  clearTimeout(SETUP.timer); api = real;
  return seen;`);
check("getting started installs git and the GitHub CLI and shows the login's code", Object.values(setup).every(Boolean), JSON.stringify(setup));
check("no page errors", errors.length === 0, errors.join(" | "));
console.log(results.join("\n"));
await closeSocket(ws);
await fetch("http://127.0.0.1:9333/json/close/" + tab.id);   // its page goes too: it holds connections
process.exitCode = results.some(line => line.startsWith("FAIL")) ? 1 : 0;
