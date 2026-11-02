"""Real Chrome acceptance via agent-browser and the local scripted API fixture.

Start an isolated Chrome CDP profile first, then run:
  python tests/browser/manual_edits.py --output-dir /tmp/prism-manual-edits-results
No real model, TeX command, remote repository or installed Prism app is used.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_agent import call_chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9223")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="prism-manual-browser-", dir="/tmp"))
    env = {**os.environ, "AGENT_BROWSER_SOCKET_DIR": str(work / "sockets"),
           "AGENT_BROWSER_SESSION": "manual-edits", "PRISM_SKIP_TEX": "1"}
    (work / "sockets").mkdir()
    results = []
    process = None
    connected = False

    def browser(*command):
        result = subprocess.run(["agent-browser", "--json", *command], env=env,
                                capture_output=True, text=True, timeout=35)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        reply = json.loads(result.stdout)
        if not reply["success"]:
            raise AssertionError(reply)
        return reply.get("data", {})

    def evaluate(expression):
        return browser("eval", expression).get("result")

    def until(expression):
        browser("wait", "--fn", expression)

    def snapshot(name):
        data = browser("snapshot", "-i")
        (output / (name + ".json")).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def check(name, condition, details=None):
        results.append({"name": name, "passed": bool(condition), "details": details})
        if not condition:
            raise AssertionError(name + ": " + repr(details))

    def git(*command):
        return subprocess.run(["git", *command], cwd=root, env=env, check=True,
                              capture_output=True, text=True).stdout

    def type_source(text):
        browser("focus", ".CodeMirror textarea")
        browser("press", "Meta+ArrowDown")
        check("typing starts at the end of the source", evaluate("cm.getCursor().line === cm.lineCount()-1") is True)
        browser("keyboard", "type", text)

    def dismiss_notices():
        while evaluate("document.querySelectorAll('#notifications button').length"):
            browser("find", "first", "[aria-label='Dismiss notification']", "click")

    def click_undo(turn):
        selector = f"[data-undo='{turn}']"
        dismiss_notices()
        browser("scrollintoview", selector)
        until("""(() => {
          const button = document.querySelector(%s), box = button.getBoundingClientRect();
          const hit = document.elementFromPoint(box.x+box.width/2, box.y+box.height/2);
          return hit === button || button.contains(hit);
        })()""" % json.dumps(selector))
        browser("click", selector)

    def begin(target, old, new, prompt):
        (work / "pause-api").touch()
        script = [call_chunks("read", "read_file", {"path": target}),
                  call_chunks("edit", "edit_file", {"path": target, "old_string": old, "new_string": new}),
                  [{"choices": [{"delta": {"content": "The requested proof description was updated."}}]}]]
        (work / "api-script.json").write_text(json.dumps(script))
        evaluate("Object.assign(window.QA, {done:null, waiting:false, released:false, writes:[], undos:[], trusted:[]})")
        snapshot("before-turn-" + str(len(results)))
        dismiss_notices()
        browser("fill", "#chat-input", prompt)
        browser("find", "role", "button", "click", "--name", "Send", "--exact")
        until("C.job && C.editTurn")

    def finish_held():
        (work / "pause-api").unlink()
        until("QA.waiting")
        snapshot("final-refresh-held-" + str(len(results)))
        browser("focus", ".CodeMirror textarea")
        browser("press", "Meta+s")
        state = evaluate("({held:!!C.job && C.editTurn, writes:QA.writes, draft:cm.getValue(), trusted:QA.trusted})")
        check("final refresh keeps saves held", state["held"] and not state["writes"], state)
        check("manual input uses trusted browser events", bool(state["trusted"]) and all(state["trusted"]), state["trusted"])
        evaluate("QA.released=true; QA.release(); true")
        until("!C.job && !C.editTurn")
        return evaluate("QA.done")

    try:
        log = (output / "server.log").open("w")
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("research_fixture.py")), str(work)],
                                   stdout=log, stderr=log, env=env)
        deadline = time.monotonic() + 10
        while not (work / "ready.json").exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(.02)
        ready = json.loads((work / "ready.json").read_text())
        root = Path(ready["root"])
        browser("connect", args.cdp_url)
        connected = True
        browser("open", ready["url"])
        until("typeof P !== 'undefined' && P.projectKey && typeof cm !== 'undefined' && S.active === 'main.tex'")
        snapshot("initial")
        browser("find", "role", "button", "click", "--name", "Initialize", "--exact")
        until("RESEARCH.state?.initialized === true")
        snapshot("initialized")
        browser("find", "role", "button", "click", "--name", "Research baseline", "--exact")
        check("automatic compilation is off", evaluate("document.querySelector('#auto-compile').checked") is False)
        git("init", "-q", "-b", "main")
        git("config", "user.name", "Browser fixture")
        git("config", "user.email", "browser-fixture@example.invalid")
        git("add", "-A")
        git("commit", "-qm", "Original manuscript")
        evaluate("Q.recheck()")
        metadata = {"title": browser("get", "title"), "url": browser("get", "url"),
                    "user_agent": evaluate("navigator.userAgent")}
        evaluate("""(() => {
          const original = window.fetch.bind(window);
          window.QA = {done:null, waiting:false, released:false, writes:[], allWrites:[], undos:[], trusted:[], errors:[], builds:0};
          document.querySelector('.CodeMirror textarea').addEventListener('beforeinput', e => QA.trusted.push(e.isTrusted));
          window.addEventListener('error', e => QA.errors.push(e.message));
          window.addEventListener('unhandledrejection', e => QA.errors.push(String(e.reason)));
          window.fetch = async (input, options) => {
            const path = new URL(String(input), location.href).pathname;
            if (path === '/api/build') { QA.builds++; throw Error('Unexpected compilation in browser fixture'); }
            let write = null;
            if (path === '/api/file' && options?.method === 'POST') {
              write = {path:JSON.parse(options.body).path, status:null};
              QA.writes.push(write); QA.allWrites.push(write);
            }
            const response = await original(input, options);
            if (path === '/api/agent/events') {
              const data = await response.clone().json();
              const done = data.events?.find(e => e.t === 'done');
              if (done) QA.done = done;
            }
            if (write) write.status = response.status;
            if (path === '/api/agent/undo') QA.undos.push(await response.clone().json());
            if (path === '/api/tree' && QA.done && !QA.released) {
              QA.waiting = true;
              await new Promise(resolve => { QA.release = resolve; });
            }
            return response;
          };
          return true;
        })()""")
        main_before = (root / "main.tex").read_bytes()
        intro_before = (root / "sections/intro.tex").read_bytes()

        # A: queued manual changes in another file save after final reconciliation.
        begin("sections/intro.tex", "conflicting definition", "checked definition",
              "Update the proof prose in @sections/intro.tex.")
        type_source("\n% Manual note A.\n")
        draft_a = evaluate("cm.getValue()")
        check("typing during Edit leaves disk unchanged", (root / "main.tex").read_bytes() == main_before)
        done_a = finish_held()
        until("!TABS.dirty(TABS.get('main.tex'))")
        check("queued manual text saves at turn end", (root / "main.tex").read_text() == draft_a)
        check("Agent diff excludes manual file", [c["path"] for c in done_a["changed"]] == ["sections/intro.tex"], done_a["changed"])
        check("queued save has no 409", evaluate("QA.writes.every(w => w.status === 200)") is True)
        snapshot("before-undo-other-file")
        click_undo(done_a["turn"])
        until("QA.undos.length === 1")
        check("Undo restores Agent bytes and keeps manual file", (root / "sections/intro.tex").read_bytes() == intro_before
              and (root / "main.tex").read_text() == draft_a, evaluate("QA.undos"))

        # B: simultaneous edits in one file stay separate until an explicit choice.
        begin("main.tex", "finite covering property", "checked covering property", "Update the proof prose in @main.tex.")
        type_source("\n% Manual note B.\n")
        draft_b = evaluate("cm.getValue()")
        done_b = finish_held()
        until("TABS.get('main.tex').conflict === 'disk'")
        disk_b = (root / "main.tex").read_bytes()
        check("same-file conflict retains both versions", b"checked covering property" in disk_b
              and b"Manual note B" not in disk_b and "Manual note B" in draft_b)
        check("Agent commit exists separately", "checked covering property" in git("show", "HEAD:main.tex"))
        snapshot("same-file-conflict")
        button_layout = evaluate("""(() => {
          const pane = document.querySelector('#editor-pane').getBoundingClientRect();
          return [...document.querySelectorAll('#banner button')].map(button => {
            const box = button.getBoundingClientRect(), hit = document.elementFromPoint(box.x+box.width/2, box.y+box.height/2);
            return {text:button.textContent, inside:box.left >= pane.left && box.right <= pane.right,
                    reachable:hit === button || button.contains(hit), textFits:button.scrollHeight <= button.clientHeight};
          });
        })()""")
        check("all conflict actions fit the narrow source pane and receive clicks",
              all(button["inside"] and button["reachable"] for button in button_layout), button_layout)
        check("wrapped conflict button text fits its button height", all(button["textFits"] for button in button_layout), button_layout)
        before_compare = disk_b, (root / "main.tex").stat().st_mtime_ns
        dismiss_notices()
        browser("find", "role", "button", "click", "--name", "Compare my edits with disk", "--exact")
        until("document.querySelector('#diff').textContent.includes('my unsaved edits')")
        comparison = evaluate("document.querySelector('#diff').textContent")
        check("comparison shows disk and unsaved manual draft", "-\\emph{compact} is a checked covering property." in comparison
              and "+% Manual note B." in comparison, comparison)
        check("comparison never saves", before_compare == ((root / "main.tex").read_bytes(), (root / "main.tex").stat().st_mtime_ns))
        browser("screenshot", str(output / "conflict-comparison.png"))
        snapshot("compared")
        click_undo(done_b["turn"])
        check("Undo waits for unresolved draft", evaluate("QA.undos.length") == 0 and (root / "main.tex").read_bytes() == disk_b)
        snapshot("before-keep-mine")
        browser("find", "role", "button", "click", "--name", "Keep mine (overwrite disk)", "--exact")
        until("!TABS.dirty(TABS.get('main.tex')) && !TABS.get('main.tex').conflict")
        check("explicit Keep mine saves manual choice", (root / "main.tex").read_text() == draft_b)
        snapshot("before-undo-later-save")
        click_undo(done_b["turn"])
        until("QA.undos.length === 1")
        undo_b = evaluate("QA.undos[0]")
        check("Undo protects later manual save", undo_b["restored"] == [] and undo_b["skipped"] == ["main.tex"]
              and (root / "main.tex").read_text() == draft_b, undo_b)

        # C: a turn without later source changes remains exactly undoable.
        before_c = (root / "main.tex").read_bytes()
        begin("main.tex", "finite covering property", "verified covering property", "Check the existing prose in @main.tex.")
        # No manual input in this control scenario; release without the typing assertions.
        (work / "pause-api").unlink()
        until("QA.waiting")
        evaluate("QA.released=true; QA.release(); true")
        until("!C.job && !C.editTurn")
        done_c = evaluate("QA.done")
        until("cm.getValue().includes('verified covering property')")
        snapshot("before-exact-undo")
        click_undo(done_c["turn"])
        until("QA.undos.length === 1")
        check("Undo restores exact pre-turn bytes", (root / "main.tex").read_bytes() == before_c, evaluate("QA.undos"))
        until("cm.getValue() === " + json.dumps(before_c.decode("utf-8")))
        check("Undo refreshes the source editor", evaluate("cm.getValue()") == before_c.decode("utf-8"))
        check("browser has no runtime errors", evaluate("QA.errors") == [], evaluate("QA.errors"))
        all_writes = evaluate("QA.allWrites")
        check("all scenarios save without stale requests or 409 errors", bool(all_writes)
              and all(write["status"] == 200 for write in all_writes), all_writes)
        check("no compilation was requested", evaluate("QA.builds") == 0)
        snapshot("final")
        report = {"successful": True, "metadata": metadata, "checks": results,
                  "git_log": git("log", "-6", "--format=%h %s"),
                  "scope": "Real Chrome UI and local HTTP/Git with scripted model responses; no live provider or installed app."}
        (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"successful": True, "checks": len(results), "output": str(output)}))
    except Exception as error:
        if connected:
            try:
                browser("screenshot", str(output / "failure.png"))
            except Exception:
                pass
        (output / "results.json").write_text(json.dumps({"successful": False, "checks": results, "error": str(error)},
                                                       ensure_ascii=False, indent=2) + "\n")
        raise
    finally:
        (work / "pause-api").unlink(missing_ok=True)
        if connected:
            try:
                browser("close")
            except Exception:
                pass
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log.close()
        shutil.rmtree(work)


if __name__ == "__main__":
    main()
