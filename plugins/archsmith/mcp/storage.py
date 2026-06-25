from __future__ import annotations

import difflib
import hashlib
import json
import ast
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def get_mutation_threshold() -> float:
    raw = os.environ.get("ARCHSMITH_MUTATION_THRESHOLD")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return 0.20

DB_FILE = "archsmith.sqlite3"


class ArchSmithError(Exception):
    error_code = "ARCHSMITH_ERROR"

    def __init__(
        self,
        message: str,
        error_code: str | None = None,
        retry_with: dict[str, Any] | None = None,
        candidates: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if error_code:
            self.error_code = error_code
        self.retry_with = retry_with
        self.candidates = candidates or []
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error_code": self.error_code,
            "message": self.message,
        }
        if self.retry_with:
            payload["retry_with"] = self.retry_with
        if self.candidates:
            payload["candidates"] = self.candidates
        if self.details:
            payload["details"] = self.details
        return payload


class ValidationError(ArchSmithError):
    error_code = "VALIDATION_ERROR"


class NotFoundError(ArchSmithError):
    error_code = "NOT_FOUND"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_home() -> Path:
    configured = os.environ.get("ARCHSMITH_HOME")
    if configured:
        return Path(configured).expanduser()
    home = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(home) / ".codex" / "archsmith"


def slugify(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required")
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    if not normalized:
        raise ValidationError(f"{field} must contain at least one letter or digit")
    return normalized[:96]


def json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else None, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_code_lines(code: str) -> list[str]:
    lines: list[str] = []
    for line in code.splitlines():
        significant = line.rstrip()
        if significant.strip():
            lines.append(significant)
    return lines


def normalized_diff_ratio(previous: str | None, current: str) -> float:
    if previous is None:
        return 1.0
    previous_lines = normalize_code_lines(previous)
    current_lines = normalize_code_lines(current)
    if not previous_lines and not current_lines:
        return 0.0
    if not previous_lines or not current_lines:
        return 1.0
    return round(1.0 - difflib.SequenceMatcher(None, previous_lines, current_lines).ratio(), 6)


def extension_for_language(language: str | None) -> str:
    mapping = {
        "python": "py",
        "py": "py",
        "javascript": "js",
        "js": "js",
        "typescript": "ts",
        "ts": "ts",
        "powershell": "ps1",
        "sql": "sql",
        "shell": "sh",
        "bash": "sh",
    }
    if not language:
        return "txt"
    return mapping.get(language.strip().lower(), "txt")


def validate_plain_filename(filename: str) -> str:
    candidate = filename.strip()
    if not candidate:
        raise ValidationError("filename is required")
    path = Path(candidate)
    if path.is_absolute() or ".." in path.parts or any(sep in candidate for sep in ("/", "\\")):
        raise ValidationError("filename must be a plain file name")
    return candidate


SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie)\b\s*[:=]\s*['\"][^'\"]{4,}['\"]"
)
CONNECTION_STRING = re.compile(r"(?i)\b(server|host|database|uid|user id|pwd|password)\s*=[^;\n]+;")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
APPROX_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|\d+|[^\w\s]|\s+", re.UNICODE)


def reject_secrets(*values: Any) -> None:
    text = "\n".join(json.dumps(value, ensure_ascii=False) for value in values if value is not None)
    if SECRET_ASSIGNMENT.search(text) or CONNECTION_STRING.search(text) or PRIVATE_KEY.search(text):
        raise ValidationError("Secret-like material is not allowed in ArchSmith memory", error_code="SECRET_DETECTED")


def require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be an object")
    return value


def approx_token_count(value: str | None) -> int:
    return len(APPROX_TOKEN_RE.findall(value or ""))


def apply_replacements(code: str, replacements: Any) -> tuple[str, list[dict[str, int]]]:
    if replacements in (None, {}):
        return code, []
    if not isinstance(replacements, dict):
        raise ValidationError("replacements must be an object")
    reject_secrets(replacements)
    result = code
    applied: list[dict[str, int]] = []
    for old, new in replacements.items():
        if not isinstance(old, str) or not old:
            raise ValidationError("replacement keys must be non-empty strings")
        if not isinstance(new, str):
            raise ValidationError("replacement values must be strings")
        count = result.count(old)
        if count == 0:
            raise ValidationError("replacement target was not found")
        result = result.replace(old, new)
        applied.append({"match_chars": len(old), "replacement_chars": len(new), "count": count})
    return result, applied


def are_imports_equal(node1: Any, node2: Any) -> bool:
    if type(node1) != type(node2):
        return False
    if isinstance(node1, ast.Import):
        names1 = sorted([alias.name + (" as " + alias.asname if alias.asname else "") for alias in node1.names])
        names2 = sorted([alias.name + (" as " + alias.asname if alias.asname else "") for alias in node2.names])
        return names1 == names2
    if isinstance(node1, ast.ImportFrom):
        if node1.module != node2.module or node1.level != node2.level:
            return False
        names1 = sorted([alias.name + (" as " + alias.asname if alias.asname else "") for alias in node1.names])
        names2 = sorted([alias.name + (" as " + alias.asname if alias.asname else "") for alias in node2.names])
        return names1 == names2
    return False


def get_node_name(node: Any) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    return None


