import os
import tempfile
import unittest

import mutexcc


class ReleaseTests(unittest.TestCase):
    def test_release_reports_noop_as_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("MUTEXCC_HOME")
            os.environ["MUTEXCC_HOME"] = tmp
            try:
                result = mutexcc.release("/tmp/none", "agent-z")
            finally:
                if old_home is None:
                    os.environ.pop("MUTEXCC_HOME", None)
                else:
                    os.environ["MUTEXCC_HOME"] = old_home

        self.assertFalse(result["ok"])
        self.assertEqual(result["released"], 0)

    def test_release_reports_deleted_lock_as_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("MUTEXCC_HOME")
            os.environ["MUTEXCC_HOME"] = tmp
            try:
                mutexcc.acquire(
                    "/tmp/held",
                    "agent-z",
                    blocking=False,
                    timeout=0,
                    ttl=0,
                    note=None,
                )
                result = mutexcc.release("/tmp/held", "agent-z")
            finally:
                if old_home is None:
                    os.environ.pop("MUTEXCC_HOME", None)
                else:
                    os.environ["MUTEXCC_HOME"] = old_home

        self.assertTrue(result["ok"])
        self.assertEqual(result["released"], 1)


if __name__ == "__main__":
    unittest.main()
