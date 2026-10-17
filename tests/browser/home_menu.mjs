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
    if (path.split("?")[0] === "/api/tools") { (seen.asked ||= []).push(path); return { _status: 200, ...JSON.parse(JSON.stringify(st)) }; }
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
  seen.pollsDoNotRefresh = seen.asked.length > 3 && seen.asked.every((p) => p === "/api/tools"); delete seen.asked;
  clearTimeout(SETUP.timer); api = real;
  return seen;`);
check("getting started installs git and the GitHub CLI and shows the login's code", Object.values(setup).every(Boolean), JSON.stringify(setup));
const recheck = await ev(`
  // Where Prism cannot install (no winget): Check again asks gh afresh, and stays usable while still missing;
  // so does a button whose request is refused.
  const real = api, seen = {}, asked = [], st = { git: null, gh: null, can_install: false, installing: {},
    how: { git: "Install git from git-scm.com.", gh: "Install the GitHub CLI from cli.github.com." }, github: { logged_in: false }, login: { state: null } };
  api = async (path, body) => {
    if (path.split("?")[0] === "/api/tools") { asked.push(path); await T.sleep(50); return { _status: 200, ...JSON.parse(JSON.stringify(st)) }; }
    if (path === "/api/tools/install") return { _status: 409, error: "fixture: refused" };
    return real(path, body);
  };
  const panel = () => document.querySelector("#setup");
  await loadSetup();
  const b = panel().querySelector('[data-setup="recheck"]');
  b.click();
  seen.busyWhileAsking = b.disabled;
  seen.usableAgain = await T.until(() => !b.disabled && asked.length === 2);
  seen.samePanel = b.isConnected;
  seen.refreshed = asked[1] === "/api/tools?refresh=1";
  b.click();
  seen.secondClick = await T.until(() => !b.disabled && asked.length === 3) && asked[2] === "/api/tools?refresh=1";
  st.can_install = true; await loadSetup();
  const i = panel().querySelector('[data-setup="install-git"]');
  i.click();
  seen.refusedUsable = await T.until(() => !i.disabled && asked.length === 5) && i.isConnected && T.toast() === "fixture: refused";
  clearTimeout(SETUP.timer); api = real;
  return seen;`);
check("check again asks afresh and stays usable, as does a refused button", Object.values(recheck).every(Boolean), JSON.stringify(recheck));
const warned = await ev(`
  // Logged in, but git could not be set to use it: said once, when the login ends; and in Settings.
  const real = api, seen = {}, warning = "You are logged in to GitHub, but the first save to GitHub may ask you to log in again.";
  const st = { git: "C:/git.exe", gh: null, can_install: true, installing: {}, how: { git: "", gh: "" },
               github: { logged_in: false }, login: { state: "waiting", code: "AB12-CD34", url: "https://github.com/login/device" } };
  api = async (path, body) => {
    if (path.split("?")[0] === "/api/tools") return { _status: 200, ...JSON.parse(JSON.stringify(st)) };
    if (path.split("?")[0] === "/api/settings") return { _status: 200, settings: {}, github: JSON.parse(JSON.stringify(st.github)), claude: {}, update: null };
    if (path === "/api/tools/install") { st.installing[body.name] = { state: "running" }; return { _status: 200, state: "running" }; }
    return real(path, body);
  };
  toast("before");
  await loadSetup();
  seen.notYet = T.toast() === "before";
  st.login = { state: "done", warning, details: "setup-git: exit 1" }; st.github = { logged_in: true, account: "me" };
  await loadSetup();
  seen.toast = T.toast() === warning && $("#toast").className === "err";
  toast("after"); await loadSetup(); await loadSetup();
  seen.once = T.toast() === "after";
  SETUP.loggingIn = false; await loadSetup();          // a page opened after the login
  seen.notOnLoad = T.toast() === "after";
  // Settings: git installed, the GitHub CLI not (here), with the same Install button.
  st.login = { state: null }; st.github = { logged_in: false, error: "The GitHub CLI is not installed." };
  await openSettings();
  await T.until(() => $("#tools-status").textContent.includes("not installed"));
  const rows = [...$("#tools-status").children].map((d) => d.textContent.trim());
  seen.gitRow = rows[0] === "✓ git is installed";
  seen.ghRow = rows[1].startsWith("The GitHub CLI is not installed") && !!$('#tools-status [data-setup="install-gh"]');
  seen.noDoubleLine = $("#gh-status").hidden;
  $('#tools-status [data-setup="install-gh"]').click();
  seen.installing = await T.until(() => $("#tools-status").textContent.includes("Installing"));
  st.installing.gh = { state: "done" }; st.gh = "C:/gh.exe"; st.github = { logged_in: true, account: "me" };
  st.login = { state: "done", warning, details: null };
  seen.ghDone = await T.until(() => $("#tools-status").textContent.includes("✓ The GitHub CLI is installed"));
  seen.settingsWarning = !$("#gh-warning").hidden && $("#gh-warning").textContent === warning && !$("#gh-status").hidden;
  $("#dlg-settings").close();
  clearTimeout(SETUP.timer); api = real;
  return seen;`);
check("a login's warning is said once, and Settings shows git, the GitHub CLI and the warning", Object.values(warned).every(Boolean), JSON.stringify(warned));
const clone = await ev(`
  const real = api, seen = {}; let asked = null;
  api = async (path, body) => path === "/api/github/repos"
    ? { _status: 200, repos: [{ full: "me/thesis", name: "thesis", private: true, updated: "2026-10-01T10:00:00Z" },
                              { full: "me/notes", name: "notes", private: false, updated: "" }] }
    : path === "/api/github/open" ? (asked = body.full, { _status: 200, ids: ["x"], path: "C:/papers/thesis", cloned: true })
    : real(path, body);
  $("#btn-clone").click();
  seen.listed = await T.until(() => $("#clone-list").querySelectorAll("[data-full]").length === 2);
  const f = $("#form-clone"); f.elements.filter.value = "thes"; f.elements.filter.dispatchEvent(new Event("input"));
  seen.filtered = $("#clone-list").querySelectorAll("[data-full]").length === 1;
  $("#clone-list").querySelector('[data-full="me/thesis"]').click();
  seen.closed = await T.until(() => !$("#dlg-clone").open);
  seen.asked = asked === "me/thesis";
  seen.said = T.toast().includes("Copied me/thesis");
  api = real;
  return seen;`);
check("open from GitHub lists your repositories and copies the one chosen", Object.values(clone).every(Boolean), JSON.stringify(clone));
check("no page errors", errors.length === 0, errors.join(" | "));
console.log(results.join("\n"));
await closeSocket(ws);
await fetch("http://127.0.0.1:9333/json/close/" + tab.id);   // its page goes too: it holds connections
process.exitCode = results.some(line => line.startsWith("FAIL")) ? 1 : 0;