def merge_python_code(existing_code: str, new_code: str) -> str:
    try:
        existing_tree = ast.parse(existing_code)
        new_tree = ast.parse(new_code)
    except Exception:
        return new_code  # Fallback to overwrite on parsing errors
        
    definitions_to_merge = []
    imports_to_merge = []
    for node in new_tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports_to_merge.append(node)
        elif get_node_name(node):
            definitions_to_merge.append(node)
            
    existing_imports = [n for n in existing_tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    
    # 1. Insert imports that don't exist
    inserted_count = 0
    for imp in imports_to_merge:
        duplicate = False
        for ex_imp in existing_imports:
            if are_imports_equal(imp, ex_imp):
                duplicate = True
                break
        if not duplicate:
            existing_tree.body.insert(inserted_count, imp)
            inserted_count += 1
            
    # 2. Merge definitions
    for def_node in definitions_to_merge:
        name = get_node_name(def_node)
        replaced = False
        for idx, node in enumerate(existing_tree.body):
            if get_node_name(node) == name:
                existing_tree.body[idx] = def_node
                replaced = True
                break
        if not replaced:
            existing_tree.body.append(def_node)
            
    if hasattr(ast, "unparse"):
        try:
            return ast.unparse(existing_tree)
        except Exception:
            pass
    return new_code


def detect_missing_dependencies(target_root: Path, declared_deps: list[str]) -> list[str]:
    if not declared_deps:
        return []
    cleaned_deps = []
    for d in declared_deps:
        if isinstance(d, str) and d.strip():
            # Extract package name (ignoring version specifiers)
            pkg = re.split(r'[=<>~!]', d.strip())[0].strip().lower()
            cleaned_deps.append(pkg)
    if not cleaned_deps:
        return []
        
    missing = list(cleaned_deps)
    
    # Walk up target_root up to 3 levels to find requirements.txt or package.json
    curr = target_root
    found_manifests = []
    for _ in range(4):
        req_file = curr / "requirements.txt"
        pkg_file = curr / "package.json"
        if req_file.exists():
            found_manifests.append(req_file)
        if pkg_file.exists():
            found_manifests.append(pkg_file)
        if curr.parent == curr:
            break
        curr = curr.parent
        
    if not found_manifests:
        return missing
        
    for manifest in found_manifests:
        try:
            content = manifest.read_text(encoding="utf-8", errors="ignore")
            if manifest.name == "package.json":
                data = json.loads(content)
                deps = {}
                deps.update(data.get("dependencies") or {})
                deps.update(data.get("devDependencies") or {})
                deps.update(data.get("peerDependencies") or {})
                installed_pkgs = {k.lower() for k in deps.keys()}
            else:
                installed_pkgs = set()
                for line in content.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    pkg = re.split(r'[=<>~!]', line)[0].strip().lower()
                    installed_pkgs.add(pkg)
            
            missing = [m for m in missing if m not in installed_pkgs]
        except Exception:
            continue
            
    return missing



def allowed_roots_from_env() -> list[Path]:
    raw = os.environ.get("ARCHSMITH_ALLOWED_ROOTS")
    if not raw:
        return []
    parts = [part.strip() for part in raw.split(";") if part.strip()]
    roots: list[Path] = []
    for part in parts:
        roots.append(Path(part).expanduser().resolve())
    return roots


def ensure_allowed_path(path: Path) -> None:
    roots = allowed_roots_from_env()
    if not roots:
        return
    resolved = path.expanduser().resolve()
    for root in roots:
        try:
            if os.path.commonpath([str(resolved), str(root)]) == str(root):
                return
        except ValueError:
            continue
    raise ValidationError(
        "target path is outside ARCHSMITH_ALLOWED_ROOTS",
        error_code="PATH_BLOCKED",
        retry_with={"destination_path": str(roots[0]) if roots else None},
        details={"path": str(resolved), "allowed_roots": [str(root) for root in roots]},
    )


class ArchSmithStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_home()).expanduser()
        self.db_path = self.root / DB_FILE
        self.code_root = self.root / "code"
        self.root.mkdir(parents=True, exist_ok=True)
        self.code_root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._fts_supported_cache: bool | None = None
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _index_revision_fts_conn(self, conn: sqlite3.Connection, r: dict[str, Any]) -> None:
        tags_raw = r.get("tags_json")
        tags = []
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw) or []
            except Exception:
                tags = []
        elif isinstance(tags_raw, list):
            tags = tags_raw
        if not isinstance(tags, list):
            tags = []
        tags_str = " ".join(str(t) for t in tags)
        search_text = (
            f"{r.get('function_name') or ''} "
            f"{r.get('summary') or ''} "
            f"{r.get('signature') or ''} "
            f"{r.get('language') or ''} "
            f"{r.get('runtime') or ''} "
            f"{tags_str} "
            f"{r.get('user_name') or ''} "
            f"{r.get('profile_name') or ''} "
            f"{r.get('knowledge_name') or ''} "
            f"{r.get('module_name') or ''}"
        ).lower().strip()
        search_text = re.sub(r"\s+", " ", search_text)
        conn.execute(
            "INSERT OR REPLACE INTO revisions_fts(rowid, search_text) VALUES (?, ?)",
            (r["id"], search_text)
        )

    def _migrate(self) -> None:
        # Create schema_version if not exists
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
            """
        )
        self.conn.commit()
        
        # Check current version
        row = self.conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            # Check if contexts exists (meaning it's an existing v1 database)
            table_exists = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='contexts'"
            ).fetchone()
            if table_exists:
                current_version = 1
                self.conn.execute("INSERT INTO schema_version (version) VALUES (1)")
            else:
                current_version = 2
                self.conn.execute("INSERT INTO schema_version (version) VALUES (2)")
            self.conn.commit()
        else:
            current_version = row["version"]

        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contexts (
                id INTEGER PRIMARY KEY,
                user_slug TEXT NOT NULL,
                user_name TEXT NOT NULL,
                profile_slug TEXT NOT NULL,
                profile_name TEXT NOT NULL,
                knowledge_slug TEXT NOT NULL,
                knowledge_name TEXT NOT NULL,
                module_slug TEXT NOT NULL,
                module_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_slug, profile_slug, knowledge_slug, module_slug)
            );

            CREATE TABLE IF NOT EXISTS functions (
                id INTEGER PRIMARY KEY,
                context_id INTEGER NOT NULL REFERENCES contexts(id) ON DELETE CASCADE,
                slug TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(context_id, slug)
            );

            CREATE TABLE IF NOT EXISTS revisions (
                id INTEGER PRIMARY KEY,
                function_id INTEGER NOT NULL REFERENCES functions(id) ON DELETE CASCADE,
                public_version INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('draft', 'approved', 'deprecated')),
                summary TEXT NOT NULL,
                language TEXT NOT NULL,
                runtime TEXT,
                signature TEXT,
                inputs_json TEXT,
                outputs_json TEXT,
                dependencies_json TEXT,
                environment_json TEXT,
                side_effects TEXT,
                usage_notes TEXT,
                limitations TEXT,
                tags_json TEXT,
                code_hash TEXT NOT NULL,
                code_path TEXT NOT NULL,
                normalized_line_count INTEGER NOT NULL,
                diff_ratio REAL NOT NULL,
                base_revision_id INTEGER REFERENCES revisions(id),
                created_by TEXT,
                approved_by TEXT,
                created_at TEXT NOT NULL,
                approved_at TEXT,
                metadata_json TEXT,
                UNIQUE(function_id, public_version, revision)
            );

            CREATE TABLE IF NOT EXISTS reuse_logs (
                id INTEGER PRIMARY KEY,
                revision_id INTEGER NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
                project_path TEXT,
                client TEXT,
                notes TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

        if self._fts_supported():
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS revisions_fts USING fts5(search_text)")
            self.conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_revisions_delete AFTER DELETE ON revisions
                BEGIN
                    DELETE FROM revisions_fts WHERE rowid = OLD.id;
                END;
                """
            )
            self.conn.commit()

        if current_version < 2:
            if self._fts_supported():
                revisions = self.conn.execute(
                    """
                    SELECT r.id, r.summary, r.language, r.runtime, r.signature, r.tags_json,
                           f.name AS function_name,
                           c.user_name, c.profile_name, c.knowledge_name, c.module_name
                    FROM revisions r
                    JOIN functions f ON f.id = r.function_id
                    JOIN contexts c ON c.id = f.context_id
                    WHERE r.status = 'approved'
                    """
                ).fetchall()
                for r in revisions:
                    self._index_revision_fts_conn(self.conn, dict(r))
            self.conn.execute("UPDATE schema_version SET version = 2")
            self.conn.commit()

    def upsert_context(self, data: dict[str, Any]) -> dict[str, Any]:
        user = str(data.get("user", "")).strip()
        profile = str(data.get("profile", "")).strip()
        knowledge = str(data.get("knowledge", "")).strip()
        module = str(data.get("module") or "").strip()
        user_slug = slugify(user, "user")
        profile_slug = slugify(profile, "profile")
        knowledge_slug = slugify(knowledge, "knowledge")
        module_slug = slugify(module, "module") if module else ""
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO contexts (
                user_slug, user_name, profile_slug, profile_name,
                knowledge_slug, knowledge_name, module_slug, module_name,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_slug, profile_slug, knowledge_slug, module_slug)
            DO UPDATE SET
                user_name = excluded.user_name,
                profile_name = excluded.profile_name,
                knowledge_name = excluded.knowledge_name,
                module_name = excluded.module_name,
                updated_at = excluded.updated_at
            """,
            (
                user_slug,
                user,
                profile_slug,
                profile,
                knowledge_slug,
                knowledge,
                module_slug,
                module,
                now,
                now,
            ),
        )
        self.conn.commit()
        return self._context_by_slugs(user_slug, profile_slug, knowledge_slug, module_slug)

    def list_contexts(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        filters = filters or {}
        where: list[str] = []
        params: list[Any] = []
        for field in ("user", "profile", "knowledge", "module"):
            value = filters.get(field)
            if value:
                slug = slugify(str(value), field)
                where.append(f"{field}_slug = ?")
                params.append(slug)
        sql = "SELECT * FROM contexts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY user_name, profile_name, knowledge_name, module_name"
        rows = [self._context_payload(row) for row in self.conn.execute(sql, params)]
        return {"contexts": rows, "count": len(rows)}

    def _find_similar_approved_function(self, code: str, language: str, exclude_function_id: int | None = None) -> dict[str, Any] | None:
        query = """
            SELECT r.*, f.name as function_name 
            FROM revisions r
            JOIN functions f ON r.function_id = f.id
            WHERE r.status = 'approved' AND r.language = ?
        """
        params = [language]
        if exclude_function_id:
            query += " AND r.function_id != ?"
            params.append(exclude_function_id)
            
        rows = self.conn.execute(query, params).fetchall()
        if not rows:
            return None
            
        normalized_new = "".join(normalize_code_lines(code))
        best_ratio = 0.0
        best_row = None
        for row in rows:
            prev_code = self._read_revision_code(row)
            if not prev_code:
                continue
            normalized_prev = "".join(normalize_code_lines(prev_code))
            ratio = difflib.SequenceMatcher(None, normalized_prev, normalized_new).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_row = row
                
        if best_ratio > 0.85 and best_row:
            return {
                "function_id": best_row["function_id"],
                "revision_id": best_row["id"],
                "name": best_row["function_name"],
                "similarity": round(best_ratio, 3)
            }
        return None

    def propose_function(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name", "")).strip()
        summary = str(data.get("summary", "")).strip()
        language = str(data.get("language", "")).strip()
        code = data.get("code")
        if not name:
            raise ValidationError("name is required")
        if not summary:
            raise ValidationError("summary is required")
        if not language:
            raise ValidationError("language is required")
        if not isinstance(code, str) or not code.strip():
            raise ValidationError("code is required")

        reject_secrets(
            code,
            summary,
            data.get("runtime"),
            data.get("signature"),
            data.get("inputs"),
            data.get("outputs"),
            data.get("dependencies"),
            data.get("environment"),
            data.get("side_effects"),
            data.get("usage_notes"),
            data.get("limitations"),
            data.get("metadata"),
        )

        base_function_id = data.get("base_function_id") or data.get("function_id")
        if base_function_id:
            function = self._function_by_id(int(base_function_id))
            context = self._context_by_id(function["context_id"])
        else:
            context_data = require_dict(data.get("context"), "context")
            context = self.upsert_context(context_data)
            function = self._find_function(context["id"], slugify(name, "name"))

        now = utc_now()
        function_slug = slugify(name, "name")
        if function is None:
            cursor = self.conn.execute(
                """
                INSERT INTO functions (context_id, slug, name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (context["id"], function_slug, name, now, now),
            )
            function_id = int(cursor.lastrowid)
            function = self._function_by_id(function_id)
        else:
            self.conn.execute(
                "UPDATE functions SET name = ?, updated_at = ? WHERE id = ?",
                (name, now, function["id"]),
            )
            function = self._function_by_id(function["id"])

        latest = self._latest_approved_revision(function["id"])
        previous_code = self._read_revision_code(latest) if latest else None
        diff_ratio = normalized_diff_ratio(previous_code, code)
        requires_new_version = bool(latest and diff_ratio > get_mutation_threshold())
        max_public_version = self._max_public_version(function["id"])
        public_version = (max_public_version + 1) if requires_new_version else (latest["public_version"] if latest else 1)
        revision = self._next_revision(function["id"], public_version)
        code_hash = sha256_text(code)
        relative_path = self._write_code(function["id"], function_slug, public_version, revision, language, code)
        cursor = self.conn.execute(
            """
            INSERT INTO revisions (
                function_id, public_version, revision, status, summary, language,
                runtime, signature, inputs_json, outputs_json, dependencies_json,
                environment_json, side_effects, usage_notes, limitations, tags_json,
                code_hash, code_path, normalized_line_count, diff_ratio,
                base_revision_id, created_by, created_at, metadata_json
            ) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                function["id"],
                public_version,
                revision,
                summary,
                language,
                data.get("runtime"),
                data.get("signature"),
                json_dumps(data.get("inputs")),
                json_dumps(data.get("outputs")),
                json_dumps(data.get("dependencies")),
                json_dumps(data.get("environment")),
                data.get("side_effects"),
                data.get("usage_notes"),
                data.get("limitations"),
                json_dumps(data.get("tags")),
                code_hash,
                relative_path,
                len(normalize_code_lines(code)),
                diff_ratio,
                data.get("base_revision_id") or (latest["id"] if latest else None),
                data.get("proposed_by"),
                now,
                json_dumps(data.get("metadata")),
            ),
        )
        self.conn.commit()
        revision_row = self._revision_by_id(int(cursor.lastrowid))
        similar = self._find_similar_approved_function(code, language, exclude_function_id=function["id"])
        return {
            "function_id": function["id"],
            "revision_id": revision_row["id"],
            "status": revision_row["status"],
            "public_version": revision_row["public_version"],
            "revision": revision_row["revision"],
            "diff_ratio": diff_ratio,
            "mutation_threshold": get_mutation_threshold(),
            "requires_new_version": requires_new_version,
            "requires_explicit_approval": True,
            "similar_approved_function": similar,
        }

    def approve_function(self, data: dict[str, Any]) -> dict[str, Any]:
        revision_id = int(data.get("revision_id") or 0)
        if revision_id <= 0:
            raise ValidationError("revision_id is required")
        approved_by = str(data.get("approved_by", "")).strip()
        if not approved_by:
            raise ValidationError("approved_by is required")
        revision = self._revision_by_id(revision_id)
        if revision["status"] == "deprecated":
            raise ValidationError("deprecated revisions cannot be approved")
        now = utc_now()
        self.conn.execute(
            "UPDATE revisions SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ?",
            (approved_by, now, revision_id),
        )
        self.conn.commit()
        
        if self._fts_supported():
            r = self.conn.execute(
                """
                SELECT r.id, r.summary, r.language, r.runtime, r.signature, r.tags_json,
                       f.name AS function_name,
                       c.user_name, c.profile_name, c.knowledge_name, c.module_name
                FROM revisions r
                JOIN functions f ON f.id = r.function_id
                JOIN contexts c ON c.id = f.context_id
                WHERE r.id = ?
                """,
                (revision_id,)
            ).fetchone()
            if r:
                self._index_revision_fts_conn(self.conn, dict(r))
                self.conn.commit()

        updated = self._revision_by_id(revision_id)
        return self._revision_payload(updated, include_code=False)

    def search_functions(self, data: dict[str, Any]) -> dict[str, Any]:
        query = str(data.get("query") or "").strip().lower()
        terms = [term for term in re.split(r"\W+", query) if term]
        tags_filter = {str(tag).lower() for tag in (data.get("tags") or [])}
        language = str(data.get("language") or "").strip().lower()
        limit = int(data.get("limit") or 10)
        limit = max(1, min(limit, 50))
        detail = bool(data.get("detail"))

        # Filter: only latest approved revision per function
        where = ["r.id IN (SELECT MAX(id) FROM revisions WHERE status = 'approved' GROUP BY function_id)"]
        params: list[Any] = []

        context_filter = data.get("context") if isinstance(data.get("context"), dict) else data
        for field in ("user", "profile", "knowledge", "module"):
            value = context_filter.get(field) if isinstance(context_filter, dict) else None
            if value:
                where.append(f"c.{field}_slug = ?")
                params.append(slugify(str(value), field))
        if language:
            where.append("lower(r.language) = ?")
            params.append(language)
        for tag in tags_filter:
            where.append("r.tags_json LIKE ?")
            params.append(f'%"{tag}"%')

        use_fts = False
        if terms and self._fts_supported():
            safe_terms = [term for term in terms if re.match(r"^[A-Za-z0-9_]+$", term)]
            if safe_terms:
                use_fts = True
                fts_query = " OR ".join(f"{term}*" for term in safe_terms)
                where.append("fts.search_text MATCH ?")
                params.append(fts_query)

        if use_fts:
            sql = f"""
                SELECT r.*, f.name AS function_name, f.slug AS function_slug,
                       c.user_name, c.profile_name, c.knowledge_name, c.module_name,
                       c.user_slug, c.profile_slug, c.knowledge_slug, c.module_slug,
                       fts.rank
                FROM revisions r
                JOIN functions f ON f.id = r.function_id
                JOIN contexts c ON c.id = f.context_id
                JOIN revisions_fts fts ON fts.rowid = r.id
                WHERE {' AND '.join(where)}
                ORDER BY fts.rank ASC, r.approved_at DESC, r.id DESC
                LIMIT ?
            """
        else:
            if terms:
                for term in terms:
                    where.append("(lower(f.name) LIKE ? OR lower(r.summary) LIKE ?)")
                    params.extend([f"%{term}%", f"%{term}%"])
            sql = f"""
                SELECT r.*, f.name AS function_name, f.slug AS function_slug,
                       c.user_name, c.profile_name, c.knowledge_name, c.module_name,
                       c.user_slug, c.profile_slug, c.knowledge_slug, c.module_slug
                FROM revisions r
                JOIN functions f ON f.id = r.function_id
                JOIN contexts c ON c.id = f.context_id
                WHERE {' AND '.join(where)}
                ORDER BY r.approved_at DESC, r.id DESC
                LIMIT ?
            """
        params.append(limit)
        rows = list(self.conn.execute(sql, params))

        results: list[dict[str, Any]] = []
        for row in rows:
            payload = self._revision_payload(row, include_code=False)
            searchable = " ".join(
                str(part or "")
                for part in (
                    payload["name"],
                    payload["summary"],
                    payload.get("language"),
                    payload.get("runtime"),
                    payload.get("signature"),
                    " ".join(payload.get("tags") or []),
                    payload["context"]["user"],
                    payload["context"]["profile"],
                    payload["context"]["knowledge"],
                    payload["context"]["module"],
                )
            ).lower()
            reasons: list[str] = []
            if terms:
                total_weight = 0.0
                for term in terms:
                    if term in payload["name"].lower():
                        total_weight += 3.0
                    elif payload.get("signature") and term in payload["signature"].lower():
                        total_weight += 2.0
                    elif term in payload["summary"].lower():
                        total_weight += 1.5
                    elif term in searchable:
                        total_weight += 1.0
                max_possible = len(terms) * 3.0
                score = total_weight / max_possible if max_possible > 0 else 0.0
                if total_weight > 0:
                    reasons.append("weighted_term_match")
                if use_fts:
                    score = min(1.0, score + 0.20)
                    reasons.append("fts_match")
            else:
                score = 1.0
                reasons.append("no_query")
            tags = {str(tag).lower() for tag in payload.get("tags") or []}
            if tags_filter:
                tag_score = len(tags_filter.intersection(tags)) / len(tags_filter)
                score = min(1.0, score + (0.15 * tag_score))
                reasons.append("tag_filter")
            if language:
                reasons.append("language_filter")
            payload["score"] = round(score, 6)
            payload["score_reasons"] = reasons
            results.append(payload if detail else self._compact_revision_payload(payload))
        results.sort(key=lambda item: (item["score"], item.get("approved_at") or ""), reverse=True)
        return {"functions": results, "count": len(results)}

    def recommend_reuse(self, data: dict[str, Any]) -> dict[str, Any]:
        task = str(data.get("task") or "").strip()
        if not task:
            raise ValidationError("task is required")
        limit = max(1, min(int(data.get("limit") or 5), 25))
        desired = [str(value).strip() for value in (data.get("desired_functions") or []) if str(value).strip()]
        query = " ".join([task, *desired])
        search = self.search_functions(
            {
                "context": data.get("context") if isinstance(data.get("context"), dict) else data,
                "query": query,
                "language": data.get("language"),
                "tags": data.get("tags") or [],
                "limit": max(limit * 3, 10),
                "detail": True,
            }
        )
        task_terms = {term for term in re.split(r"\W+", query.lower()) if term}
        desired_slugs = {slugify(value, "desired_functions") for value in desired} if desired else set()
        context_filter = data.get("context") if isinstance(data.get("context"), dict) else data
        scored: list[dict[str, Any]] = []
        for payload in search["functions"]:
            reasons: list[str] = list(payload.get("score_reasons") or [])
            score = 0.0
            name_slug = slugify(payload["name"], "name")
            name_terms = {term for term in re.split(r"\W+", payload["name"].lower()) if term}
            metadata_terms = {
                term
                for term in re.split(
                    r"\W+",
                    self._metadata_search_text(payload),
                )
                if term
            }
            if name_slug in desired_slugs or payload["name"].lower() in query.lower() or name_slug in query.lower().replace("_", "-"):
                score += 0.45
                reasons.append("exact_name")
            elif task_terms.intersection(name_terms):
                score += 0.24
                reasons.append("name_terms")
            if task_terms:
                overlap = len(task_terms.intersection(metadata_terms)) / len(task_terms)
                if overlap:
                    score += min(0.25, 0.25 * overlap)
                    reasons.append("metadata_terms")
            context_score = self._context_match_score(payload["context"], context_filter)
            if context_score:
                score += 0.18 * context_score
                reasons.append("context_match")
            if data.get("language") and str(data.get("language")).lower() == str(payload.get("language")).lower():
                score += 0.08
                reasons.append("language_match")
            requested_tags = {str(tag).lower() for tag in (data.get("tags") or [])}
            payload_tags = {str(tag).lower() for tag in (payload.get("tags") or [])}
            if requested_tags:
                tag_overlap = len(requested_tags.intersection(payload_tags)) / len(requested_tags)
                if tag_overlap:
                    score += 0.12 * tag_overlap
                    reasons.append("tags_match")
            stats = self._reuse_stats(int(payload["revision_id"]))
            if stats["reuse_count"]:
                score += min(0.05, stats["reuse_count"] * 0.01)
                reasons.append("reuse_history")
            if payload.get("approved_at"):
                score += 0.03
                reasons.append("approved")
            candidate = {
                "name": payload["name"],
                "function_id": payload["function_id"],
                "revision_id": payload["revision_id"],
                "score": round(min(score, 1.0), 6),
                "reasons": sorted(set(reasons)),
                "context": payload["context"],
                "signature": payload.get("signature"),
                "tags": payload.get("tags") or [],
                "public_version": payload["public_version"],
                "reuse_count": stats["reuse_count"],
                "last_reused_at": stats["last_reused_at"],
            }
            if candidate["score"] > 0:
                scored.append(candidate)
        scored.sort(key=lambda item: (item["score"], item["reuse_count"], item["revision_id"]), reverse=True)
        candidates = scored[:limit]
        if not candidates:
            return {"status": "not_found", "candidates": [], "suggested_action": "propose_new"}

        top = candidates[0]
        second_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
        has_context_filter = any((context_filter.get(field) if isinstance(context_filter, dict) else None) for field in ("user", "profile", "knowledge", "module"))
        distinct_contexts = {
            (
                item["context"].get("user"),
                item["context"].get("profile"),
                item["context"].get("knowledge"),
                item["context"].get("module"),
            )
            for item in candidates
        }
        if not has_context_filter and len(distinct_contexts) > 1 and top["score"] < 0.90:
            status = "needs_context"
            suggested_action = "ask_user"
        elif top["score"] >= 0.75 and top["score"] - second_score >= 0.15:
            status = "ready"
            suggested_action = "materialize_project" if len(desired) > 1 else "materialize_by_name"
        elif len(candidates) > 1:
            status = "ambiguous"
            suggested_action = "ask_user"
        else:
            status = "not_found" if top["score"] < 0.35 else "ambiguous"
            suggested_action = "propose_new" if status == "not_found" else "search_more"
        return {"status": status, "candidates": candidates, "suggested_action": suggested_action}

    def get_function(self, data: dict[str, Any]) -> dict[str, Any]:
        include_code = bool(data.get("include_code"))
        revision = self._select_revision(data, approved_only=False)
        return self._revision_payload(revision, include_code=include_code)

    def materialize_function(self, data: dict[str, Any]) -> dict[str, Any]:
        dry_run = bool(data.get("dry_run"))
        if not data.get("confirm_write") and not dry_run:
            raise ValidationError("confirm_write must be true", error_code="APPROVAL_REQUIRED")
        revision = self._select_revision(data, approved_only=True)
        source = self.root / revision["code_path"]
        if not source.exists():
            raise NotFoundError("stored code file was not found")
        destination_value = data.get("destination_path")
        if not isinstance(destination_value, str) or not destination_value.strip():
            raise ValidationError("destination_path is required")
        destination = Path(destination_value).expanduser()
        filename = data.get("filename")
        if filename:
            filename = validate_plain_filename(str(filename))
            target_dir = destination
            target = target_dir / filename
        elif destination.suffix:
            target = destination
            target_dir = destination.parent
        else:
            ext = extension_for_language(revision["language"])
            target_dir = destination
            target = target_dir / f"{self._function_by_id(revision['function_id'])['slug']}.{ext}"

        target_dir = target_dir.resolve()
        target = target.resolve()
        try:
            common = os.path.commonpath([str(target), str(target_dir)])
        except ValueError as exc:
            raise ValidationError("target path is outside destination", error_code="PATH_BLOCKED") from exc
        if common != str(target_dir):
            raise ValidationError("target path is outside destination", error_code="PATH_BLOCKED")
        ensure_allowed_path(target)
        if target.exists() and not data.get("overwrite"):
            if not dry_run:
                raise ValidationError("target exists and overwrite is false", error_code="TARGET_EXISTS")
        payload = {
            "revision_id": revision["id"],
            "function_id": revision["function_id"],
            "path": str(target),
        }
        if dry_run:
            payload["dry_run"] = True
            payload["would_write"] = str(target)
            payload["target_exists"] = target.exists()
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
            final_code = source.read_text(encoding="utf-8")
            if data.get("merge", True) and target.exists() and target.suffix == ".py":
                try:
                    existing_content = target.read_text(encoding="utf-8")
                    final_code = merge_python_code(existing_content, final_code)
                except Exception:
                    pass
            target.write_text(final_code, encoding="utf-8")
        if data.get("include_hash"):
            payload["code_hash"] = revision["code_hash"]
        return payload

    def materialize_by_name(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name") or data.get("function_name") or "").strip()
        if not name:
            raise ValidationError("name is required")
        revision = self._select_approved_revision_by_name(data, name)
        materialized = self.materialize_function(
            {
                "revision_id": revision["id"],
                "destination_path": data.get("destination_path"),
                "filename": data.get("filename"),
                "overwrite": data.get("overwrite"),
                "confirm_write": data.get("confirm_write"),
                "dry_run": data.get("dry_run"),
                "merge": data.get("merge"),
            }
        )
        payload = {
            "path": materialized["path"],
            "function_id": revision["function_id"],
            "revision_id": revision["id"],
            "name": revision["function_name"],
            "public_version": revision["public_version"],
            "revision": revision["revision"],
        }
        if materialized.get("dry_run"):
            payload["dry_run"] = True
            payload["would_write"] = materialized.get("would_write")
            payload["target_exists"] = materialized.get("target_exists")
        if data.get("include_hash"):
            payload["code_hash"] = revision["code_hash"]
        if data.get("record_reuse", True) and not materialized.get("dry_run"):
            try:
                reuse = self.record_reuse(
                    {
                        "revision_id": revision["id"],
                        "project_path": data.get("project_path") or data.get("destination_path"),
                        "client": data.get("client") or data.get("used_by"),
                        "notes": data.get("notes"),
                    }
                )
                payload["reuse_log_id"] = reuse["reuse_log_id"]
                payload["reuse_recorded"] = True
            except sqlite3.OperationalError as exc:
                payload["reuse_recorded"] = False
                payload["reuse_error"] = str(exc)
                payload["reuse_retry"] = {
                    "revision_id": revision["id"],
                    "project_path": data.get("project_path") or data.get("destination_path"),
                    "client": data.get("client") or data.get("used_by"),
                }
        return payload

    def materialize_project(self, data: dict[str, Any]) -> dict[str, Any]:
        plans = self._plan_project_materialization(data, include_memory=False)
        if not data.get("confirm_write"):
            raise ValidationError("confirm_write must be true", error_code="APPROVAL_REQUIRED")
        target_root = plans["target_root"]
        blocked = plans["blocked"]
        if blocked:
            raise ValidationError(
                "adaptation exceeds mutation threshold",
                error_code="DIFF_TOO_LARGE",
                candidates=blocked,
                details={"mutation_threshold": get_mutation_threshold()},
            )
        overwrite = bool(data.get("overwrite"))
        for item in plans["items"]:
            target = item["target"]
            if target.exists() and not overwrite:
                raise ValidationError(
                    "target exists and overwrite is false",
                    error_code="TARGET_EXISTS",
                    details={"path": str(target)},
                )
        target_root.mkdir(parents=True, exist_ok=True)
        files: list[dict[str, Any]] = []
        for item in plans["items"]:
            target_path = item["target"]
            final_code = item["code"]
            if data.get("merge", True) and target_path.exists() and target_path.suffix == ".py":
                try:
                    existing_content = target_path.read_text(encoding="utf-8")
                    final_code = merge_python_code(existing_content, item["code"])
                except Exception:
                    pass
            target_path.write_text(final_code, encoding="utf-8", newline="\n")
            file_payload = {
                "name": item["name"],
                "path": str(item["target"]),
                "function_id": item["function_id"],
                "revision_id": item["revision_id"],
                "public_version": item["public_version"],
                "revision": item["revision"],
                "diff_ratio": item["diff_ratio"],
                "replacements": item["replacements"],
                "missing_dependencies": item.get("missing_dependencies", []),
            }
            if data.get("record_reuse", True):
                try:
                    reuse = self.record_reuse(
                        {
                            "revision_id": item["revision_id"],
                            "project_path": str(target_root),
                            "client": data.get("client") or item.get("client"),
                            "notes": item["notes"],
                        }
                    )
                    file_payload["reuse_log_id"] = reuse["reuse_log_id"]
                    file_payload["reuse_recorded"] = True
                except sqlite3.OperationalError as exc:
                    file_payload["reuse_recorded"] = False
                    file_payload["reuse_error"] = str(exc)
                    file_payload["reuse_retry"] = {
                        "revision_id": item["revision_id"],
                        "project_path": str(target_root),
                        "client": data.get("client") or item.get("client"),
                    }
            files.append(file_payload)
            
        all_missing = []
        for item in plans["items"]:
            for dep in item.get("missing_dependencies", []):
                if dep not in all_missing:
                    all_missing.append(dep)
                    
        return {
            "project_path": str(target_root),
            "count": len(files),
            "files": files,
            "missing_dependencies": all_missing,
        }

    def plan_project(self, data: dict[str, Any]) -> dict[str, Any]:
        plans = self._plan_project_materialization(data, include_memory=True)
        estimate = self._estimate_project_from_plans(plans)
        files: list[dict[str, Any]] = []
        for item in plans["items"]:
            files.append(
                {
                    "name": item["name"],
                    "filename": item["filename"],
                    "path": str(item["target"]),
                    "function_id": item["function_id"],
                    "revision_id": item["revision_id"],
                    "public_version": item["public_version"],
                    "revision": item["revision"],
                    "diff_ratio": item["diff_ratio"],
                    "replacements": item["replacements"],
                    "missing_dependencies": item.get("missing_dependencies", []),
                }
            )
        all_missing = []
        for item in plans["items"]:
            for dep in item.get("missing_dependencies", []):
                if dep not in all_missing:
                    all_missing.append(dep)
        return {
            "project_path": str(plans["target_root"]),
            "count": len(files),
            "files": files,
            "blocked": plans["blocked"],
            "can_materialize": not bool(plans["blocked"]),
            "token_estimate": estimate,
            "missing_dependencies": all_missing,
        }

    def record_reuse(self, data: dict[str, Any]) -> dict[str, Any]:
        revision = self._select_revision(data, approved_only=True)
        now = utc_now()
        cursor = self.conn.execute(
            """
            INSERT INTO reuse_logs (revision_id, project_path, client, notes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                revision["id"],
                data.get("project_path"),
                data.get("client") or data.get("used_by"),
                data.get("notes"),
                now,
            ),
        )
        self.conn.commit()
        return {"reuse_log_id": int(cursor.lastrowid), "revision_id": revision["id"], "created_at": now}

    def estimate_savings(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name") or data.get("function_name") or "").strip()
        if not name:
            raise ValidationError("name is required")
        revision = self._select_approved_revision_by_name(data, name)
        payload = self._revision_payload(revision, include_code=False)
        code = self._read_revision_code(revision) or ""
        destination = str(data.get("destination_path") or "<destination>")
        memory_payload = {
            "name": payload["name"],
            "summary": payload["summary"],
            "language": payload["language"],
            "runtime": payload["runtime"],
            "signature": payload["signature"],
            "inputs": payload["inputs"],
            "outputs": payload["outputs"],
            "dependencies": payload["dependencies"],
            "environment": payload["environment"],
            "side_effects": payload["side_effects"],
            "usage_notes": payload["usage_notes"],
            "limitations": payload["limitations"],
            "tags": payload["tags"],
            "context": payload["context"],
            "public_version": payload["public_version"],
            "revision": payload["revision"],
        }
        memory_json = json.dumps(memory_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        direct_call = json.dumps(
            {
                "name": name,
                "destination_path": destination,
                "confirm_write": True,
                "record_reuse": True,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        direct_result = json.dumps(
            {
                "function_id": payload["function_id"],
                "revision_id": payload["revision_id"],
                "path": str(Path(destination) / f"{slugify(payload['name'], 'name')}.{extension_for_language(payload['language'])}"),
                "reuse_recorded": True,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        no_arch_prompt = (
            "Create the requested file from this approved memory, preserving behavior, "
            "dependencies, inputs, outputs, usage notes, and limitations. Do not hardcode secrets.\n"
            + memory_json
        )
        return {
            "method": "approx_regex_tokens",
            "function_id": payload["function_id"],
            "revision_id": payload["revision_id"],
            "name": payload["name"],
            "file": {
                "chars": len(code),
                "lines": code.count("\n") + 1 if code else 0,
                "approx_tokens": approx_token_count(code),
            },
            "memory": {
                "chars": len(memory_json),
                "approx_tokens": approx_token_count(memory_json),
            },
            "with_archsmith": {
                "input": approx_token_count(direct_call),
                "output": approx_token_count(direct_result),
                "total": approx_token_count(direct_call) + approx_token_count(direct_result),
            },
            "without_archsmith": {
                "input_prompt_plus_memory": approx_token_count(no_arch_prompt),
                "output_generated_file": approx_token_count(code),
                "total": approx_token_count(no_arch_prompt) + approx_token_count(code),
            },
        }

    def estimate_project_savings(self, data: dict[str, Any]) -> dict[str, Any]:
        plans = self._plan_project_materialization(data, include_memory=True)
        blocked = plans["blocked"]
        if blocked:
            raise ValidationError(
                "adaptation exceeds mutation threshold",
                error_code="DIFF_TOO_LARGE",
                candidates=blocked,
                details={"mutation_threshold": get_mutation_threshold()},
            )
        return self._estimate_project_from_plans(plans)

    def _estimate_project_from_plans(self, plans: dict[str, Any]) -> dict[str, Any]:
        target_root = plans["target_root"]
        direct_call = json.dumps(
            {
                "destination_path": str(target_root),
                "confirm_write": True,
                "record_reuse": True,
                "functions": [
                    {
                        "name": item["name"],
                        "filename": item["filename"],
                        "replacements": bool(item["replacements"]),
                    }
                    for item in plans["items"]
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        direct_result = json.dumps(
            {
                "project_path": str(target_root),
                "count": len(plans["items"]),
                "files": [
                    {
                        "name": item["name"],
                        "path": str(item["target"]),
                        "revision_id": item["revision_id"],
                        "diff_ratio": item["diff_ratio"],
                    }
                    for item in plans["items"]
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        memory_json = json.dumps(
            [item["memory"] for item in plans["items"]],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        no_arch_prompt = (
            "Create a project using these approved implementation memories. Preserve behavior, "
            "dependencies, inputs, outputs, usage notes, and limitations. Do not hardcode secrets.\n"
            + memory_json
        )
        output_tokens = sum(approx_token_count(item["code"]) for item in plans["items"])
        return {
            "method": "approx_regex_tokens",
            "project_path": str(target_root),
            "function_count": len(plans["items"]),
            "files": [
                {
                    "name": item["name"],
                    "filename": item["filename"],
                    "chars": len(item["code"]),
                    "approx_tokens": approx_token_count(item["code"]),
                    "diff_ratio": item["diff_ratio"],
                    "revision_id": item["revision_id"],
                }
                for item in plans["items"]
            ],
            "memory": {
                "chars": len(memory_json),
                "approx_tokens": approx_token_count(memory_json),
            },
            "with_archsmith": {
                "input": approx_token_count(direct_call),
                "output": approx_token_count(direct_result),
                "total": approx_token_count(direct_call) + approx_token_count(direct_result),
            },
            "without_archsmith": {
                "input_prompt_plus_memory": approx_token_count(no_arch_prompt),
                "output_generated_files": output_tokens,
                "total": approx_token_count(no_arch_prompt) + output_tokens,
            },
        }

    def deprecate_function(self, data: dict[str, Any]) -> dict[str, Any]:
        revision_id = data.get("revision_id")
        function_id = data.get("function_id")
        if revision_id:
            revision = self._revision_by_id(int(revision_id))
            self.conn.execute("UPDATE revisions SET status = 'deprecated' WHERE id = ?", (revision["id"],))
            count = 1
        elif function_id:
            function = self._function_by_id(int(function_id))
            cursor = self.conn.execute(
                "UPDATE revisions SET status = 'deprecated' WHERE function_id = ? AND status = 'approved'",
                (function["id"],),
            )
            count = int(cursor.rowcount)
        else:
            raise ValidationError("revision_id or function_id is required")
        self.conn.commit()
        return {"deprecated": count}

    def _metadata_search_text(self, payload: dict[str, Any]) -> str:
        context = payload.get("context") or {}
        parts = [
            payload.get("name"),
            payload.get("summary"),
            payload.get("signature"),
            payload.get("language"),
            payload.get("runtime"),
            " ".join(str(tag) for tag in (payload.get("tags") or [])),
            context.get("user"),
            context.get("profile"),
            context.get("knowledge"),
            context.get("module"),
        ]
        return " ".join(str(part or "") for part in parts).lower()

    def _fts_supported(self) -> bool:
        if os.environ.get("ARCHSMITH_DISABLE_FTS"):
            return False
        if self._fts_supported_cache is not None:
            return self._fts_supported_cache
        try:
            self.conn.execute("CREATE VIRTUAL TABLE temp.archsmith_fts_probe USING fts5(value)")
            self.conn.execute("DROP TABLE temp.archsmith_fts_probe")
            self._fts_supported_cache = True
        except sqlite3.OperationalError:
            self._fts_supported_cache = False
        return self._fts_supported_cache

    def _fts_match_indexes(self, documents: list[str], terms: list[str]) -> set[int]:
        if not documents or not terms or not self._fts_supported():
            return set()
        safe_terms = [term for term in terms if re.match(r"^[A-Za-z0-9_]+$", term)]
        if not safe_terms:
            return set()
        query = " OR ".join(f"{term}*" for term in safe_terms)
        try:
            self.conn.execute("DROP TABLE IF EXISTS temp.archsmith_search_fts")
            self.conn.execute("CREATE VIRTUAL TABLE temp.archsmith_search_fts USING fts5(value)")
            self.conn.executemany("INSERT INTO temp.archsmith_search_fts(value) VALUES (?)", [(document,) for document in documents])
            rows = self.conn.execute("SELECT rowid FROM temp.archsmith_search_fts WHERE value MATCH ?", (query,)).fetchall()
            return {int(row["rowid"]) - 1 for row in rows}
        except sqlite3.OperationalError:
            return set()
        finally:
            try:
                self.conn.execute("DROP TABLE IF EXISTS temp.archsmith_search_fts")
            except sqlite3.OperationalError:
                pass

    def _context_match_score(self, context: dict[str, Any], context_filter: dict[str, Any] | None) -> float:
        if not isinstance(context_filter, dict):
            return 0.0
        requested = {field: context_filter.get(field) for field in ("user", "profile", "knowledge", "module") if context_filter.get(field)}
        if not requested:
            return 0.0
        matches = 0
        for field, value in requested.items():
            if slugify(str(context.get(field) or ""), field) == slugify(str(value), field):
                matches += 1
        return matches / len(requested)

    def _reuse_stats(self, revision_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT count(*) AS reuse_count, max(created_at) AS last_reused_at FROM reuse_logs WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        return {"reuse_count": int(row["reuse_count"] or 0), "last_reused_at": row["last_reused_at"]}

    def _plan_project_materialization(self, data: dict[str, Any], include_memory: bool) -> dict[str, Any]:
        destination_value = data.get("destination_path")
        if not isinstance(destination_value, str) or not destination_value.strip():
            raise ValidationError("destination_path is required")
        target_root = Path(destination_value).expanduser().resolve()
        if target_root.suffix:
            raise ValidationError("destination_path must be a directory for project materialization")
        ensure_allowed_path(target_root)
        functions = data.get("functions")
        if not isinstance(functions, list) or not functions:
            raise ValidationError("functions must be a non-empty array")
        base_context = data.get("context") if isinstance(data.get("context"), dict) else {}
        base_fields = {
            key: value
            for key, value in {
                "user": data.get("user"),
                "profile": data.get("profile"),
                "knowledge": data.get("knowledge"),
                "module": data.get("module"),
            }.items()
            if value
        }
        items: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for raw_item in functions:
            item = require_dict(raw_item, "functions[]")
            name = str(item.get("name") or item.get("function_name") or "").strip()
            if not name:
                raise ValidationError("function name is required")
            selector: dict[str, Any] = dict(base_context)
            selector.update(base_fields)
            if isinstance(item.get("context"), dict):
                selector.update(item["context"])
            for field in ("user", "profile", "knowledge", "module", "language", "tags", "allow_fuzzy"):
                if item.get(field) is not None:
                    selector[field] = item[field]
            selector["name"] = name
            revision = self._select_approved_revision_by_name(selector, name)
            original_code = self._read_revision_code(revision)
            if original_code is None:
                raise NotFoundError("stored code file was not found")
            code, replacements = apply_replacements(original_code, item.get("replacements"))
            diff_ratio = normalized_diff_ratio(original_code, code)
            if diff_ratio > get_mutation_threshold():
                blocked.append(
                    {
                        "name": revision["function_name"],
                        "revision_id": revision["id"],
                        "diff_ratio": diff_ratio,
                        "mutation_threshold": get_mutation_threshold(),
                    }
                )
            filename = validate_plain_filename(
                str(item.get("filename") or f"{revision['function_slug']}.{extension_for_language(revision['language'])}")
            )
            target = (target_root / filename).resolve()
            try:
                common = os.path.commonpath([str(target), str(target_root)])
            except ValueError as exc:
                raise ValidationError("target path is outside destination", error_code="PATH_BLOCKED") from exc
            else:
                if common != str(target_root):
                    raise ValidationError("target path is outside destination", error_code="PATH_BLOCKED")
            ensure_allowed_path(target)
            memory = None
            if include_memory:
                payload = self._revision_payload(revision, include_code=False)
                memory = {
                    "function_id": payload["function_id"],
                    "revision_id": payload["revision_id"],
                    "name": payload["name"],
                    "summary": payload["summary"],
                    "language": payload["language"],
                    "runtime": payload["runtime"],
                    "signature": payload["signature"],
                    "inputs": payload["inputs"],
                    "outputs": payload["outputs"],
                    "dependencies": payload["dependencies"],
                    "environment": payload["environment"],
                    "side_effects": payload["side_effects"],
                    "usage_notes": payload["usage_notes"],
                    "limitations": payload["limitations"],
                    "tags": payload["tags"],
                    "context": payload["context"],
                    "public_version": payload["public_version"],
                    "revision": payload["revision"],
                    "adaptation": {
                        "has_replacements": bool(replacements),
                        "diff_ratio": diff_ratio,
                        "mutation_threshold": get_mutation_threshold(),
                    },
                }
            notes = str(item.get("notes") or data.get("notes") or "").strip()
            if replacements:
                suffix = f"minor adaptation; replacements={len(replacements)}; diff_ratio={diff_ratio}"
                notes = f"{notes}; {suffix}" if notes else suffix
            deps = json_loads(revision["dependencies_json"]) or []
            missing_deps = detect_missing_dependencies(target_root, deps)
            items.append(
                {
                    "name": revision["function_name"],
                    "filename": filename,
                    "target": target,
                    "code": code,
                    "function_id": revision["function_id"],
                    "revision_id": revision["id"],
                    "public_version": revision["public_version"],
                    "revision": revision["revision"],
                    "diff_ratio": diff_ratio,
                    "replacements": replacements,
                    "notes": notes or None,
                    "client": item.get("client"),
                    "memory": memory,
                    "missing_dependencies": missing_deps,
                }
            )
        return {"target_root": target_root, "items": items, "blocked": blocked}

    def _select_approved_revision_by_name(self, data: dict[str, Any], name: str) -> sqlite3.Row:
        name_slug = slugify(name, "name")
        context_filter = data.get("context") if isinstance(data.get("context"), dict) else data
        where = ["r.status = 'approved'", "(lower(f.name) = ? OR f.slug = ?)"]
        params: list[Any] = [name.lower(), name_slug]
        for field in ("user", "profile", "knowledge", "module"):
            value = context_filter.get(field) if isinstance(context_filter, dict) else None
            if value:
                where.append(f"c.{field}_slug = ?")
                params.append(slugify(str(value), field))
        language = str(data.get("language") or "").strip().lower()
        if language:
            where.append("lower(r.language) = ?")
            params.append(language)
        rows = list(
            self.conn.execute(
                f"""
                SELECT r.*, f.name AS function_name, f.slug AS function_slug,
                       c.user_name, c.profile_name, c.knowledge_name, c.module_name,
                       c.user_slug, c.profile_slug, c.knowledge_slug, c.module_slug
                FROM revisions r
                JOIN functions f ON f.id = r.function_id
                JOIN contexts c ON c.id = f.context_id
                WHERE {' AND '.join(where)}
                ORDER BY r.public_version DESC, r.revision DESC, r.id DESC
                """,
                params,
            )
        )
        tags_filter = {str(tag).lower() for tag in (data.get("tags") or [])}
        if tags_filter:
            rows = [
                row
                for row in rows
                if tags_filter.issubset({str(tag).lower() for tag in (json_loads(row["tags_json"]) or [])})
            ]
        if not rows and data.get("allow_fuzzy"):
            result = self.search_functions(
                {
                    "context": context_filter,
                    "query": name,
                    "language": data.get("language"),
                    "tags": data.get("tags") or [],
                    "limit": 2,
                }
            )
            if result["count"] == 1:
                return self._revision_by_id(int(result["functions"][0]["revision_id"]))
        if not rows:
            raise NotFoundError(
                "approved function was not found",
                retry_with={"name": name, "context": context_filter if isinstance(context_filter, dict) else {}},
            )
        latest_by_function: dict[int, sqlite3.Row] = {}
        for row in rows:
            latest_by_function.setdefault(int(row["function_id"]), row)
        if len(latest_by_function) > 1:
            candidates = [
                {
                    "function_id": row["function_id"],
                    "revision_id": row["id"],
                    "name": row["function_name"],
                    "context": {
                        "user": row["user_name"],
                        "profile": row["profile_name"],
                        "knowledge": row["knowledge_name"],
                        "module": row["module_name"],
                    },
                }
                for row in latest_by_function.values()
            ]
            raise ValidationError(
                "function name is ambiguous",
                error_code="AMBIGUOUS_FUNCTION",
                retry_with={"context": {"profile": "<profile>", "knowledge": "<knowledge>", "module": "<module>"}},
                candidates=candidates,
            )
        return next(iter(latest_by_function.values()))

    def _write_code(self, function_id: int, function_slug: str, public_version: int, revision: int, language: str, code: str) -> str:
        ext = extension_for_language(language)
        relative = Path("code") / f"{function_id}-{function_slug}" / f"v{public_version}" / f"r{revision}" / f"source.{ext}"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8", newline="\n")
        return relative.as_posix()

    def _context_by_slugs(self, user_slug: str, profile_slug: str, knowledge_slug: str, module_slug: str) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT * FROM contexts
            WHERE user_slug = ? AND profile_slug = ? AND knowledge_slug = ? AND module_slug = ?
            """,
            (user_slug, profile_slug, knowledge_slug, module_slug),
        ).fetchone()
        if row is None:
            raise NotFoundError("context not found")
        return self._context_payload(row)

    def _context_by_id(self, context_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM contexts WHERE id = ?", (context_id,)).fetchone()
        if row is None:
            raise NotFoundError("context not found")
        return self._context_payload(row)

    def _context_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "user": row["user_name"],
            "profile": row["profile_name"],
            "knowledge": row["knowledge_name"],
            "module": row["module_name"],
            "slugs": {
                "user": row["user_slug"],
                "profile": row["profile_slug"],
                "knowledge": row["knowledge_slug"],
                "module": row["module_slug"],
            },
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _find_function(self, context_id: int, slug: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM functions WHERE context_id = ? AND slug = ?",
            (context_id, slug),
        ).fetchone()

    def _function_by_id(self, function_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM functions WHERE id = ?", (function_id,)).fetchone()
        if row is None:
            raise NotFoundError("function not found")
        return row

    def _revision_by_id(self, revision_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            """
            SELECT r.*, f.name AS function_name, f.slug AS function_slug,
                   c.user_name, c.profile_name, c.knowledge_name, c.module_name,
                   c.user_slug, c.profile_slug, c.knowledge_slug, c.module_slug
            FROM revisions r
            JOIN functions f ON f.id = r.function_id
            JOIN contexts c ON c.id = f.context_id
            WHERE r.id = ?
            """,
            (revision_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("revision not found")
        return row

    def _latest_approved_revision(self, function_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM revisions
            WHERE function_id = ? AND status = 'approved'
            ORDER BY public_version DESC, revision DESC, id DESC
            LIMIT 1
            """,
            (function_id,),
        ).fetchone()

    def _max_public_version(self, function_id: int) -> int:
        row = self.conn.execute(
            "SELECT max(public_version) AS value FROM revisions WHERE function_id = ?",
            (function_id,),
        ).fetchone()
        return int(row["value"] or 0)

    def _next_revision(self, function_id: int, public_version: int) -> int:
        row = self.conn.execute(
            "SELECT max(revision) AS value FROM revisions WHERE function_id = ? AND public_version = ?",
            (function_id, public_version),
        ).fetchone()
        return int(row["value"] or 0) + 1

    def _read_revision_code(self, revision: sqlite3.Row | dict[str, Any] | None) -> str | None:
        if revision is None:
            return None
        path = self.root / revision["code_path"]
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def approved_functions_metadata(self, limit: int = 200) -> dict[str, Any]:
        rows = self.conn.execute(
            """
            SELECT r.*, f.name AS function_name, f.slug AS function_slug,
                   c.user_name, c.profile_name, c.knowledge_name, c.module_name,
                   c.user_slug, c.profile_slug, c.knowledge_slug, c.module_slug
            FROM revisions r
            JOIN functions f ON f.id = r.function_id
            JOIN contexts c ON c.id = f.context_id
            WHERE r.status = 'approved'
            ORDER BY r.approved_at DESC, r.id DESC
            """
        ).fetchall()
        seen: set[int] = set()
        functions: list[dict[str, Any]] = []
        for row in rows:
            if row["function_id"] in seen:
                continue
            seen.add(row["function_id"])
            payload = self._compact_revision_payload(self._revision_payload(row, include_code=False))
            stats = self._reuse_stats(int(row["id"]))
            payload["reuse_count"] = stats["reuse_count"]
            payload["last_reused_at"] = stats["last_reused_at"]
            functions.append(payload)
            if len(functions) >= limit:
                break
        return {"functions": functions, "count": len(functions)}

    def function_metadata(self, function_id: int) -> dict[str, Any]:
        function = self._function_by_id(function_id)
        row = self.conn.execute(
            """
            SELECT r.*, f.name AS function_name, f.slug AS function_slug,
                   c.user_name, c.profile_name, c.knowledge_name, c.module_name,
                   c.user_slug, c.profile_slug, c.knowledge_slug, c.module_slug
            FROM revisions r
            JOIN functions f ON f.id = r.function_id
            JOIN contexts c ON c.id = f.context_id
            WHERE r.function_id = ? AND r.status = 'approved'
            ORDER BY r.public_version DESC, r.revision DESC, r.id DESC
            LIMIT 1
            """,
            (function["id"],),
        ).fetchone()
        if row is None:
            raise NotFoundError("approved function metadata was not found")
        payload = self._revision_payload(row, include_code=False)
        stats = self._reuse_stats(int(row["id"]))
        payload["reuse_count"] = stats["reuse_count"]
        payload["last_reused_at"] = stats["last_reused_at"]
        return payload

    def _select_revision(self, data: dict[str, Any], approved_only: bool) -> sqlite3.Row:
        revision_id = data.get("revision_id")
        if revision_id:
            revision = self._revision_by_id(int(revision_id))
        else:
            function_id = int(data.get("function_id") or 0)
            if function_id <= 0:
                raise ValidationError("revision_id or function_id is required")
            status_clause = "AND status = 'approved'" if approved_only else ""
            row = self.conn.execute(
                f"""
                SELECT id FROM revisions
                WHERE function_id = ? {status_clause}
                ORDER BY public_version DESC, revision DESC, id DESC
                LIMIT 1
                """,
                (function_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("revision not found")
            revision = self._revision_by_id(int(row["id"]))
        if approved_only and revision["status"] != "approved":
            raise ValidationError("approved revision is required", error_code="APPROVAL_REQUIRED")
        return revision

    def _revision_payload(self, row: sqlite3.Row, include_code: bool) -> dict[str, Any]:
        payload = {
            "function_id": row["function_id"],
            "revision_id": row["id"],
            "name": row["function_name"] if "function_name" in row.keys() else self._function_by_id(row["function_id"])["name"],
            "status": row["status"],
            "public_version": row["public_version"],
            "revision": row["revision"],
            "summary": row["summary"],
            "language": row["language"],
            "runtime": row["runtime"],
            "signature": row["signature"],
            "inputs": json_loads(row["inputs_json"]),
            "outputs": json_loads(row["outputs_json"]),
            "dependencies": json_loads(row["dependencies_json"]),
            "environment": json_loads(row["environment_json"]),
            "side_effects": row["side_effects"],
            "usage_notes": row["usage_notes"],
            "limitations": row["limitations"],
            "tags": json_loads(row["tags_json"]) or [],
            "code_hash": row["code_hash"],
            "normalized_line_count": row["normalized_line_count"],
            "diff_ratio": row["diff_ratio"],
            "base_revision_id": row["base_revision_id"],
            "created_at": row["created_at"],
            "approved_at": row["approved_at"],
            "context": {
                "user": row["user_name"] if "user_name" in row.keys() else None,
                "profile": row["profile_name"] if "profile_name" in row.keys() else None,
                "knowledge": row["knowledge_name"] if "knowledge_name" in row.keys() else None,
                "module": row["module_name"] if "module_name" in row.keys() else None,
            },
        }
        if include_code:
            payload["code"] = self._read_revision_code(row)
        return payload

    def _compact_revision_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "function_id": payload["function_id"],
            "revision_id": payload["revision_id"],
            "name": payload["name"],
            "summary": payload["summary"],
            "language": payload["language"],
            "signature": payload["signature"],
            "tags": payload["tags"],
            "context": payload["context"],
            "public_version": payload["public_version"],
            "revision": payload["revision"],
            "score": payload.get("score"),
        }
