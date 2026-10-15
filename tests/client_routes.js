// Prints the requests client.js makes, one per function, as JSON [{method, path, query}]
// (tests/test_frontend.py checks them against the server's routes).
"use strict";
const Client = require("../prism_local/static/client.js");

const calls = [];
const api = (url, body) => {
  const [path, query = ""] = url.split("?");
  calls.push({ method: body === undefined ? "GET" : "POST", path, query, body: body ?? null });
  return Promise.resolve({ _status: 200 });
};
const Q = Client.create(api);

// What each function is called with here: every one of them, so a new one needs a line.
const ARGS = {
  tree: [], config: [], symbols: [], pdfstat: [], home: [], restart: [], renameProject: ["Paper"],
  snippets: [], saveSnippets: ["x"],
  file: ["sec/a b.tex"], saveFile: [{ path: "a.tex", content: "x", base_mtime: 1, force: false }],
  upload: [{ name: "p.png", data: "", destination: "" }], uploadSync: [["a.png", "b.png"]], diff: ["a.tex"],
  build: [{ mode: "latexmk", clean: false, active: "a.tex" }], buildStop: [],
  forward: ["a.tex", 3], inverse: [1, 10.5, 20.25],
  sync: ["pull"], recheck: [], github: [], publishInfo: [], publish: [{ name: "p", leave_out: [] }],
  theirs: ["a.tex"], log: ["a.tex"], show: ["abc", "a.tex"],
  agent: [{ prompt: "hi" }], events: [7, 3], stop: [7], undo: [7], info: [], usage: ["claude"],
  account: ["claude"], commands: ["claude", true],
};
const missing = Object.keys(Q).filter((k) => k !== "fileAction" && !(k in ARGS));
if (missing.length) { console.error("no ARGS for: " + missing.join(", ")); process.exit(1); }
for (const [k, args] of Object.entries(ARGS)) Q[k](...args);
for (const a of Client.FILE_ACTIONS) Q.fileAction(a, { paths: ["a.tex"] });
console.log(JSON.stringify(calls));
