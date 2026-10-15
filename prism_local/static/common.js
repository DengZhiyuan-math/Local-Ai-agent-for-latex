/* Shared by the editor (app.js) and the pop-out PDF viewer (viewer.js). */
"use strict";

const $ = (s) => document.querySelector(s);
const store = {
  get(k, d) { try { const v = localStorage.getItem("prism." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("prism." + k, JSON.stringify(v)); } catch { /* ignore */ } },
};

let projectKey = null;             // fixed for this page's lifetime; never follow a reused port
// Never throws: when the server cannot be reached, the answer is {_status: 0, error}, so
// every caller handles it like any other failed request (and re-enables its buttons).
async function api(path, body) {
  if (projectKey && (path.startsWith("/api/agent") || path.startsWith("/api/files/"))) {
    if (body !== undefined) body = { ...body, project_key: projectKey };
    else if (path.startsWith("/api/agent/events")) path += (path.includes("?") ? "&" : "?") + "project_key=" + encodeURIComponent(projectKey);
  }
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Prism-Local": "1" },
    body: JSON.stringify(body),
  };
  let r;
  try { r = await fetch(path, opts); } catch { return { _status: 0, error: "server not reachable" }; }
  let data = {};
  try { data = await r.json(); } catch { /* non-JSON */ }
  if (!data || typeof data !== "object") data = {};
  data._status = r.status;
  return data;
}
// util.js (loaded first): plain helpers both pages use, checked under Node.
const { ago, plural, diffHtml } = Util;
const esc = Util.escapeHtml;

/* Polling. nap() waits `ms`, or `hiddenMs` while the page is hidden (null: until it is
   shown again), and stops waiting the moment a hidden page is shown. */
function nap(ms, hiddenMs = null) {
  return new Promise((res) => {
    const hidden = document.visibilityState === "hidden", wait = hidden ? hiddenMs : ms;
    const timer = wait === null ? null : setTimeout(done, wait);
    function wake() { if (document.visibilityState === "visible") done(); }
    function done() { clearTimeout(timer); document.removeEventListener("visibilitychange", wake); res(); }
    if (hidden) document.addEventListener("visibilitychange", wake);
  });
}
// A poll that never runs twice at once. Calling it during a run queues one more run after
// it (however many calls come in meanwhile), so a caller still sees what changed before it
// called. start() runs it every `ms` after the last run ended (see nap), while when() holds.
function poller(fn, ms, hiddenMs = null) {
  let running = null, again = null, started = false;
  function run() {
    if (running) return again || (again = running.then(() => { again = null; return run(); }));
    running = Promise.resolve().then(fn).catch((e) => console.error(e)).finally(() => { running = null; });
    return running;
  }
  run.start = async (when = () => true) => {
    if (started) return;
    started = true;
    for (;;) { await nap(ms, hiddenMs); if (when()) await (running || run()); }
  };
  return run;
}

/* theme */
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  const dark = t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  if (dark) document.documentElement.dataset.dark = ""; else delete document.documentElement.dataset.dark;
}
applyTheme(store.get("theme", null));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(store.get("theme", null)));
// The theme button (editor, Home): the system's, then dark, then light.
if (document.querySelector("#btn-theme")) document.querySelector("#btn-theme").onclick = () => {
  const cur = store.get("theme", null);
  const next = cur === null ? "dark" : cur === "dark" ? "light" : null;
  store.set("theme", next); applyTheme(next);
};

/* Icons: one line-icon set drawn on a 24px grid, so every icon has the same size, stroke
   and centre (text glyphs such as ☰ ⌂ ◐ differ in size and sit at different heights).
   <button data-icon="home"> puts the icon before the label, data-icon-end after it. */
