"""Agent label navigation uses the project's current visible sources."""
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from project import Project
from tmpdirs import tmpdir


def make(files):
    root = tmpdir()
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return Project(root)


class Labels(unittest.TestCase):
    def test_resolves_labels_in_multiple_files(self):
        project = make({"main.tex": "\\input{sections/intro}\n\\label{sec:main}\n",
                        "sections/intro.tex": "Intro\n\\label{sec:intro}\n"})
        index = project.label_index()
        self.assertEqual(index["labels"]["sec:intro"],
                         [{"label": "sec:intro", "file": "sections/intro.tex", "line": 2, "kind": ""}])
        result = project.resolve_label("sec:intro", index["version"])
        self.assertEqual((result["status"], result["file"], result["line"], result["stale"]),
                         ("ok", "sections/intro.tex", 2, False))

    def test_label_spacing_and_comments(self):
        project = make({"main.tex": "% \\label{ignored}\n\\label {sec:one} % \\label{ignored}\n"})
        self.assertEqual(list(project.label_index()["labels"]), ["sec:one"])
        self.assertEqual(project.resolve_label("sec:one")["line"], 2)

    def test_duplicates_are_explicit_and_become_resolvable_after_removal(self):
        project = make({"main.tex": "\\label{same}\n", "other.tex": "\\label{same}\n"})
        index = project.label_index()
        duplicate = project.resolve_label("same", index["version"])
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertNotIn("file", duplicate)
        self.assertEqual({item["file"] for item in duplicate["matches"]}, {"main.tex", "other.tex"})
        (project.root / "other.tex").unlink()
        current = project.resolve_label("same", index["version"])
        self.assertEqual((current["status"], current["file"], current["stale"]), ("ok", "main.tex", True))

    def test_click_rechecks_even_when_size_and_timestamp_were_preserved(self):
        project = make({"main.tex": "\\label{old}\n", "other.tex": "\\label{new}\n"})
        index = project.label_index()
        path = project.root / "main.tex"
        stamp = path.stat()
        path.write_text("\\label{new}\n", encoding="utf-8")
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        removed = project.resolve_label("old", index["version"])
        self.assertEqual((removed["status"], removed["matches"], removed["stale"]), ("missing", [], True))
        self.assertEqual(project.resolve_label("new")["status"], "duplicate")

    def test_line_moves_and_new_files_are_found_on_click(self):
        project = make({"main.tex": "\\label{a}\n"})
        index = project.label_index()
        (project.root / "main.tex").write_text("\n\n\\label{a}\n", encoding="utf-8")
        moved = project.resolve_label("a", index["version"])
        self.assertEqual((moved["line"], moved["stale"]), (3, True))
        (project.root / "added.tex").write_text("\\label{b}\n", encoding="utf-8")
        self.assertEqual(project.resolve_label("b")["file"], "added.tex")

    def test_hidden_excluded_generated_and_external_sources_are_not_targets(self):
        project = make({"main.tex": "\\input{.private}\n\\input{skip}\n\\input{build/out}\n\\label{visible}\n",
                        ".private.tex": "\\label{private}\n", "skip.tex": "\\label{skip}\n",
                        "build/out.tex": "\\label{out}\n", "prism.json": '{"exclude": ["skip.tex"]}'})
        outside = tmpdir() / "outside.tex"
        outside.write_text("\\label{outside}\n", encoding="utf-8")
        try:
            (project.root / "link.tex").symlink_to(outside)
        except (OSError, NotImplementedError):
            pass
        self.assertEqual(list(project.label_index()["labels"]), ["visible"])
        for label in ("private", "skip", "out", "outside"):
            self.assertEqual(project.resolve_label(label)["status"], "missing")

    def test_invalid_label_input_is_refused(self):
        project = make({"main.tex": ""})
        for label in (None, [], "", "x" * 513):
            with self.subTest(label=label), self.assertRaises(ValueError):
                project.resolve_label(label)


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class ChatLabels(unittest.TestCase):
    def test_agent_reply_references(self):
        result = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("labels_check.js"))],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
