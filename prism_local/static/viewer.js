/* Pop-out PDF viewer: reloads on every build, SyncTeX both ways via pdfChannel. */
"use strict";

PV.init({ scaleKey: "scale.popout", standalone: true });   // ⌘F searches the PDF here

function note(msg, warn) {
  const n = $("#viewer-note");
  n.textContent = msg; n.className = warn ? "warn" : "";
}
const DEFAULT_NOTE = "Double-click (or Ctrl-click) to jump to the source in the editor tab";

// A double-click asks the editor tab to open the source. The editor answers ("jumped"),
// and this tab then brings it to the front; with no answer, there is no editor tab.
let jumpSeq = 0, jumpWait = null;
PV.onInverse = (p) => {
  if (!pdfChannel) return note("This browser cannot talk to the editor tab (no BroadcastChannel).", true);
  const id = ++jumpSeq;
  pdfChannel.postMessage({ type: "inverse", id, ...p });
  note("Finding the source…");
  clearTimeout(jumpWait);
  jumpWait = setTimeout(() => {
    if (id === jumpSeq) note("No editor tab found — open the editor tab to jump to sources.", true);
  }, 3000);
};
function jumped(m) {
  if (m.id !== jumpSeq) return;              // an answer to an older click, or another editor tab's
  clearTimeout(jumpWait); jumpSeq++;
  if (!m.ok) return note(m.msg || "No source location found here.", true);
  const msg = `Opened ${m.file}:${m.line} in the editor tab`;
  note(msg);
  // Bring the editor tab to the front. Opening a window by the name of an existing one
  // switches to it (still inside the double-click's user activation); "" keeps its page.
  // Only a tab opened from the editor can find it by name: elsewhere the name would open
  // a new blank tab instead.
  if (window.opener && !window.opener.closed) {
    if (m.name) window.open("", m.name); else window.opener.focus();
  }
  setTimeout(() => { if ($("#viewer-note").textContent === msg) note(DEFAULT_NOTE); }, 5000);
}

async function checkPdf() {
  const r = await api("/api/pdfstat").catch(() => null);
  if (r && r.mtime && r.mtime !== PV.mtime) await PV.load(r.mtime);
}

if (pdfChannel) {
  pdfChannel.onmessage = async (ev) => {
    const m = ev.data || {};
    if (m.type === "jumped") jumped(m);
    else if (m.type === "reload") location.reload();      // the editor restarted with new code
    else if (m.type === "close") window.close();          // "Show here" in the editor
    else if (m.type === "pdf" && m.mtime !== PV.mtime) await PV.load(m.mtime);
    else if (m.type === "forward") {
      await checkPdf();
      PV.highlight(m.r);
      window.focus();
    }
  };
  // Hold this lock while the tab lives: the browser drops it when the tab closes or
  // crashes, which is how the editor knows the viewer is gone (see app.js).
  if (navigator.locks && navigator.locks.request)
    navigator.locks.request("prism-pdf-viewer", () => new Promise(() => {}));
  const alive = () => pdfChannel.postMessage({ type: "alive" });
  alive(); setInterval(alive, 2000);
  window.addEventListener("pagehide", () => pdfChannel.postMessage({ type: "bye" }));
}
// Theme changes made in the editor tab apply here too.
window.addEventListener("storage", (e) => { if (e.key === "prism.theme") applyTheme(store.get("theme", null)); });

note(DEFAULT_NOTE);
checkPdf();
setInterval(checkPdf, 2000);
