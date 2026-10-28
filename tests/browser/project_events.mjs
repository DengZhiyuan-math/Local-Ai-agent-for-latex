// Filesystem -> native notification -> SSE -> visible Home, with no reload or polling.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
const [url, work, cdp = 'http://127.0.0.1:9333'] = process.argv.slice(2);
const root = path.join(path.resolve(work), 'fixture', 'explorer-events');
const make = (p, title) => { fs.mkdirSync(p, { recursive: true }); fs.writeFileSync(path.join(p, 'main.tex'), `\\documentclass{article}\n\\title{${title}}\n`); };
const original = path.join(root, 'original');
make(original, 'Original title');
const post = async (route, data) => {
  const r = await fetch(url + route, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Prism-Local': '1' }, body: JSON.stringify(data) });
  assert.equal(r.status, 200); return r.json();
};
const group = await post('api/folders/create', { name: 'explorer-events' });
await post('api/projects/add', { path: original, folder_id: group.id });
const tab = await (await fetch(cdp + '/json/new?' + encodeURIComponent(url), { method: 'PUT' })).json();
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r));
let sequence = 0, requests = 0;
const pending = new Map(), errors = [];
ws.addEventListener('close', e => { for (const p of pending.values()) p.reject(Error(`CDP closed: ${e.code} ${e.reason}`)); pending.clear(); });
ws.addEventListener('message', e => {
  const m = JSON.parse(e.data);
  if (m.id) { const p = pending.get(m.id); pending.delete(m.id); if (m.error) p.reject(Error(m.error.message)); else p.resolve(m.result); }
  if (m.method === 'Network.requestWillBeSent' && new URL(m.params.request.url).pathname === '/api/projects') requests++;
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.text);
});
function send(method, params = {}) { return new Promise((resolve, reject) => { const id = ++sequence; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params })); }); }
async function evaluate(expression) { const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw Error(r.exceptionDetails.text); return r.result.value; }
const pause = ms => new Promise(r => setTimeout(r, ms));
async function until(expression) { const end = Date.now() + 5000; while (Date.now() < end) { if (await evaluate(expression)) return; await pause(30); } throw Error('Timed out: ' + expression); }
try {
  await send('Runtime.enable'); await send('Network.enable');
  await until('typeof H !== "undefined" && H.loaded');
  await evaluate(`setView({kind:"folder",id:${JSON.stringify(group.id)}})`);
  await until('document.querySelector("#view").textContent.includes("original")');
  await pause(350);
  const idle = requests;
  await pause(700);
  assert.equal(requests, idle, 'Idle page must not request the project list');
  console.log('PASS idle Home sends no project polling requests');
  const added = path.join(root, 'new project');
  const start = Date.now(); make(added, 'New title');
  await until('document.querySelector("#view").textContent.includes("new project")');
  console.log(`PASS new project appeared via SSE in ${Date.now() - start} ms`);
  const renamed = path.join(root, 'renamed');
  assert.equal(path.dirname(original), root); assert.equal(path.dirname(renamed), root);
  fs.renameSync(original, renamed);
  await until('document.querySelector("#view").textContent.includes("renamed") && !H.projects.some(p=>p.folder==="original")');
  console.log('PASS external folder rename updated visible Home');
  fs.writeFileSync(path.join(renamed, 'main.tex'), '\\documentclass{article}\n\\title{Externally edited}\n');
  await until('H.projects.some(p=>p.title==="Externally edited")');
  console.log('PASS external source edit updated project title');
  await post('api/folders/create', { name: 'Another window group' });
  await until('H.folders.some(f=>f.name==="Another window group")');
  console.log('PASS registry update from another window pushed to Home');
  assert.deepEqual(errors, []);
} finally {
  if (process.argv[4]) {
    await send('Browser.close').catch(() => {}); // dedicated browser owned by the runner
    ws.close();
  } else {
    ws.close();
    await fetch(cdp + '/json/close/' + tab.id);
  }
}
