"""Run the isolated Home push check in headless Chrome on Windows."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'tests'))
from test_lifecycle import NO_WINDOW, stop, wait_file


def main():
    with tempfile.TemporaryDirectory(prefix='prism-push-check-') as directory:
        work = Path(directory).resolve()
        ready, profile = work / 'home.json', work / 'chrome'
        env = {**os.environ, 'PRISM_STATE_DIR': str(work / 'state'), 'PRISM_AUTO_UPDATE': '0'}
        with (work / 'home.log').open('wb') as log:
            server = subprocess.Popen([sys.executable, str(REPO / 'prism_local/hub.py'), '--port', '0',
                                       '--no-browser', '--ready-file', str(ready)], env=env,
                                      stdout=log, stderr=log, creationflags=NO_WINDOW)
            chrome = None
            try:
                url = wait_file(ready)['url']
                chrome = subprocess.Popen([r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                    '--headless=new', '--remote-debugging-port=0', '--user-data-dir=' + str(profile),
                    '--no-first-run', '--disable-gpu', 'about:blank'], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
                active, deadline = profile / 'DevToolsActivePort', time.monotonic() + 15
                while not active.exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                port = active.read_text().splitlines()[0]
                result = subprocess.run(['node', str(REPO / 'tests/browser/project_events.mjs'), url,
                    str(work), 'http://127.0.0.1:' + port], capture_output=True, text=True,
                    encoding='utf-8', timeout=35)
                print(result.stdout, end='')
                print(result.stderr, end='')
                if result.returncode:
                    print((work / 'home.log').read_text(encoding='utf-8', errors='replace')[-3000:])
                return result.returncode
            finally:
                stop(server)
                if chrome:
                    subprocess.run(['taskkill', '/T', '/F', '/PID', str(chrome.pid)], capture_output=True)
                    chrome.wait(timeout=10)
                    time.sleep(.3)  # Chrome releases its profile handles after Browser.close


if __name__ == '__main__':
    sys.exit(main())
