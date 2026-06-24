from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.storage import ArchSmithStore, ValidationError


TEST_ROOT = Path(os.environ.get("ARCHSMITH_TEST_ROOT", str(ROOT / ".test-tmp")))


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.root = Path(self.temp.name)
        self.store = ArchSmithStore(self.root)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def context(self) -> dict[str, str]:
        return {"user": "u", "profile": "p", "knowledge": "k", "module": "m"}

    def propose(self, code: str, name: str = "fn") -> dict[str, object]:
        return self.store.propose_function(
            {
                "context": self.context(),
                "name": name,
                "summary": "s",
                "language": "python",
                "code": code,
                "signature": "fn()",
                "tags": ["t"],
            }
        )

    def test_context_and_search_hide_code_by_default(self) -> None:
        self.store.upsert_context(self.context())
        proposal = self.propose("def fn():\n    return 1\n")
        self.store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        result = self.store.search_functions({"context": self.context(), "query": "s", "language": "python"})
        self.assertEqual(result["count"], 1)
        self.assertNotIn("code", result["functions"][0])
        self.assertNotIn("dependencies", result["functions"][0])
        loaded = self.store.get_function({"revision_id": proposal["revision_id"], "include_code": True})
        self.assertIn("return 1", loaded["code"])

    def test_mutation_threshold_keeps_small_change_in_public_version(self) -> None:
        first = self.propose("def fn():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n")
        self.store.approve_function({"revision_id": first["revision_id"], "approved_by": "u"})
        second = self.propose("def fn():\n    a = 1\n    b = 2\n    c = 4\n    return a + b + c\n")
        self.assertEqual(second["public_version"], 1)
        self.assertFalse(second["requires_new_version"])

    def test_large_change_creates_new_public_version_candidate(self) -> None:
        first = self.propose("def fn():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n")
        self.store.approve_function({"revision_id": first["revision_id"], "approved_by": "u"})
        second = self.propose("def fn():\n    return sum(range(10))\n")
        self.assertEqual(second["public_version"], 2)
        self.assertTrue(second["requires_new_version"])

    def test_materialize_blocks_path_traversal(self) -> None:
        proposal = self.propose("def fn():\n    return 1\n")
        self.store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        with self.assertRaises(ValidationError):
            self.store.materialize_function(
                {
                    "revision_id": proposal["revision_id"],
                    "destination_path": str(self.root / "out"),
                    "filename": "../x.py",
                    "confirm_write": True,
                }
            )
        result = self.store.materialize_function(
            {
                "revision_id": proposal["revision_id"],
                "destination_path": str(self.root / "out"),
                "confirm_write": True,
            }
        )
        self.assertTrue(Path(result["path"]).exists())

    def test_materialize_by_name_records_reuse_in_one_call(self) -> None:
        proposal = self.propose("def fn():\n    return 1\n", name="named_fn")
        self.store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        result = self.store.materialize_by_name(
            {
                "name": "named_fn",
                "destination_path": str(self.root / "out"),
                "confirm_write": True,
                "record_reuse": True,
                "client": "test",
            }
        )
        self.assertTrue(Path(result["path"]).exists())
        self.assertEqual(result["revision_id"], proposal["revision_id"])
        self.assertTrue(result["reuse_recorded"])
        self.assertGreater(result["reuse_log_id"], 0)

    def test_estimate_savings_does_not_return_code(self) -> None:
        proposal = self.propose("def fn():\n    return 1\n", name="estimate_fn")
        self.store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        result = self.store.estimate_savings({"name": "estimate_fn", "destination_path": str(self.root / "out")})
        self.assertEqual(result["name"], "estimate_fn")
        self.assertGreater(result["without_archsmith"]["total"], result["with_archsmith"]["total"])
        self.assertNotIn("code", json.dumps(result))

    def test_materialize_project_with_minor_replacement_and_estimate(self) -> None:
        code = "\n".join(
            [
                'SERVICE_NAME = "base"',
                "def project_fn():",
                "    values = []",
                '    values.append("a")',
                '    values.append("b")',
                '    values.append("c")',
                '    values.append("d")',
                '    values.append("e")',
                "    return SERVICE_NAME, values",
                "",
            ]
        )
        first = self.propose(code, name="project_fn")
        second = self.propose("def companion_fn():\n    return 'ok'\n", name="companion_fn")
        self.store.approve_function({"revision_id": first["revision_id"], "approved_by": "u"})
        self.store.approve_function({"revision_id": second["revision_id"], "approved_by": "u"})
        out_dir = self.root / "project"
        result = self.store.materialize_project(
            {
                "destination_path": str(out_dir),
                "confirm_write": True,
                "record_reuse": True,
                "client": "test",
                "functions": [
                    {
                        "name": "project_fn",
                        "filename": "project_fn.py",
                        "replacements": {'SERVICE_NAME = "base"': 'SERVICE_NAME = "tenant"'},
                    },
                    {"name": "companion_fn", "filename": "companion_fn.py"},
                ],
            }
        )
        self.assertEqual(result["count"], 2)
        self.assertTrue((out_dir / "project_fn.py").exists())
        self.assertIn("tenant", (out_dir / "project_fn.py").read_text(encoding="utf-8"))
        self.assertLessEqual(result["files"][0]["diff_ratio"], 0.2)
        estimate = self.store.estimate_project_savings(
            {
                "destination_path": str(out_dir),
                "functions": [
                    {
                        "name": "project_fn",
                        "filename": "project_fn.py",
                        "replacements": {'SERVICE_NAME = "base"': 'SERVICE_NAME = "tenant"'},
                    },
                    {"name": "companion_fn", "filename": "companion_fn.py"},
                ],
            }
        )
        self.assertGreater(estimate["without_archsmith"]["total"], estimate["with_archsmith"]["total"])
        self.assertNotIn("code", json.dumps(estimate))

    def test_materialize_project_blocks_large_replacement(self) -> None:
        proposal = self.propose(
            "def blocked_fn():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n",
            name="blocked_fn",
        )
        self.store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        with self.assertRaises(ValidationError):
            self.store.materialize_project(
                {
                    "destination_path": str(self.root / "project"),
                    "confirm_write": True,
                    "functions": [
                        {
                            "name": "blocked_fn",
                            "replacements": {
                                "def blocked_fn():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n": "def blocked_fn():\n    return 99\n"
                            },
                        }
                    ],
                }
            )

    def test_secret_like_assignment_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.propose("to" + "ken = 'abcdefghi'\n")


