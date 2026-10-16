/* The editor's requests to its server, one function per route: the route names and how
   their questions are written (?path=…, encoded) are here once. Each answers what api()
   does (common.js): the JSON with its _status, {_status: 0, error} when the server cannot
   be reached; it never throws.

     Q (made below, on a page after common.js) = Client.create(api)
     await Q.file("main.tex")                // GET  /api/file?path=main.tex
     await Q.saveFile({path, content, base_mtime, force})

   Node checks that each route named here is one the server answers, with that method
   (tests/test_frontend.py). */
"use strict";

const Client = (() => {
  // "?a=1&b=2" of the given values, each encoded; undefined and null ones left out.
  function query(params = {}) {
    const parts = Object.entries(params).filter(([, v]) => v !== undefined && v !== null)
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`);
    return parts.length ? "?" + parts.join("&") : "";
  }

  function create(api) {
    const get = (path, params) => api(path + query(params));
    const post = (path, body = {}) => api(path, body);
    return {
      // the project
      tree: () => get("/api/tree"),
      config: () => get("/api/config"),
      symbols: () => get("/api/symbols"),
      pdfstat: () => get("/api/pdfstat"),
      home: () => post("/api/home"),
      restart: () => post("/api/restart"),
      renameProject: (name) => post("/api/project/rename", { name }),
      snippets: () => get("/api/snippets"),
      // files
      file: (path) => get("/api/file", { path }),
      saveFile: ({ path, content, base_mtime, force }) => post("/api/file", { path, content, base_mtime, force }),
      // the Files menu: one of FILE_ACTIONS
      fileAction: (action, body) => post("/api/files/" + action, body),
      // upload_id: the page's name for it, so removing its chip can take it back
      upload: ({ name, data, destination, upload_id, signal }) => api("/api/upload", { name, data, destination, upload_id }, { signal }),
      uploadCancel: (upload_id) => post("/api/upload/cancel", { upload_id }),
      uploadSync: (paths) => get("/api/upload/sync", { paths: paths.join("\n") }),
      diff: (path) => get("/api/diff", { path }),
      // the build and the PDF
      build: ({ mode, clean, active }) => post("/api/build", { mode, clean, active }),
      buildStop: () => post("/api/build/stop"),
      forward: (file, line) => get("/api/synctex/forward", { file, line }),
      inverse: (page, x, y) => get("/api/synctex/inverse", { page, x: x.toFixed(2), y: y.toFixed(2) }),
      // git
      sync: (action, target) => post("/api/git/sync", { action, target }),
      recheck: () => post("/api/git/recheck"),
      github: () => get("/api/git/github"),
      publishInfo: () => get("/api/git/publish"),
      publish: (body) => post("/api/git/publish", body),
      theirs: (path) => get("/api/git/theirs", { path }),
      log: (path) => get("/api/git/log", { path }),
      show: (rev, path) => get("/api/git/show", { rev, path }),
      // the agent
      agent: (body) => post("/api/agent", body),
      // wait: seconds the server may wait for new ones (20 if not given)
      events: (job, after, wait) => get("/api/agent/events", { job, after, wait }),
      stop: (job) => post("/api/agent/stop", { job }),
      undo: (turn) => post("/api/agent/undo", { turn }),
      info: () => get("/api/agent/info"),
      usage: (provider) => post("/api/agent/usage", { provider }),
      account: (provider) => get("/api/agent/account", { provider: provider || "" }),
      commands: (provider, refresh) => get("/api/agent/commands", { provider: provider || "", refresh: refresh ? 1 : undefined }),
    };
  }

  const FILE_ACTIONS = ["reveal", "copy", "paste", "rename", "new", "trash"];

  // A project file opened in a new tab, which cannot send the X-Prism-Project header: the
  // project is named in the URL, so another project that has taken the port refuses it.
  const assetUrl = (path, projectKey) => "/api/asset" + query({ path, project_key: projectKey });

  return { create, query, assetUrl, FILE_ACTIONS };
})();

// On a page (after common.js): its requests, Q.file(path) …
const Q = typeof api === "function" ? Client.create(api) : null;

if (typeof module !== "undefined") module.exports = Client;
