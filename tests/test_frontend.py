"""The pages' plain modules (prism_local/static/*.js with module.exports), checked under
Node by tests/frontend_check.js."""
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class Frontend(unittest.TestCase):
    def test_plain_modules(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "frontend_check.js")], capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)


if __name__ == "__main__":
    unittest.main()
