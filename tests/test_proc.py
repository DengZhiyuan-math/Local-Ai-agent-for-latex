"""Stopping a program and every program it started, also those it left running when it
exited (proc.py)."""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import proc  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


def running(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, **proc.NO_WINDOW).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone(pid: int, wait: float = 5) -> bool:
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not running(pid):
            return True
        time.sleep(0.1)
    return False


def kill_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, **proc.NO_WINDOW)
    else:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


class Tree(unittest.TestCase):
    def start(self, exits: bool):
        """A program that starts one that sleeps (its pid in a file), then exits or sleeps too."""
        pidfile = tmpdir() / "grandchild.pid"
        code = ("import subprocess, sys, time; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"open({str(pidfile)!r}, 'w').write(str(p.pid))"
                + ("" if exits else "; time.sleep(60)"))
        p = proc.start([sys.executable, "-c", code])
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.05)
        grandchild = int(pidfile.read_text())
        self.addCleanup(kill_pid, grandchild)
        self.addCleanup(proc.kill_tree, p)
        return p, grandchild

    def test_stopping_ends_the_programs_it_started(self):
        p, grandchild = self.start(exits=False)
        proc.kill_tree(p)
        self.assertIsNotNone(p.poll())
        self.assertTrue(gone(grandchild), "the program it started still runs")

    def test_what_it_left_running_when_it_exited_is_ended(self):
        """On Windows nothing finds these through their parent (taskkill /T) once it is gone."""
        p, grandchild = self.start(exits=True)
        p.wait(10)
        self.assertTrue(running(grandchild), "the test's program left one running")
        proc.end_group(p)
        self.assertTrue(gone(grandchild), "what it left running still runs")

    def test_its_output_and_exit_code_are_as_without(self):
        p = proc.start([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"],
                       stdout=subprocess.PIPE, text=True)
        out, _ = p.communicate(timeout=20)
        self.assertEqual((out.strip(), p.returncode), ("hi", 3))
        proc.end_group(p)                      # nothing left: nothing happens

    def test_a_program_that_ended_on_its_own_is_left_alone(self):
        p = proc.start([sys.executable, "-c", "pass"])
        p.wait(20)
        proc.kill_tree(p)
        proc.end_group(p)
        self.assertEqual(p.returncode, 0)

    @unittest.skipUnless(os.name == "nt", "job objects: Windows")
    def test_if_the_server_dies_what_it_started_ends_with_it(self):
        pidfile = tmpdir() / "child.pid"
        code = (f"import sys, time; sys.path.insert(0, {str(Path(proc.__file__).parent)!r}); import proc; "
                "p = proc.start([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"open({str(pidfile)!r}, 'w').write(str(p.pid)); time.sleep(60)")
        server = subprocess.Popen([sys.executable, "-c", code], **proc.NO_WINDOW)
        self.addCleanup(server.kill)
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.05)
        child = int(pidfile.read_text())
        self.addCleanup(kill_pid, child)
        self.assertTrue(running(child))
        server.kill()                          # no finally, no atexit: as a crash
        server.wait(10)
        self.assertTrue(gone(child), "what the server started outlived it")


if __name__ == "__main__":
    unittest.main()