class McpCase(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_tools_list_over_stdio(self) -> None:
        env = os.environ.copy()
        env["ARCHSMITH_HOME"] = self.temp.name
        server = ROOT / "mcp" / "server.py"
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        payload = b"".join(frame(message) for message in messages)
        proc = subprocess.run(
            [sys.executable, str(server), "--stdio"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
            timeout=10,
        )
        responses = parse_frames(proc.stdout)
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "archsmith-mcp")
        tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertIn("archsmith_search_functions", tool_names)
        self.assertIn("archsmith_materialize_by_name", tool_names)
        self.assertIn("archsmith_materialize_project", tool_names)
        self.assertIn("archsmith_estimate_savings", tool_names)
        self.assertIn("archsmith_estimate_project_savings", tool_names)

    def test_materialize_by_name_over_stdio(self) -> None:
        env = os.environ.copy()
        env["ARCHSMITH_HOME"] = self.temp.name
        store = ArchSmithStore(Path(self.temp.name))
        try:
            proposal = store.propose_function(
                {
                    "context": {"user": "u", "profile": "p", "knowledge": "k", "module": "m"},
                    "name": "mcp_direct_fn",
                    "summary": "s",
                    "language": "python",
                    "code": "def mcp_direct_fn():\n    return 1\n",
                }
            )
            store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        finally:
            store.close()
        server = ROOT / "mcp" / "server.py"
        out_dir = Path(self.temp.name) / "out"
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "archsmith_materialize_by_name",
                    "arguments": {
                        "name": "mcp_direct_fn",
                        "destination_path": str(out_dir),
                        "confirm_write": True,
                        "record_reuse": True,
                        "client": "test",
                    },
                },
            },
        ]
        proc = subprocess.run(
            [sys.executable, str(server), "--stdio"],
            input=b"".join(frame(message) for message in messages),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
            timeout=10,
        )
        responses = parse_frames(proc.stdout)
        result = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertTrue(Path(result["path"]).exists())
        self.assertTrue(result["reuse_recorded"])
        self.assertEqual(result["name"], "mcp_direct_fn")

    def test_cli_materialize_by_name(self) -> None:
        env = os.environ.copy()
        env["ARCHSMITH_HOME"] = self.temp.name
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        store = ArchSmithStore(Path(self.temp.name))
        try:
            proposal = store.propose_function(
                {
                    "context": {"user": "u", "profile": "p", "knowledge": "k", "module": "m"},
                    "name": "cli_fn",
                    "summary": "s",
                    "language": "python",
                    "code": "def cli_fn():\n    return 1\n",
                }
            )
            store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        finally:
            store.close()
        out_dir = Path(self.temp.name) / "out"
        cli = ROOT / "mcp" / "cli.py"
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(cli),
                "materialize",
                "cli_fn",
                "--destination",
                str(out_dir),
                "--confirm-write",
                "--record-reuse",
                "--minimal",
                "--client",
                "test",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            check=True,
            timeout=10,
        )
        result = json.loads(proc.stdout)
        self.assertTrue(Path(result["path"]).exists())
        self.assertTrue(result["reuse_recorded"])
        self.assertNotIn("name", result)

    def test_cli_reuse_only(self) -> None:
        env = os.environ.copy()
        env["ARCHSMITH_HOME"] = self.temp.name
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        store = ArchSmithStore(Path(self.temp.name))
        try:
            proposal = store.propose_function(
                {
                    "context": {"user": "u", "profile": "p", "knowledge": "k", "module": "m"},
                    "name": "reuse_fn",
                    "summary": "s",
                    "language": "python",
                    "code": "def reuse_fn():\n    return 1\n",
                }
            )
            store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
            revision_id = int(proposal["revision_id"])
        finally:
            store.close()
        cli = ROOT / "mcp" / "cli.py"
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(cli),
                "reuse",
                "--revision-id",
                str(revision_id),
                "--project-path",
                str(Path(self.temp.name) / "out"),
                "--client",
                "test",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            check=True,
            timeout=10,
        )
        result = json.loads(proc.stdout)
        self.assertEqual(result["revision_id"], revision_id)
        self.assertGreater(result["reuse_log_id"], 0)

    def test_cli_estimate(self) -> None:
        env = os.environ.copy()
        env["ARCHSMITH_HOME"] = self.temp.name
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        store = ArchSmithStore(Path(self.temp.name))
        try:
            proposal = store.propose_function(
                {
                    "context": {"user": "u", "profile": "p", "knowledge": "k", "module": "m"},
                    "name": "estimate_cli_fn",
                    "summary": "s",
                    "language": "python",
                    "code": "def estimate_cli_fn():\n    return 1\n",
                }
            )
            store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        finally:
            store.close()
        cli = ROOT / "mcp" / "cli.py"
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(cli),
                "estimate",
                "estimate_cli_fn",
                "--destination",
                str(Path(self.temp.name) / "out"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            check=True,
            timeout=10,
        )
        result = json.loads(proc.stdout)
        self.assertEqual(result["name"], "estimate_cli_fn")
        self.assertGreater(result["without_archsmith"]["total"], result["with_archsmith"]["total"])

    def test_cli_project_from_spec(self) -> None:
        env = os.environ.copy()
        env["ARCHSMITH_HOME"] = self.temp.name
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        store = ArchSmithStore(Path(self.temp.name))
        try:
            proposal = store.propose_function(
                {
                    "context": {"user": "u", "profile": "p", "knowledge": "k", "module": "m"},
                    "name": "project_cli_fn",
                    "summary": "s",
                    "language": "python",
                    "code": "\n".join(
                        [
                            'NAME = "base"',
                            "def project_cli_fn():",
                            "    items = []",
                            '    items.append("a")',
                            '    items.append("b")',
                            '    items.append("c")',
                            '    items.append("d")',
                            "    return NAME, items",
                            "",
                        ]
                    ),
                }
            )
            store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        finally:
            store.close()
        out_dir = Path(self.temp.name) / "project"
        spec_path = Path(self.temp.name) / "spec.json"
        spec_path.write_text(
            json.dumps(
                {
                    "destination_path": str(out_dir),
                    "functions": [
                        {
                            "name": "project_cli_fn",
                            "filename": "project_cli_fn.py",
                            "replacements": {'NAME = "base"': 'NAME = "tenant"'},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        cli = ROOT / "mcp" / "cli.py"
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(cli),
                "project",
                "--spec",
                str(spec_path),
                "--confirm-write",
                "--record-reuse",
                "--minimal",
                "--client",
                "test",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            check=True,
            timeout=10,
        )
        result = json.loads(proc.stdout)
        self.assertEqual(result["count"], 1)
        self.assertTrue((out_dir / "project_cli_fn.py").exists())


def frame(message: dict[str, object]) -> bytes:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload


def parse_frames(data: bytes) -> list[dict[str, object]]:
    responses: list[dict[str, object]] = []
    offset = 0
    while offset < len(data):
        header_end = data.index(b"\r\n\r\n", offset)
        header = data[offset:header_end].decode("ascii")
        length = int(header.split(":", 1)[1].strip())
        start = header_end + 4
        end = start + length
        responses.append(json.loads(data[start:end]))
        offset = end
    return responses


if __name__ == "__main__":
    unittest.main()
