import json
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MUTEXCC = REPO_ROOT / "mutexcc.py"


def run_mcp(stdin):
    return subprocess.run(
        [sys.executable, str(MUTEXCC), "mcp"],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def parse_output_lines(stdout):
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


class McpServeTests(unittest.TestCase):
    def test_non_object_json_rpc_frame_returns_invalid_request(self):
        for frame in ("42\n", "null\n", '"text"\n'):
            with self.subTest(frame=frame):
                result = run_mcp(frame)

                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")

                responses = parse_output_lines(result.stdout)
                self.assertEqual(
                    responses,
                    [
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32600, "message": "invalid request"},
                        }
                    ],
                )

    def test_batch_frame_does_not_stop_following_request(self):
        result = run_mcp(
            '[{"jsonrpc":"2.0","id":1,"method":"tools/list"}]\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

        responses = parse_output_lines(result.stdout)
        self.assertEqual(
            responses[0],
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "invalid request"},
            },
        )
        self.assertEqual(responses[1]["jsonrpc"], "2.0")
        self.assertEqual(responses[1]["id"], 2)
        self.assertIn("tools", responses[1]["result"])


if __name__ == "__main__":
    unittest.main()
