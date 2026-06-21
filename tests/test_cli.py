import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "mutexcc.py"


def run_mutexcc(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


class ReleaseCliTests(unittest.TestCase):
    def test_release_requires_path_without_all(self):
        result = run_mutexcc("release")

        self.assertEqual(result.returncode, 2)
        self.assertIn("release requires a path unless --all is used", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_release_with_agent_still_requires_target(self):
        result = run_mutexcc("release", "--agent", "agent-1")

        self.assertEqual(result.returncode, 2)
        self.assertIn("release requires a path unless --all is used", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
