// Exercise the sidebar renderer with nested paths without starting a browser.
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const path = require("path");
const app = fs.readFileSync(path.join(__dirname, "../prism_local/static/app.js"), "utf8");
const start = app.indexOf("const expandedFolders = new Set();");
const end = app.indexOf("/* The Files sidebar's menu", start);
assert(start >= 0 && end > start);
const tree = { innerHTML: "", addEventListener() {}, querySelector() { return null; } };
const button = { setAttribute() {}, addEventListener() {} };
const S = { folders: ["sec/nested", "empty", "build/audits/old", "build/source"], files: [
  { path: "main.tex" }, { path: "sec/a.tex" }, { path: "sec/nested/a.tex", git: "M" },
  { path: "build/main.pdf" }, { path: "build/main.synctex.gz" }, { path: "build/source/keep.tex" },
], order: [], active: "main.tex" };
const context = { S, AH: { refresh() {} }, store: { get: () => false, set() {} },
  $: (selector) => selector === "#tree" ? tree : selector === "#btn-build-files" ? button : null,
  esc: (s) => String(s).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;") };
vm.runInNewContext(app.slice(start, end) + `
  renderTree();
  if (!tree.innerHTML.includes('data-dir="empty"') || tree.innerHTML.includes('data-dir="sec/nested"')) throw Error('initial tree');
  if (!tree.innerHTML.includes('data-dir="build"') || tree.innerHTML.includes('data-dir="build/audits"')) throw Error('build output folders');
  expandedFolders.add('build'); expandedFolders.add('build/source'); renderTree();
  if (tree.innerHTML.includes('data-path="build/main.pdf"') || tree.innerHTML.includes('data-path="build/main.synctex.gz"') || !tree.innerHTML.includes('data-path="build/source/keep.tex"')) throw Error('build output filtering');
  showBuildFiles = true; renderTree();
  if (!tree.innerHTML.includes('data-path="build/main.pdf"') || !tree.innerHTML.includes('data-dir="build/audits"')) throw Error('show build outputs');
  expandedFolders.add('sec'); renderTree();
  if (!tree.innerHTML.includes('data-dir="sec/nested"') || !tree.innerHTML.includes('data-path="sec/a.tex"') || tree.innerHTML.includes('data-path="sec/nested/a.tex"')) throw Error('direct children');
  expandFileParents('sec/nested/a.tex'); S.active = 'sec/nested/a.tex'; renderTree();
  if (!tree.innerHTML.includes('data-path="sec/nested/a.tex" class="active"') || !tree.innerHTML.includes('title="git status">M')) throw Error('nested active file');
  expandedFolders.delete('sec'); renderTree();
  if (tree.innerHTML.includes('data-path="sec/nested/a.tex"')) throw Error('collapsed descendants');
  expandedFolders.add('sec'); renderTree();
  if (!tree.innerHTML.includes('data-path="sec/nested/a.tex"')) throw Error('nested expansion retained');
`, { ...context, tree });