const ICONS = {
  menu: '<path d="M4 6.5h16M4 12h16M4 17.5h16"/>',
  home: '<path d="M4 10.5 12 4l8 6.5"/><path d="M6 9v10.5h4.5V14h3v5.5H18V9"/>',
  theme: '<circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor" stroke="none"/>',
  settings: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  minus: '<path d="M6 12h12"/>',
  plus: '<path d="M12 6v12M6 12h12"/>',
  more: '<circle cx="6" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="18" cy="12" r="1.4" fill="currentColor" stroke="none"/>',
  down: '<path d="m7 10 5 5 5-5"/>',
  up: '<path d="m7 14 5-5 5 5"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z" fill="currentColor" stroke-width="1.2"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5" fill="currentColor" stroke="none"/>',
  popout: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3L19.5 9"/><path d="M19.5 4.5V9H15"/>',
  arrow: '<path d="M5 12h14M13.5 6.5 19 12l-5.5 5.5"/>',
  list: '<path d="M9 6.5h11M9 12h11M9 17.5h11"/><circle cx="4.8" cy="6.5" r="1.1" fill="currentColor" stroke="none"/><circle cx="4.8" cy="12" r="1.1" fill="currentColor" stroke="none"/><circle cx="4.8" cy="17.5" r="1.1" fill="currentColor" stroke="none"/>',
  cloud: '<path d="M7 18.5h10a4 4 0 0 0 .6-7.95A5.5 5.5 0 0 0 7 9.5a4.5 4.5 0 0 0 0 9z"/>',
  search: '<circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5 5"/>',
  download: '<path d="M12 4v11M7 10.5l5 5 5-5"/><path d="M5 19.5h14"/>',
  spark: '<path d="M12 4l1.7 5.3L19 11l-5.3 1.7L12 18l-1.7-5.3L5 11l5.3-1.7z" fill="currentColor" stroke-width="1"/>',
  star: '<path d="m12 4.5 2.3 4.7 5.2.8-3.8 3.6.9 5.1L12 16.3l-4.6 2.4.9-5.1-3.8-3.6 5.2-.8z"/>',
  right: '<path d="m10 7 5 5-5 5"/>',
  folder: '<path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
  tag: '<path d="M4.5 4.5h7l8 8-7 7-8-8z"/><circle cx="8.5" cy="8.5" r="1.3" fill="currentColor" stroke="none"/>',
  doc: '<path d="M6.5 3.5h7.5l4 4v13h-11.5z"/><path d="M14 3.5v4h4M9.5 12.5h5M9.5 16h5"/>',
  grid: '<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"/>',
  panel: '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M14.5 5v14"/>',
  arrowup: '<path d="M12 19V5M6.5 10.5 12 5l5.5 5.5"/>',
  arrowdown: '<path d="M12 5v14M6.5 13.5 12 19l5.5-5.5"/>',
  edit: '<path d="M5 19h3.5L19 8.5 15.5 5 5 15.5z"/><path d="m13.5 7 3.5 3.5"/>',
  move: '<path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/><path d="M9 13.5h6M12.5 11l2.5 2.5-2.5 2.5"/>',
  bolt: '<path d="M13.5 3.5 6 13.5h5.5l-1 7 7.5-10h-5.5z"/>',
  trash: '<path d="M5 7h14M10 7V5h4v2M7 7l1 12.5h8L17 7"/>',
  inbox: '<path d="M4 13.5 6.5 5.5h11l2.5 8v5H4z"/><path d="M4 13.5h5l1 2h4l1-2h5"/>',
};
function icon(name, cls = "") {
  return `<svg class="ico ${cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICONS[name] || ""}</svg>`;
}
for (const el of document.querySelectorAll("[data-icon]")) el.insertAdjacentHTML("afterbegin", icon(el.dataset.icon));
for (const el of document.querySelectorAll("[data-icon-end]")) el.insertAdjacentHTML("beforeend", icon(el.dataset.iconEnd, "end"));

/* Key hints are written for macOS (⌘); on Windows and Linux the same keys use Ctrl. */
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const keys = (s) => (IS_MAC ? s : String(s).replace(/⌘/g, "Ctrl+"));
if (!IS_MAC) for (const el of document.querySelectorAll("[title*='⌘'], [placeholder*='⌘']")) {
  if (el.title) el.title = keys(el.title);
  if (el.placeholder) el.placeholder = keys(el.placeholder);
}

// Editor tab <-> pop-out PDF tab. Messages:
//   viewer -> editor: {type:"alive"} (heartbeat), {type:"bye"}, {type:"inverse", id, page, x, y}
//   editor -> viewer: {type:"forward", r} (SyncTeX box), {type:"pdf", mtime},
//                     {type:"jumped", id, name, ok, file, line | msg} (answers "inverse")
const pdfChannel = "BroadcastChannel" in window ? new BroadcastChannel("prism-pdf") : null;

