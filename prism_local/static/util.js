/* Plain helpers shared by the editor (app.js) and the Home page (home.js): no page, no
   server. Loaded before common.js on both pages, and by the Node checks
   (tests/frontend_check.js) through module.exports. */
"use strict";

const Util = (() => {
  // "3 min ago", "2 h ago", "5 d ago"; "" for no time. t: seconds since 1970.
  function ago(t, now = Date.now() / 1000) {
    if (!t) return "";
    const s = now - t;
    if (s < 60) return "just now";
    const units = [[60, "min"], [3600, "h"], [86400, "d"], [86400 * 30, "mo"], [86400 * 365, "y"]];
    for (let i = units.length - 1; i >= 0; i--) {
      if (s >= units[i][0]) return Math.floor(s / units[i][0]) + " " + units[i][1] + " ago";
    }
    return "just now";
  }

  // "1 file", "2 files"; ws: the plural when it is not w + "s" ("passes").
  const plural = (n, w, ws = w + "s") => `${n} ${n === 1 ? w : ws}`;

  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // A unified diff as HTML: added, removed and hunk lines in their own spans.
  function diffHtml(diff) {
    return String(diff).split("\n").map((l) => {
      const cls = l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : "";
      return cls ? `<span class="${cls}">${escapeHtml(l)}</span>` : escapeHtml(l);
    }).join("\n");
  }

  // The server stopped to start again (an update, a folder rename): ask probe() every `ms`
  // until isNew(its answer) says another server answers. true, or false after `tries`.
  async function waitForRestart(probe, isNew, { tries = 80, ms = 500, sleep = (t) => new Promise((r) => setTimeout(r, t)) } = {}) {
    for (let i = 0; i < tries; i++) {         // the new server listens within a few seconds
      await sleep(ms);
      const answer = await Promise.resolve().then(probe).catch(() => null);
      if (answer && isNew(answer)) return true;
    }
    return false;
  }

  // What a "create a GitHub repository" form can do, from the server's publish_info:
  // why: "nested" (inside another repository), "login" (the GitHub CLI is not logged in),
  // "shared" (the whole shared repository goes), or "ok". leaveOut: the private folders
  // offered to keep off GitHub.
  function publishState(r) {
    const gh = (r && r.gh) || {};
    const why = r.kind === "nested" ? "nested" : !gh.logged_in ? "login" : r.kind === "shared" ? "shared" : "ok";
    return { why, blocked: why === "nested" || why === "login", gh,
             leaveOut: (r.private_dirs || []).filter((d) => !d.ignored) };
  }

  function createStore(access, { project = () => null, now = Date.now, onError = () => {} } = {}) {
    let used = {};
    try { used = JSON.parse(access().getItem("prism.storage.used") || "{}"); } catch { /* no history */ }
    const touch = (key) => {
      used[key] = now();
      try { access().setItem("prism.storage.used", JSON.stringify(used)); } catch { /* value already saved */ }
    };
    return {
      get(key, fallback) {
        try {
          const value = access().getItem("prism." + key);
          if (value === null) return fallback;
          if (key.endsWith(".log")) touch("prism." + key);
          return JSON.parse(value);
        } catch { return fallback; }
      },
      set(key, value) {
        const full = "prism." + key;
        try {
          const storage = access(), text = JSON.stringify(value);
          try { storage.setItem(full, text); }
          catch (e) {
            if (!(e.name === "QuotaExceededError" || e.code === 22 || e.code === 1014)) throw e;
            const candidates = Array.from({ length: storage.length }, (_, i) => storage.key(i))
              .filter((k) => { const m = /^prism\.chat\.([^.]+)\..+\.log$/.exec(k); return m && m[1] !== project() && k !== full; })
              .sort((a, b) => (used[a] || 0) - (used[b] || 0));
            let saved = false;
            for (const old of candidates) {
              storage.removeItem(old); delete used[old];
              try { storage.setItem(full, text); saved = true; break; } catch { /* next oldest */ }
            }
            if (!saved) throw e;
          }
          if (key.endsWith(".log")) touch(full);
          return true;
        } catch {
          onError("Browser storage is full or unavailable. Settings or chat history could not be saved.");
          return false;
        }
      },
    };
  }
  function boundedLog(elements, limit = 400000) {
    let html = "";
    for (let i = elements.length - 1; i >= 0; i--) if (elements[i].length + html.length <= limit) html = elements[i] + html;
    return html;
  }
  function frameBatch(draw, { schedule = requestAnimationFrame, cancel = cancelAnimationFrame } = {}) {
    const pending = new Map(); let frame = null;
    function flush() {
      if (frame !== null) cancel(frame);
      frame = null;
      const batch = [...pending]; pending.clear();
      for (const [key, value] of batch) draw(key, value);
    }
    return { set(key, value) { pending.set(key, value); if (frame === null) frame = schedule(flush); }, flush };
  }
  return { ago, plural, escapeHtml, diffHtml, waitForRestart, publishState, createStore, boundedLog, frameBatch };
})();

if (typeof module !== "undefined") module.exports = Util;
