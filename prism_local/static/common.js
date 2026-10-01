/* Shared by the editor (app.js) and the pop-out PDF viewer (viewer.js). */
"use strict";

const $ = (s) => document.querySelector(s);
const store = {
  get(k, d) { try { const v = localStorage.getItem("prism." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("prism." + k, JSON.stringify(v)); } catch { /* ignore */ } },
};

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Prism-Local": "1" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch { /* non-JSON */ }
  data._status = r.status;
  return data;
}
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* theme */
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  const dark = t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  if (dark) document.documentElement.dataset.dark = ""; else delete document.documentElement.dataset.dark;
}
applyTheme(store.get("theme", null));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(store.get("theme", null)));

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