/* Page presence. Every page (editor, pop-out PDF) sends a heartbeat so a server started
   with --exit-when-idle knows it is in use, and says goodbye when it closes. The goodbye
   goes through sendBeacon, which survives page unload but cannot set X-Prism-Local; the
   server accepts it only for a page id that has sent a heartbeat. */
(function presence() {
  const id = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
    : Date.now().toString(36) + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
  let fails = 0, banner = null;

  function showGone(on) {
    if (on && !banner) {
      banner = document.createElement("div");
      banner.id = "server-gone";
      banner.setAttribute("role", "alert");
      banner.style.cssText = "position:fixed;inset:0;z-index:9999;display:flex;align-items:center;" +
        "justify-content:center;background:rgba(0,0,0,.45)";
      banner.innerHTML = '<div style="background:var(--panel,#fff);color:var(--text,#111);' +
        'border:1px solid var(--border,#ccc);border-radius:8px;padding:18px 22px;max-width:420px;' +
        'font:14px/1.5 var(--sans,system-ui)"><b>prism-local is not running.</b><br>' +
        "The server stopped, probably because every Prism page was closed. " +
        "Start it again from its shortcut; this page reconnects by itself.</div>";
      document.body.appendChild(banner);
    } else if (!on && banner) {
      banner.remove(); banner = null;
    }
  }

  async function beat() {
    try {
      const r = await api("/api/presence", { client: id });
      fails = r._status === 200 ? 0 : fails + 1;
    } catch { fails += 1; }
    showGone(fails >= 3);
  }

  // The open stream is what keeps the server alive: when this page, its tab or the
  // whole browser closes, the connection drops and the server notices at once, even
  // if the goodbye below never gets out. EventSource reconnects by itself.
  let stream = null;
  function hold() {
    if (!("EventSource" in window) || stream) return;
    stream = new EventSource("/api/presence/stream?client=" + encodeURIComponent(id));
  }

  window.addEventListener("pagehide", () => {
    if (stream) { stream.close(); stream = null; }
    navigator.sendBeacon("/api/bye", id);
  });
  window.addEventListener("pageshow", (e) => { if (e.persisted) { hold(); beat(); } });   // back/forward cache
  hold();
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") beat(); });
  beat();
  setInterval(beat, 5000);
})();

/* A context menu, for Home and the editor: items are [[label, fn, cls?], "-", …], shown
   below an anchor element or at a point {x, y}. */
let menuItems = [];
const MENU = $("#menu") || document.body.appendChild(Object.assign(document.createElement("div"),
  { id: "menu", className: "menu", hidden: true }));
MENU.setAttribute("role", "menu");
function showMenu(anchor, items, at = null) {
  const m = MENU;
  menuItems = items;
  m.innerHTML = items.map((it, i) => it === "-" ? "<hr>" : `<button data-m="${i}" class="${it[2] || ""}" role="menuitem">${esc(it[0])}</button>`).join("");
  m.hidden = false;
  const mw = m.offsetWidth, mh = m.offsetHeight;
  const r = anchor ? anchor.getBoundingClientRect() : { left: at.x, right: at.x + mw, top: at.y, bottom: at.y };
  m.style.left = Math.max(8, Math.min(anchor ? r.right - mw : r.left, innerWidth - mw - 8)) + "px";
  m.style.top = Math.max(8, r.bottom + mh + 6 > innerHeight ? r.top - mh - 4 : r.bottom + 4) + "px";
  m.querySelector("button").focus();
}
function closeMenu() { MENU.hidden = true; }
MENU.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-m]"); if (!b) return;
  closeMenu(); menuItems[+b.dataset.m][1]();
});
MENU.addEventListener("keydown", (e) => {
  const bs = [...MENU.querySelectorAll("button")], i = bs.indexOf(document.activeElement);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault(); bs[(i + (e.key === "ArrowDown" ? 1 : bs.length - 1)) % bs.length].focus();
  }
});
document.addEventListener("mousedown", (e) => { if (!MENU.hidden && !e.target.closest("#menu")) closeMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
window.addEventListener("blur", closeMenu);
window.addEventListener("resize", closeMenu);
// Which file manager "Show in …" opens.
const FILE_MANAGER = IS_MAC ? "Finder" : /Windows/.test(navigator.userAgent) ? "File Explorer" : "file manager";
const TRASH = IS_MAC ? "Trash" : "Recycle Bin";
