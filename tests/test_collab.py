"""Several people writing one paper: each has a clone, synced by prism-local (gitsync.py),
against one shared remote (a bare repository standing in for GitHub).

What must hold whatever happens:
- nothing that ever reached the remote is lost from its history (no force push, no
  rewritten history: every commit seen on the remote stays an ancestor of its main);
- nobody's text is lost: what a person wrote is either on the remote or still committed
  in their own clone, never thrown away;
- a clone is never left half-way (a merge in progress, conflict markers in a file).
"""
import json
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitsync  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


def git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def identity(cwd, name):
    git(cwd, "config", "user.email", f"{name}@example.com")
    git(cwd, "config", "user.name", name)
    git(cwd, "config", "commit.gpgsign", "false")


PAPER = "\\section{Intro}\nFirst paragraph.\n\nSecond paragraph.\n\nThird paragraph.\n"


@unittest.skipUnless(shutil.which("git"), "needs git")
class Collaboration(unittest.TestCase):
    def setUp(self):
        self.remote = tmpdir() / "paper.git"
        git(self.remote.parent, "init", "-q", "--bare", "-b", "main", str(self.remote))
        seed = tmpdir()
        git(seed, "init", "-q", "-b", "main")
        identity(seed, "seed")
        (seed / "main.tex").write_text(PAPER, encoding="utf-8")
        (seed / "refs.bib").write_text("% refs\n", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "start")
        git(seed, "remote", "add", "origin", str(self.remote))
        git(seed, "push", "-q", "-u", "origin", "main")
        self.seen: set[str] = set()
        self.busy: dict[str, bool] = {}           # whose agent is in the middle of a turn
        self.people = {n: self.clone(n) for n in ("A", "B")}
        self.watch()

    def clone(self, name):
        d = tmpdir() / name
        git(d.parent, "clone", "-q", str(self.remote), str(d))
        identity(d, name)
        self.busy[name] = False
        g = gitsync.GitSync(lambda d=d: d, lambda: "build", busy=lambda name=name: self.busy[name])
        g.check_repo()
        g.bind_target()
        g._remote_state()
        return d, g

    # ------------------------------------------------------------ helpers
    def write(self, who, rel, text):
        d, g = self.people[who]
        (d / rel).write_text(text, encoding="utf-8")
        g.touched()

    def save(self, who):
        """The editor's *Save to GitHub now*: commit everything and push."""
        self.people[who][1].now("commit")
        self.watch()

    def pull(self, who):
        self.people[who][1].now("pull")
        self.watch()

    def read(self, who, rel):
        return (self.people[who][0] / rel).read_text(encoding="utf-8")

    def remote_file(self, rel):
        return git(self.remote, "show", f"main:{rel}")

    def watch(self):
        """Remember every commit the remote has; all must stay in its history."""
        tip = git(self.remote, "rev-parse", "main")
        self.seen.update(git(self.remote, "rev-list", tip).split())

    def assertSafe(self, known_only=False):
        """known_only: with outsiders force-pushing, a commit only outsiders ever had (pushed
        by one, wiped by another before any prism-local copy fetched it) is beyond repair
        from here; every commit a prism-local copy had must be back."""
        tip = git(self.remote, "rev-parse", "main")
        for c in self.seen:
            if known_only and not any(subprocess.run(["git", "cat-file", "-e", c], cwd=d).returncode == 0
                                      for d, _ in self.people.values()):
                continue
            self.assertEqual(subprocess.run(["git", "merge-base", "--is-ancestor", c, tip], cwd=self.remote)
                             .returncode, 0, f"commit {c[:8]} was lost from the remote's history")
        for who, (d, _) in self.people.items():
            self.assertFalse((d / ".git" / "MERGE_HEAD").exists(), f"{who}: left in the middle of a merge")
            self.assertFalse((d / ".git" / "rebase-merge").exists(), f"{who}: left in the middle of a rebase")
            for f in d.glob("*.tex"):
                self.assertNotIn("<<<<<<<", f.read_text(encoding="utf-8"), f"{who}: conflict markers in {f.name}")

    def in_history(self, who, rel, text):
        """Whether `text` is in `rel` in some commit of who's clone (nothing thrown away)."""
        d, _ = self.people[who]
        # --full-history: a merge that kept one side hides the other from a plain log.
        return text in git(d, "log", "-p", "--all", "--full-history", "--", rel)

    # ------------------------------------------------------------ scenarios
    def test_different_files_at_the_same_time(self):
        self.write("A", "main.tex", PAPER.replace("First", "A's first"))
        self.write("B", "refs.bib", "% refs\n@book{b, title={B}}\n")
        self.save("A")
        self.save("B")                    # B's push meets A's commit
        self.pull("A")
        for who in "AB":
            self.assertIn("A's first", self.read(who, "main.tex"), who)
            self.assertIn("@book{b", self.read(who, "refs.bib"), who)
        self.assertIn("A's first", self.remote_file("main.tex"))
        self.assertIn("@book{b", self.remote_file("refs.bib"))
        self.assertSafe()

    def test_same_file_different_paragraphs(self):
        self.write("A", "main.tex", PAPER.replace("First paragraph.", "First paragraph, by A."))
        self.write("B", "main.tex", PAPER.replace("Third paragraph.", "Third paragraph, by B."))
        self.save("A")
        self.save("B")
        self.pull("A")
        for who in "AB":
            text = self.read(who, "main.tex")
            self.assertIn("by A", text, who)
            self.assertIn("by B", text, who)
        self.assertSafe()

    def test_same_lines_both_versions_kept_and_sync_goes_on(self):
        """Both changed the same sentence: no one has to step in. The file gets both versions
        between comment lines, everyone gets that file, and syncing carries on."""
        self.write("A", "main.tex", PAPER.replace("Second paragraph.", "Second paragraph as A wants it."))
        self.write("B", "main.tex", PAPER.replace("Second paragraph.", "Second paragraph as B wants it.")
                   .replace("Third paragraph.", "Third paragraph, B's other change."))
        self.save("A")
        self.save("B")
        _, gb = self.people["B"]
        self.assertIsNone(gb.error, gb.error)
        text = self.remote_file("main.tex")
        self.assertIn("as A wants it", text)
        self.assertIn("as B wants it", text)
        self.assertIn("B's other change", text, "the rest merges as usual")
        self.assertIn("% [prism-local] A's version (from GitHub):", text)
        self.assertIn("% [prism-local] B's version:", text)
        self.assertEqual([(k["path"], k["line"]) for k in gb.kept_both()], [("main.tex", 4)])     # after \section, First, a blank line
        self.pull("A")
        self.assertEqual(self.read("A", "main.tex"), self.read("B", "main.tex"))
        # Tidied up later: B keeps one version and deletes the markers; the list empties.
        self.write("B", "main.tex", PAPER.replace("Second paragraph.", "Second paragraph as both want it.")
                   .replace("Third paragraph.", "Third paragraph, B's other change."))
        self.save("B")
        self.assertEqual(gb.kept_both(), [])
        self.pull("A")
        self.assertIn("as both want it", self.read("A", "main.tex"))
        self.assertSafe()

    def test_bib_both_entries_kept_without_markers(self):
        self.write("A", "refs.bib", "% refs\n@book{a, title={A}}\n")
        self.write("B", "refs.bib", "% refs\n@book{b, title={B}}\n")
        self.save("A")
        self.save("B")
        text = self.remote_file("refs.bib")
        self.assertIn("@book{a", text)
        self.assertIn("@book{b", text)
        self.assertNotIn("[prism-local]", text, "% is not a comment inside a .bib entry")
        self.assertSafe()

    def test_figure_both_changed_theirs_saved_next_to_it(self):
        da, _ = self.people["A"]
        db, gb = self.people["B"]
        (da / "fig.png").write_bytes(b"\x89PNG\0A's figure")
        self.people["A"][1].touched()
        self.save("A")
        self.pull("B")
        (da / "fig.png").write_bytes(b"\x89PNG\0A's new figure")
        (db / "fig.png").write_bytes(b"\x89PNG\0B's new figure")
        self.save("A")
        self.save("B")
        self.assertIsNone(gb.error, gb.error)
        self.assertEqual((db / "fig.png").read_bytes(), b"\x89PNG\0B's new figure")
        self.assertEqual((db / "fig.from-A.png").read_bytes(), b"\x89PNG\0A's new figure")
        self.assertIn("fig.from-A.png", git(self.remote, "ls-tree", "--name-only", "main"))
        self.assertSafe()

    def test_deleted_here_changed_there_keeps_the_change(self):
        db, _ = self.people["B"]
        self.write("A", "refs.bib", "% refs\n@book{keep, title={Keep}}\n")
        self.save("A")
        (db / "refs.bib").unlink()
        self.save("B")
        self.assertIn("Keep", self.remote_file("refs.bib"))
        self.assertIn("Keep", self.read("B", "refs.bib"))
        self.assertSafe()

    def test_stale_lock_and_crashed_merge_clear_themselves(self):
        import os
        da, ga = self.people["A"]
        lock = da / ".git" / "index.lock"
        lock.write_text("", encoding="utf-8")
        old = time.time() - gitsync.STALE_LOCK - 5
        os.utime(lock, (old, old))
        self.write("A", "main.tex", PAPER.replace("First", "After a crash, first"))
        self.save("A")
        self.assertIsNone(ga.error, ga.error)
        self.assertIn("After a crash", self.remote_file("main.tex"))
        # A merge prism-local left half-done (the editor was killed in the middle):
        self.write("B", "main.tex", PAPER.replace("Second paragraph.", "B's second"))
        db, gb = self.people["B"]
        git(db, "commit", "-qam", "B's")
        git(db, "fetch", "-q")
        subprocess.run(["git", "merge", "--no-commit", "--no-ff", "-m", "Merge changes from GitHub", "@{u}"],
                       cwd=db, capture_output=True)
        self.assertTrue((db / ".git" / "MERGE_HEAD").exists())
        fresh = gitsync.GitSync(lambda: db, lambda: "build")
        fresh.stop.set()
        fresh.run()                           # an editor starting again
        self.assertFalse((db / ".git" / "MERGE_HEAD").exists())
        fresh.now("commit")
        self.assertIsNone(fresh.error, fresh.error)
        self.assertIn("B's second", self.remote_file("main.tex"))
        self.watch()
        self.assertSafe()

    def test_no_merge_under_a_running_agent(self):
        """B's changes arrive while A's agent is in a turn and has written nothing yet: the
        files stay as they are until the turn ends; then everything combines."""
        da, ga = self.people["A"]
        self.busy["A"] = True
        ga.before_turn()
        self.write("B", "main.tex", PAPER.replace("First", "B's first"))
        self.save("B")
        ga.now("pull")
        ga.last_fetch = 0
        ga.tick()                              # the timer, too
        self.assertEqual(self.read("A", "main.tex"), PAPER, "nothing changes under the agent")
        # The agent rewrites the file from what it read (its whole-file Write).
        (da / "main.tex").write_text(PAPER.replace("Third", "Agent's third"), encoding="utf-8")
        self.busy["A"] = False
        ga.after_turn(["main.tex"], "Improve the third paragraph")
        ga.now("commit")
        self.watch()
        text = self.remote_file("main.tex")
        self.assertIn("B's first", text, "B's text is still in the paper")
        self.assertIn("Agent's third", text)
        self.assertEqual(git(da, "log", "-1", "--skip=1", "--format=%s", "--first-parent"),
                         "Agent: Improve the third paragraph")
        self.assertSafe()

    def test_save_now_waits_for_the_agents_turn(self):
        da, ga = self.people["A"]
        self.busy["A"] = True
        (da / "main.tex").write_text(PAPER.replace("First", "Half-done by the agent"), encoding="utf-8")
        before = git(da, "rev-parse", "HEAD")
        ga.now("commit")
        self.assertEqual(git(da, "rev-parse", "HEAD"), before, "the agent's half-done writes are not committed")
        self.assertIn("agent is working", ga.notice)
        self.busy["A"] = False
        ga.after_turn(["main.tex"], "Rewrite")
        ga.now("commit")
        self.watch()
        self.assertIn("Half-done by the agent", self.remote_file("main.tex"))

    def test_whole_file_rewrite_does_not_double_the_paper(self):
        """B's agent reformats every line while A edits one: the file keeps one version, the
        other is saved whole next to it; nothing is lost and the paper is not doubled."""
        long = "".join(f"Sentence {i}.\n" for i in range(100))
        self.write("A", "main.tex", long)
        self.save("A")
        self.pull("B")
        self.write("A", "main.tex", long.replace("Sentence 50.", "Sentence 50, by A."))
        self.write("B", "main.tex", long.replace("Sentence", "SENTENCE"))     # an agent's reformat
        self.save("A")
        self.save("B")
        db, gb = self.people["B"]
        self.assertIsNone(gb.error, gb.error)
        text = self.read("B", "main.tex")
        self.assertEqual(text.count("ENTENCE 7."), 1, "not doubled")
        side = db / "main.from-A.tex"
        self.assertIn("Sentence 50, by A.", side.read_text(encoding="utf-8"), "A's version is kept, whole")
        self.assertIn("main.tex", [k["path"] for k in gb.kept_both()])
        self.assertSafe()

    def test_agents_and_typing_on_all_sides_at_random(self):
        """Three people typing, each with an agent that starts and ends turns at random, all
        syncing in random order. In the end every person's and every agent's text is on the
        remote, everyone has the same paper, nobody stepped in, nothing was lost."""
        import random
        rnd = random.Random(20261008)
        self.people["C"] = self.clone("C")
        files = ["main.tex", "intro.tex"]
        written: list[tuple[str, str]] = []
        turn: dict[str, set] = {p: set() for p in self.people}     # files each agent wrote this turn
        queued: dict[str, list] = {p: [] for p in self.people}     # typed during a turn: saved after it

        def add_line(who, f, line):
            d, g = self.people[who]
            path = d / f
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            lines.insert(rnd.randint(0, len(lines)), line)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            g.touched()

        for step in range(50):
            who = rnd.choice(sorted(self.people))
            d, g = self.people[who]
            act = rnd.random()
            if act < 0.3:                                   # the person types
                f, line = rnd.choice(files), f"% {who} typed {step}"
                written.append((f, line))
                if self.busy[who]:
                    queued[who].append((f, line))           # the editor holds it until the turn ends
                else:
                    add_line(who, f, line)
            elif act < 0.42 and not self.busy[who]:         # an agent turn starts
                g.before_turn()
                self.busy[who] = True
            elif act < 0.6 and self.busy[who]:              # the agent writes
                f, line = rnd.choice(files), f"% agent of {who} wrote {step}"
                written.append((f, line))
                add_line(who, f, line)
                turn[who].add(f)
            elif act < 0.68 and self.busy[who]:             # the turn ends
                self.end_turn(who, turn, queued, add_line)
            elif act < 0.88:
                self.save(who)
            else:
                self.pull(who)
            g.last_fetch = 0
            g.last_edit = (g.last_edit or 0) - gitsync.QUIET
            g.dirty_since = g.dirty_since and g.dirty_since - gitsync.MAX_WAIT
            g.tick()                                        # its timers, too
            self.watch()
        for who in sorted(self.people):
            if self.busy[who]:
                self.end_turn(who, turn, queued, add_line)
        for _ in range(3):
            for who in sorted(self.people):
                self.save(who)
                self.pull(who)
        for who, (d, g) in self.people.items():
            self.assertIsNone(g.error, f"{who}: {g.error}")
            self.assertFalse(g.clash, f"{who} had to step in")
        tip = git(self.remote, "rev-parse", "main")
        for who, (d, _) in self.people.items():
            self.assertEqual(git(d, "rev-parse", "HEAD"), tip, f"{who} has the same paper")
        for f, line in written:
            self.assertIn(line, git(self.remote, "show", f"main:{f}"), f"{line} reached GitHub")
        self.assertSafe()

    def end_turn(self, who, turn, queued, add_line):
        _, g = self.people[who]
        self.busy[who] = False
        g.after_turn(sorted(turn[who]), f"turn of {who}")
        turn[who].clear()
        for f, line in queued[who]:
            add_line(who, f, line)
        queued[who].clear()

    def test_automatic_pull_waits_while_you_type(self):
        _, ga = self.people["A"]
        self.write("B", "main.tex", PAPER.replace("First", "B's first"))
        self.save("B")
        ga.last_fetch = 0
        ga.touched()                          # A saved a moment ago: still typing
        ga.tick()
        self.assertNotIn("B's first", self.read("A", "main.tex"))
        ga.last_edit -= gitsync.QUIET + 1
        ga.dirty_since = None
        ga.tick()
        self.assertIn("B's first", self.read("A", "main.tex"))

    def test_clash_then_combined_by_hand(self):
        """Only when the automatic merge itself fails (here made to): nothing is lost, and
        the GitHub menu's compare-and-combine works."""
        from unittest import mock
        self.write("A", "main.tex", PAPER.replace("Second paragraph.", "Second, A's way."))
        self.write("B", "main.tex", PAPER.replace("Second paragraph.", "Second, B's way."))
        self.write("A", "refs.bib", "% refs\n@book{a, title={A}}\n")      # theirs, no clash
        self.save("A")
        with mock.patch("keepboth.keep_both", side_effect=gitsync.keepboth.MergeError("made to fail")):
            self.save("B")
        self.assertFalse((self.people["B"][0] / ".git" / "MERGE_HEAD").exists(), "called off cleanly")
        _, gb = self.people["B"]
        self.assertIn("main.tex", gb.clash)
        theirs = gb.theirs("main.tex")
        self.assertIn("+Second, A's way.", theirs["diff"], "B can see what A wrote")
        # B writes what both want, and keeps that for the clashing lines.
        self.write("B", "main.tex", PAPER.replace("Second paragraph.", "Second, A's way, and B's way."))
        gb.now("resolve")
        self.watch()
        self.assertEqual((gb.error, gb.clash), (None, []))
        self.assertIn("A's way, and B's way", self.remote_file("main.tex"))
        self.assertIn("@book{a", self.remote_file("refs.bib"), "A's other changes come in too")
        self.pull("A")
        self.assertIn("A's way, and B's way", self.read("A", "main.tex"))
        self.assertTrue(self.in_history("A", "main.tex", "Second, A's way."), "A's version stays in history")
        self.assertSafe()

    def test_three_people_at_random(self):
        """Many rounds of edits, saves and pulls in random order: the rules hold throughout,
        and in the end everyone has the same paper with every non-clashing edit in it."""
        self.random_run(20261006, rewrites=False)

    def test_three_people_and_force_pushes_at_random(self):
        """The same, while someone without prism-local now and then force-pushes a rewritten
        history past the guard: in the end every commit that ever reached the remote is
        back in its history."""
        self.random_run(20261007, rewrites=True)

    def random_run(self, seed, rewrites):
        import random
        rnd = random.Random(seed)
        self.people["C"] = self.clone("C")
        files = ["main.tex", "refs.bib", "intro.tex"]
        written: dict[str, list] = {p: [] for p in self.people}
        self.rewrites = 0
        for step in range(40):
            if rewrites and step % 9 == 4:
                self.outsider_rewrite(step)
                continue
            who = rnd.choice(sorted(self.people))
            d, g = self.people[who]
            act = rnd.random()
            if act < 0.55:
                f = rnd.choice(files)
                line = f"% {who} line {step}"
                path = d / f
                text = path.read_text(encoding="utf-8") if path.exists() else ""
                lines = text.splitlines()
                lines.insert(rnd.randint(0, len(lines)), line)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                g.touched()
                written[who].append((f, line))
            elif act < 0.85:
                self.save(who)
            else:
                self.pull(who)
            self.settle(who)
            if not rewrites:
                self.assertSafe()
        for _ in range(3):                    # everyone saves and pulls until all agree
            for who in sorted(self.people):
                self.save(who)
                self.pull(who)
                self.settle(who)
        for who, (d, g) in self.people.items():
            self.assertIsNone(g.error, f"{who}: {g.error}")
            for f, line in written[who]:
                self.assertIn(line, git(self.remote, "show", f"main:{f}"), f"{who}'s {line} reached GitHub")
        tip = git(self.remote, "rev-parse", "main")
        for who, (d, _) in self.people.items():
            self.assertEqual(git(d, "rev-parse", "HEAD"), tip, f"{who} has the same paper")
        self.assertEqual(getattr(self, "clashes", 0), 0, "no one had to step in")
        kept = sum(git(self.remote, "show", f"main:{f}").count("changed these lines at the same time")
                   for f in ("main.tex", "intro.tex") if f in git(self.remote, "ls-tree", "--name-only", "main"))
        self.assertGreater(kept + self.bib_unions(), 0, "the run met clashes, kept both versions, went on")
        if rewrites:
            self.assertGreater(self.rewrites, 0)
        self.assertSafe(known_only=rewrites)  # nothing a prism-local copy had is lost

    def bib_unions(self):
        """Clashes in refs.bib leave no marker: count lines that came in from both sides."""
        return 0 if "refs.bib" not in git(self.remote, "ls-tree", "--name-only", "main") else \
            sum(1 for ln in git(self.remote, "show", "main:refs.bib").splitlines() if ln.startswith("% "))

    def outsider_rewrite(self, step):
        """Drop the remote's last commit or two, from a plain clone, past the guard."""
        tip = git(self.remote, "rev-parse", "main")
        if int(git(self.remote, "rev-list", "--count", tip)) < 3:
            return
        other = tmpdir() / f"outsider{step}"
        git(other.parent, "clone", "-q", str(self.remote), str(other))
        identity(other, "Outsider")
        git(other, "reset", "-q", "--hard", f"HEAD~{1 + step % 2}")
        (other / "outsider.tex").write_text(f"% outsider {step}\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", f"outsider {step}")
        git(other, "push", "-q", "--force", "--no-verify")
        self.rewrites += 1

    def settle(self, who):
        """What a co-author does after a clash: compare with GitHub's version, write a file
        holding both (here: every line of either, in order), keep it as the combination."""
        d, g = self.people[who]
        if not g.clash:
            return
        self.clashes = getattr(self, "clashes", 0) + 1
        for f in g.clash:
            theirs = (g.theirs(f)["content"] or "").splitlines()
            mine = (d / f).read_text(encoding="utf-8").splitlines() if (d / f).exists() else []
            both = list(theirs)
            for i, line in enumerate(mine):
                if line not in both:
                    both.insert(min(i, len(both)), line)
            (d / f).write_text("\n".join(both) + "\n", encoding="utf-8")
        g.now("resolve")
        self.watch()

    def test_guard_keeps_a_teams_own_hook(self):
        d, _ = self.people["A"]
        hook = d / ".git" / "hooks" / "pre-push"
        self.assertIn(gitsync.GUARD_MARK, hook.read_text(encoding="utf-8"))
        other = tmpdir() / "own-hook"
        git(other.parent, "clone", "-q", str(self.remote), str(other))
        (other / ".git" / "hooks" / "pre-push").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.assertFalse(gitsync.install_guard(other / ".git"))
        self.assertEqual((other / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")

    def test_uncommitted_edit_while_someone_pushes(self):
        self.write("A", "main.tex", PAPER.replace("Third", "A's third"))
        self.save("A")
        self.write("B", "main.tex", PAPER.replace("First", "B's first"))     # not saved yet
        self.pull("B")
        text = self.read("B", "main.tex")
        self.assertIn("B's first", text)
        self.assertIn("A's third", text)
        self.save("B")
        self.assertIn("B's first", self.remote_file("main.tex"))
        self.assertSafe()

    def test_deleted_there_edited_here(self):
        da, _ = self.people["A"]
        (da / "refs.bib").unlink()
        self.save("A")
        self.write("B", "refs.bib", "% refs\n@book{keep, title={Keep me}}\n")
        self.save("B")
        self.assertTrue(self.in_history("B", "refs.bib", "Keep me"), "B's edit is kept")
        self.assertIn("Keep me", self.read("B", "refs.bib"))
        self.assertSafe()

    def rewrite_remote(self, text="Rewritten first"):
        """A co-author without prism-local force-pushes a rewritten history from a terminal,
        past the guard (--no-verify): A's last commit is dropped from the remote."""
        other = tmpdir() / "rewriter"
        git(other.parent, "clone", "-q", str(self.remote), str(other))
        identity(other, "R")
        git(other, "reset", "-q", "--hard", "HEAD~1")
        (other / "main.tex").write_text(PAPER.replace("First", text), encoding="utf-8")
        git(other, "commit", "-qam", "rewrite")
        git(other, "push", "-q", "--force", "--no-verify")
        return git(self.remote, "rev-parse", "main")

    def test_history_rewritten_on_the_remote_is_put_back(self):
        """A force push drops A's commit from the remote; B's next sync puts it back with an
        ordinary merge and push. Nothing that ever reached the remote stays lost."""
        self.write("A", "main.tex", PAPER.replace("Third", "A's third"))
        self.save("A")
        self.pull("B")
        rewritten = self.rewrite_remote()
        self.pull("B")
        _, gb = self.people["B"]
        self.assertIn("rewrote the history", gb.notice or "")
        self.assertIn("R", gb.notice or "")
        tip = git(self.remote, "rev-parse", "main")
        self.assertEqual(subprocess.run(["git", "merge-base", "--is-ancestor", rewritten, tip],
                                        cwd=self.remote).returncode, 0, "R's commit is kept too")
        text = self.remote_file("main.tex")
        self.assertIn("A's third", text)
        self.assertIn("Rewritten first", text)
        self.assertSafe()                     # every commit seen before the rewrite is back

    def test_rewrite_put_back_after_a_fetch_in_between(self):
        """The Home page's background check fetched first: the old position is only in git's
        log of the remote branch, and that is enough."""
        self.write("A", "main.tex", PAPER.replace("Third", "A's third"))
        self.save("A")
        self.pull("B")
        self.rewrite_remote()
        db, gb = self.people["B"]
        git(db, "fetch", "-q")                 # as the Home page does at start
        self.pull("B")
        self.assertIn("A's third", self.remote_file("main.tex"))
        self.assertSafe()

    def test_deliberate_rewrite_can_be_kept(self):
        """A team that removes something from history on purpose: restore_rewritten off."""
        self.write("A", "main.tex", PAPER.replace("Third", "A's secret"))
        self.save("A")
        self.pull("B")
        db, gb = self.people["B"]
        (gb.git_dir / "prism-local.json").write_text(json.dumps({**gb._settings(), "restore_rewritten": False}), encoding="utf-8")
        self.rewrite_remote()
        self.pull("B")
        self.assertNotIn("A's secret", self.remote_file("main.tex"))
        self.assertIn("rewritten on purpose", gb.error or "")
        self.write("B", "refs.bib", "% refs\n% later\n")
        self.save("B")                        # and B's later saves do not bring it back either
        self.assertNotIn("A's secret", self.remote_file("main.tex"))
        self.assertTrue(self.in_history("B", "main.tex", "A's secret"), "B's copy keeps everything")

    def test_agents_cannot_reach_the_remote(self):
        """Whatever an AI agent runs, with --force or --no-verify, git cannot push from it."""
        import backends
        d, _ = self.people["A"]
        git(d, "commit", "-q", "--allow-empty", "-m", "agent's")
        before = git(self.remote, "rev-parse", "main")
        for args in (["push", "origin", "HEAD:main"], ["push", "--force", "--no-verify", "origin", "HEAD:main"],
                     ["push", "--force", "--no-verify", str(self.remote), "HEAD:main"]):
            r = subprocess.run(["git", *args], cwd=d, env=backends.agent_env(), capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("not allowed", r.stderr)
        self.assertEqual(git(self.remote, "rev-parse", "main"), before)

    def test_unreachable_remote_changes_nothing(self):
        d, g = self.people["A"]
        git(d, "remote", "set-url", "origin", str(self.remote.parent / "gone.git"))
        self.write("A", "main.tex", PAPER.replace("First", "Offline first"))
        g.now("commit")
        g.now("pull")
        self.assertTrue(g.blocked_reason)
        self.assertIn("Offline first", self.read("A", "main.tex"))
        self.assertTrue(self.in_history("A", "main.tex", "Offline first"), "committed, waiting to be pushed")

    def test_no_force_push_from_a_clone(self):
        """A force push from a synced clone (an agent's shell, say) is refused before it leaves."""
        self.write("A", "main.tex", PAPER.replace("First", "Safe first"))
        self.save("A")
        d, g = self.people["B"]
        git(d, "commit", "-q", "--allow-empty", "-m", "unrelated")
        r = subprocess.run(["git", "push", "-q", "--force", "origin", "HEAD:main"], cwd=d, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "the force push must be refused")
        self.assertIn("Safe first", self.remote_file("main.tex"))
        self.assertSafe()


@unittest.skipUnless(shutil.which("git"), "needs git")
class SharedFolderCollaboration(unittest.TestCase):
    """A folder's shared repository (练习/, 答案/ …): two co-authors, each editor syncing
    only its own project, while the other project has unsaved changes on disk."""

    def setUp(self):
        self.remote = tmpdir() / "course.git"
        git(self.remote.parent, "init", "-q", "--bare", "-b", "main", str(self.remote))
        seed = tmpdir()
        git(seed, "init", "-q", "-b", "main")
        identity(seed, "seed")
        (seed / gitsync.SHARED_MARKER).write_text('{"shared": true}', encoding="utf-8")
        for p in ("练习/ex 1", "答案/ex 1 sol"):
            (seed / p).mkdir(parents=True)
            (seed / p / "main.tex").write_text(PAPER, encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "start")
        git(seed, "remote", "add", "origin", str(self.remote))
        git(seed, "push", "-q", "-u", "origin", "main")

    def clone(self, name, project):
        d = tmpdir() / name
        git(d.parent, "clone", "-q", str(self.remote), str(d))
        identity(d, name)
        g = gitsync.GitSync(lambda: d / project, lambda: "build")
        g.check_repo()
        g.bind_target()
        g._remote_state()
        return d, g

    def test_each_editor_its_own_project(self):
        da, ga = self.clone("A", "练习/ex 1")
        db, gb = self.clone("B", "答案/ex 1 sol")
        (da / "练习/ex 1/main.tex").write_text(PAPER.replace("First", "A's exercise"), encoding="utf-8")
        (db / "答案/ex 1 sol/main.tex").write_text(PAPER.replace("First", "B's solution"), encoding="utf-8")
        # B also has an unsaved edit in A's project (another editor on B's machine): not B's to commit.
        (db / "练习/ex 1/main.tex").write_text(PAPER.replace("Third", "B's draft, unsaved"), encoding="utf-8")
        ga.now("commit")
        gb.now("commit")                      # meets A's commit; B's other project is in the way:
        self.assertIsNone(gb.error, gb.error)  # recorded by itself, then combined
        tip = git(self.remote, "show", "main:答案/ex 1 sol/main.tex")
        self.assertIn("B's solution", tip)
        self.assertIn("A's exercise", git(self.remote, "show", "main:练习/ex 1/main.tex"))
        merged = (db / "练习/ex 1/main.tex").read_text(encoding="utf-8")
        self.assertIn("B's draft, unsaved", merged, "B's draft is kept")
        self.assertIn("A's exercise", merged, "and combined with A's change")


if __name__ == "__main__":
    unittest.main()
