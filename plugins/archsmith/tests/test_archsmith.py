from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import sqlite3
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

    def propose_in_context(
        self,
        context: dict[str, str],
        name: str,
        summary: str,
        code: str = "def saved_fn():\n    return 1\n",
        tags: list[str] | None = None,
    ) -> dict[str, object]:
        return self.store.propose_function(
            {
                "context": context,
                "name": name,
                "summary": summary,
                "language": "python",
                "code": code,
                "signature": f"{name}()",
                "tags": tags or [],
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

    def test_recommend_reuse_exact_match_without_code(self) -> None:
        proposal = self.propose_in_context(
            self.context(),
            "invoice_export_csv",
            "export invoices to csv with approved formatting",
            tags=["billing", "csv"],
        )
        self.store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        self.store.record_reuse({"revision_id": proposal["revision_id"], "project_path": str(self.root), "client": "test"})
        result = self.store.recommend_reuse(
            {
                "task": "Create a script with invoice_export_csv",
                "context": self.context(),
                "desired_functions": ["invoice_export_csv"],
                "language": "python",
                "tags": ["csv"],
            }
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["suggested_action"], "materialize_by_name")
        self.assertEqual(result["candidates"][0]["name"], "invoice_export_csv")
        self.assertGreaterEqual(result["candidates"][0]["reuse_count"], 1)
        self.assertNotIn("code", json.dumps(result))

    def test_recommend_reuse_needs_context_when_name_spans_contexts(self) -> None:
        first_context = {"user": "team", "profile": "acme", "knowledge": "portal", "module": "auth"}
        second_context = {"user": "team", "profile": "globex", "knowledge": "portal", "module": "auth"}
        first = self.propose_in_context(first_context, "portal_login", "login to a web portal")
        second = self.propose_in_context(second_context, "portal_login", "login to a different web portal")
        self.store.approve_function({"revision_id": first["revision_id"], "approved_by": "u"})
        self.store.approve_function({"revision_id": second["revision_id"], "approved_by": "u"})
        result = self.store.recommend_reuse({"task": "Use portal_login", "desired_functions": ["portal_login"]})
        self.assertEqual(result["status"], "needs_context")
        self.assertEqual(result["suggested_action"], "ask_user")
        self.assertEqual(len(result["candidates"]), 2)

    def test_recommend_reuse_not_found(self) -> None:
        result = self.store.recommend_reuse({"task": "Create an unrelated geospatial tiling pipeline"})
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["suggested_action"], "propose_new")

    def test_recommend_reuse_prefers_exact_context(self) -> None:
        wanted_context = {"user": "team", "profile": "alpha", "knowledge": "etl", "module": "import"}
        other_context = {"user": "team", "profile": "beta", "knowledge": "etl", "module": "import"}
        wanted = self.propose_in_context(wanted_context, "customer_csv_import", "import customer csv rows")
        other = self.propose_in_context(other_context, "customer_csv_import", "import customer csv rows with a newer variant")
        self.store.approve_function({"revision_id": other["revision_id"], "approved_by": "u"})
        self.store.approve_function({"revision_id": wanted["revision_id"], "approved_by": "u"})
        result = self.store.recommend_reuse(
            {
                "task": "Create customer_csv_import",
                "context": wanted_context,
                "desired_functions": ["customer_csv_import"],
            }
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["candidates"][0]["context"]["profile"], "alpha")

    def test_search_uses_fts_when_available_and_fallback_when_disabled(self) -> None:
        proposal = self.propose_in_context(
            self.context(),
            "report_pdf_builder",
            "build monthly compliance report pdf",
            tags=["reports", "pdf"],
        )
        self.store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        result = self.store.search_functions({"query": "compliance pdf", "detail": True})
        self.assertEqual(result["count"], 1)
        if self.store._fts_supported():
            self.assertIn("fts_match", result["functions"][0]["score_reasons"])
        old = os.environ.get("ARCHSMITH_DISABLE_FTS")
        os.environ["ARCHSMITH_DISABLE_FTS"] = "1"
        try:
            fallback = self.store.search_functions({"query": "compliance pdf", "detail": True})
        finally:
            if old is None:
                os.environ.pop("ARCHSMITH_DISABLE_FTS", None)
            else:
                os.environ["ARCHSMITH_DISABLE_FTS"] = old
        self.assertEqual(fallback["count"], 1)
        self.assertNotIn('"code":', json.dumps(fallback))

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
        with self.assertRaises(ValidationError) as raised:
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
        self.assertEqual(raised.exception.error_code, "DIFF_TOO_LARGE")

    def test_plan_project_reports_blocked_items_without_writing(self) -> None:
        proposal = self.propose(
            "def planned_fn():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n",
            name="planned_fn",
        )
        self.store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        out_dir = self.root / "planned"
        result = self.store.plan_project(
            {
                "destination_path": str(out_dir),
                "functions": [
                    {
                        "name": "planned_fn",
                        "filename": "planned_fn.py",
                        "replacements": {
                            "def planned_fn():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n": "def planned_fn():\n    return 99\n"
                        },
                    }
                ],
            }
        )
        self.assertFalse(result["can_materialize"])
        self.assertEqual(result["blocked"][0]["revision_id"], proposal["revision_id"])
        self.assertIn("token_estimate", result)
        self.assertFalse((out_dir / "planned_fn.py").exists())

    def test_user_company_project_flow_with_token_estimate(self) -> None:
        context = {"user": "User X", "profile": "Company X", "knowledge": "Project 123", "module": "automation"}
        self.store.upsert_context(context)
        first = self.propose_in_context(
            context,
            "load_project_config",
            "load project configuration from approved environment names",
            code="\n".join(
                [
                    'PROJECT_LABEL = "default"',
                    "def load_project_config():",
                    "    config = {}",
                    "    config['label'] = PROJECT_LABEL",
                    "    config['enabled'] = True",
                    "    config['mode'] = 'standard'",
                    "    config['retries'] = 3",
                    "    config['timeout'] = 30",
                    "    config['format'] = 'json'",
                    "    return config",
                    "",
                ]
            ),
            tags=["config"],
        )
        second = self.propose_in_context(
            context,
            "write_status_report",
            "write a compact status report artifact",
            code="def write_status_report(config):\n    return f\"status:{config['label']}\"\n",
            tags=["reporting"],
        )
        self.store.approve_function({"revision_id": first["revision_id"], "approved_by": "User X"})
        self.store.approve_function({"revision_id": second["revision_id"], "approved_by": "User X"})
        out_dir = self.root / "company-project"
        plan = self.store.plan_project(
            {
                "destination_path": str(out_dir),
                "context": context,
                "functions": [
                    {
                        "name": "load_project_config",
                        "filename": "config.py",
                        "replacements": {'PROJECT_LABEL = "default"': 'PROJECT_LABEL = "tenant-a"'},
                    },
                    {"name": "write_status_report", "filename": "report.py"},
                ],
            }
        )
        self.assertTrue(plan["can_materialize"])
        self.assertEqual(plan["count"], 2)
        self.assertGreater(plan["token_estimate"]["without_archsmith"]["total"], plan["token_estimate"]["with_archsmith"]["total"])
        self.assertNotIn('"code":', json.dumps(plan))
        materialized = self.store.materialize_project(
            {
                "destination_path": str(out_dir),
                "context": context,
                "confirm_write": True,
                "record_reuse": False,
                "functions": [
                    {
                        "name": "load_project_config",
                        "filename": "config.py",
                        "replacements": {'PROJECT_LABEL = "default"': 'PROJECT_LABEL = "tenant-a"'},
                    },
                    {"name": "write_status_report", "filename": "report.py"},
                ],
            }
        )
        self.assertEqual(materialized["count"], 2)
        self.assertTrue((out_dir / "config.py").exists())
        self.assertTrue((out_dir / "report.py").exists())

    def test_materialize_allowed_roots_and_dry_run(self) -> None:
        proposal = self.propose("def guarded_fn():\n    return 1\n", name="guarded_fn")
        self.store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
        allowed = self.root / "allowed"
        blocked = self.root / "blocked"
        old = os.environ.get("ARCHSMITH_ALLOWED_ROOTS")
        os.environ["ARCHSMITH_ALLOWED_ROOTS"] = str(allowed)
        try:
            dry_run = self.store.materialize_by_name(
                {
                    "name": "guarded_fn",
                    "destination_path": str(allowed),
                    "dry_run": True,
                    "record_reuse": True,
                }
            )
            self.assertTrue(dry_run["dry_run"])
            self.assertFalse(Path(dry_run["path"]).exists())
            written = self.store.materialize_by_name(
                {
                    "name": "guarded_fn",
                    "destination_path": str(allowed),
                    "confirm_write": True,
                    "record_reuse": False,
                }
            )
            self.assertTrue(Path(written["path"]).exists())
            with self.assertRaises(ValidationError) as raised:
                self.store.materialize_by_name(
                    {
                        "name": "guarded_fn",
                        "destination_path": str(blocked),
                        "confirm_write": True,
                        "record_reuse": False,
                    }
                )
            self.assertEqual(raised.exception.error_code, "PATH_BLOCKED")
        finally:
            if old is None:
                os.environ.pop("ARCHSMITH_ALLOWED_ROOTS", None)
            else:
                os.environ["ARCHSMITH_ALLOWED_ROOTS"] = old

    def test_secret_like_assignment_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.propose("to" + "ken = 'abcdefghi'\n")

    def test_configurable_mutation_threshold(self) -> None:
        old = os.environ.get("ARCHSMITH_MUTATION_THRESHOLD")
        os.environ["ARCHSMITH_MUTATION_THRESHOLD"] = "0.50"
        try:
            # 1. Propose base version
            first = self.propose("def fn():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n")
            self.store.approve_function({"revision_id": first["revision_id"], "approved_by": "u"})
            
            # 2. Propose revision with ~40% mutation
            second = self.propose("def fn():\n    a = 10\n    b = 20\n    c = 3\n    return a + b + c\n")
            
            # Since threshold is 0.50 (50%), it should not require a new public version candidate
            self.assertEqual(second["public_version"], 1)
            self.assertFalse(second["requires_new_version"])
            
            # 3. Change threshold to 0.10 (10%)
            os.environ["ARCHSMITH_MUTATION_THRESHOLD"] = "0.10"
            third = self.propose("def fn():\n    a = 1\n    b = 2\n    c = 100\n    return a + b + c\n")
            
            # It should require a new version because mutation exceeds 10%
            self.assertEqual(third["public_version"], 2)
            self.assertTrue(third["requires_new_version"])
        finally:
            if old is None:
                os.environ.pop("ARCHSMITH_MUTATION_THRESHOLD", None)
            else:
                os.environ["ARCHSMITH_MUTATION_THRESHOLD"] = old

    def test_database_migration_v1_to_v2(self) -> None:
        # Create a clean temp folder and set up a raw SQLite db representing v1 schema (no schema_version, no fts)
        temp_root = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        db_path = Path(temp_root.name) / "archsmith.sqlite3"
        
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE contexts (
                id INTEGER PRIMARY KEY,
                user_slug TEXT NOT NULL, user_name TEXT NOT NULL,
                profile_slug TEXT NOT NULL, profile_name TEXT NOT NULL,
                knowledge_slug TEXT NOT NULL, knowledge_name TEXT NOT NULL,
                module_slug TEXT NOT NULL, module_name TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(user_slug, profile_slug, knowledge_slug, module_slug)
            );
            CREATE TABLE functions (
                id INTEGER PRIMARY KEY,
                context_id INTEGER NOT NULL REFERENCES contexts(id) ON DELETE CASCADE,
                slug TEXT NOT NULL, name TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(context_id, slug)
            );
            CREATE TABLE revisions (
                id INTEGER PRIMARY KEY,
                function_id INTEGER NOT NULL REFERENCES functions(id) ON DELETE CASCADE,
                public_version INTEGER NOT NULL, revision INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('draft', 'approved', 'deprecated')),
                summary TEXT NOT NULL, language TEXT NOT NULL,
                runtime TEXT, signature TEXT, inputs_json TEXT, outputs_json TEXT,
                dependencies_json TEXT, environment_json TEXT, side_effects TEXT,
                usage_notes TEXT, limitations TEXT, tags_json TEXT,
                code_hash TEXT NOT NULL, code_path TEXT NOT NULL,
                normalized_line_count INTEGER NOT NULL, diff_ratio REAL NOT NULL,
                base_revision_id INTEGER REFERENCES revisions(id),
                created_by TEXT, approved_by TEXT, created_at TEXT NOT NULL, approved_at TEXT,
                metadata_json TEXT,
                UNIQUE(function_id, public_version, revision)
            );
            INSERT INTO contexts VALUES (1, 'u', 'u', 'p', 'p', 'k', 'k', 'm', 'm', 'now', 'now');
            INSERT INTO functions VALUES (1, 1, 'my-func', 'my_func', 'now', 'now');
            INSERT INTO revisions VALUES (1, 1, 1, 1, 'approved', 'migrated summary', 'python',
                NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, '[]',
                'hash', 'code/1.py', 5, 1.0, NULL, 'u', 'u', 'now', 'now', '{}');
            """
        )
        conn.close()
        
        # Instantiate store - this should trigger migration from v1 to v2!
        store = ArchSmithStore(Path(temp_root.name))
        
        # Verify schema_version table exists and version is 2
        ver_row = store.conn.execute("SELECT version FROM schema_version").fetchone()
        self.assertIsNotNone(ver_row)
        self.assertEqual(ver_row["version"], 2)
        
        # Verify FTS virtual table exists and the migrated revision was indexed
        if store._fts_supported():
            fts_row = store.conn.execute("SELECT search_text FROM revisions_fts WHERE rowid = 1").fetchone()
            self.assertIsNotNone(fts_row)
            self.assertIn("migrated summary", fts_row["search_text"])
            self.assertIn("my_func", fts_row["search_text"])
            
        store.close()
        temp_root.cleanup()


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
        self.assertIn("archsmith_recommend_reuse", tool_names)
        self.assertIn("archsmith_materialize_by_name", tool_names)
        self.assertIn("archsmith_plan_project", tool_names)
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

    def test_prompts_and_resources_over_stdio(self) -> None:
        env = os.environ.copy()
        env["ARCHSMITH_HOME"] = self.temp.name
        store = ArchSmithStore(Path(self.temp.name))
        try:
            proposal = store.propose_function(
                {
                    "context": {"user": "u", "profile": "p", "knowledge": "k", "module": "m"},
                    "name": "resource_fn",
                    "summary": "metadata only function",
                    "language": "python",
                    "code": "def resource_fn():\n    return 1\n",
                    "signature": "resource_fn()",
                    "tags": ["metadata"],
                }
            )
            store.approve_function({"revision_id": proposal["revision_id"], "approved_by": "u"})
            function_id = int(proposal["function_id"])
        finally:
            store.close()
        server = ROOT / "mcp" / "server.py"
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "prompts/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "prompts/get",
                "params": {"name": "recommend_reuse_for_task", "arguments": {"task": "build metadata only"}},
            },
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {}},
            {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": "archsmith://contexts"}},
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "resources/read",
                "params": {"uri": "archsmith://functions/approved"},
            },
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "resources/read",
                "params": {"uri": f"archsmith://functions/{function_id}/metadata"},
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
        prompt_names = {prompt["name"] for prompt in responses[1]["result"]["prompts"]}
        self.assertIn("recommend_reuse_for_task", prompt_names)
        self.assertIn("archsmith_recommend_reuse", responses[2]["result"]["messages"][0]["content"]["text"])
        resource_uris = {resource["uri"] for resource in responses[3]["result"]["resources"]}
        self.assertIn("archsmith://contexts", resource_uris)
        self.assertIn(f"archsmith://functions/{function_id}/metadata", resource_uris)
        for response in responses[4:]:
            text = response["result"]["contents"][0]["text"]
            self.assertNotIn("def resource_fn", text)
            self.assertNotIn('"code"', text)

    def test_structured_error_over_stdio(self) -> None:
        env = os.environ.copy()
        env["ARCHSMITH_HOME"] = self.temp.name
        server = ROOT / "mcp" / "server.py"
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "archsmith_materialize_by_name",
                    "arguments": {
                        "name": "missing_fn",
                        "destination_path": str(Path(self.temp.name) / "out"),
                        "confirm_write": True,
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
        result = responses[1]["result"]
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["error_code"], "NOT_FOUND")


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
