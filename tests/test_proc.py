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


class Run(unittest.TestCase):
    """proc.run: subprocess.run whose time limit holds, also when a program the command
    started keeps its output pipes open (git's https helper, the node behind a .cmd shim)."""

    def holder(self, parent_waits: bool):
        """A command that starts a program holding its stdout and stderr, then sleeps or exits."""
        pidfile = tmpdir() / "holder.pid"
        self.addCleanup(lambda: pidfile.exists() and pidfile.read_text() and kill_pid(int(pidfile.read_text())))
        code = ("import subprocess, sys, time; print('started', flush=True); "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"open({str(pidfile)!r}, 'w').write(str(p.pid))"
                + ("; time.sleep(60)" if parent_waits else "; sys.exit(4)"))
        return [sys.executable, "-c", code], pidfile

    def test_the_time_limit_holds_and_ends_everything_the_command_started(self):
        cmd, pidfile = self.holder(parent_waits=True)
        t = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            proc.run(cmd, timeout=1.5)
        self.assertLess(time.monotonic() - t, 1.5 + proc.RUN_GRACE + 3)
        self.assertTrue(gone(int(pidfile.read_text())), "what the command started still runs")

    def test_a_command_that_exited_is_done_although_what_it_left_holds_the_pipes(self):
        cmd, pidfile = self.holder(parent_waits=False)
        t = time.monotonic()
        r = proc.run(cmd, timeout=30, text=True)
        self.assertLess(time.monotonic() - t, 10, "it waited for the program the command left")
        self.assertEqual((r.returncode, r.stdout.strip()), (4, "started"))
        self.assertTrue(gone(int(pidfile.read_text())), "what the command left still runs")

    def test_a_daemon_the_command_started_on_its_own_carries_on(self):
        """git's fsmonitor daemon, gpg-agent: started apart from the command's pipes, they
        outlive it under subprocess.run, and did not once the command's job handle closed."""
        import gc
        pidfile = tmpdir() / "daemon.pid"
        self.addCleanup(lambda: pidfile.exists() and pidfile.read_text() and kill_pid(int(pidfile.read_text())))
        apart = "creationflags=0x208" if os.name == "nt" else "start_new_session=True"   # DETACHED, NEW_GROUP
        code = ("import subprocess, sys; d = subprocess.DEVNULL; "
                f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=d, stdout=d, stderr=d, {apart}); "
                f"open({str(pidfile)!r}, 'w').write(str(p.pid))")
        self.assertEqual(proc.run([sys.executable, "-c", code], timeout=30).returncode, 0)
        gc.collect()
        time.sleep(1)
        self.assertTrue(running(int(pidfile.read_text())), "the daemon was ended with the command")

    def test_output_input_exit_code_and_check_are_subprocess_runs(self):
        echo = [sys.executable, "-c", "import sys; d = sys.stdin.read(); print(d.upper()); "
                                      "print('e', file=sys.stderr); sys.exit(2)"]
        r = proc.run(echo, timeout=20, input="ok", text=True)
        self.assertEqual((r.returncode, r.stdout.strip(), r.stderr.strip()), (2, "OK", "e"))
        self.assertEqual(proc.run(echo, timeout=20).stdout.strip(), b"", "no input: stdin is empty")
        with self.assertRaises(subprocess.CalledProcessError):
            proc.run(echo, timeout=20, input="x", text=True, check=True)
        with self.assertRaises(OSError):
            proc.run(["prism-no-such-program"], timeout=5)


if __name__ == "__main__":
    unittest.main()
