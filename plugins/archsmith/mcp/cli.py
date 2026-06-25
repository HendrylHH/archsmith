from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.storage import ArchSmithError, ArchSmithStore


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def minimal_result(value: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "path",
        "function_id",
        "revision_id",
        "dry_run",
        "would_write",
        "target_exists",
        "reuse_log_id",
        "reuse_recorded",
        "reuse_error",
        "reuse_retry",
    )
    return {field: value[field] for field in fields if field in value}


def minimal_project_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_path": value["project_path"],
        "count": value["count"],
        "files": [
            {
                key: item[key]
                for key in ("name", "path", "function_id", "revision_id", "diff_ratio", "reuse_log_id", "reuse_recorded")
                if key in item
            }
            for item in value["files"]
        ],
    }


def load_spec(path: str) -> dict[str, Any]:
    spec_path = Path(path).expanduser()
    return json.loads(spec_path.read_text(encoding="utf-8"))


def add_context_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user")
    parser.add_argument("--profile")
    parser.add_argument("--knowledge")
    parser.add_argument("--module")
    parser.add_argument("--language")
    parser.add_argument("--tag", action="append", dest="tags")


def materialize(args: argparse.Namespace) -> dict[str, Any]:
    name = args.name or args.positional_name
    if not name:
        raise ArchSmithError("name is required")
    if not args.confirm_write and not args.dry_run:
        raise ArchSmithError("--confirm-write is required")
    data: dict[str, Any] = {
        "name": name,
        "destination_path": args.dest,
        "filename": args.filename,
        "overwrite": args.overwrite,
        "confirm_write": args.confirm_write,
        "dry_run": args.dry_run,
        "record_reuse": args.record_reuse or not args.no_record_reuse,
        "project_path": args.project_path or args.dest,
        "client": args.client,
        "notes": args.notes,
        "allow_fuzzy": args.allow_fuzzy,
        "language": args.language,
        "tags": args.tags or [],
        "include_hash": args.include_hash,
    }
    context = {
        key: value
        for key, value in {
            "user": args.user,
            "profile": args.profile,
            "knowledge": args.knowledge,
            "module": args.module,
        }.items()
        if value
    }
    if context:
        data["context"] = context
    store = ArchSmithStore()
    try:
        return store.materialize_by_name(data)
    finally:
        store.close()


def recommend(args: argparse.Namespace) -> dict[str, Any]:
    data: dict[str, Any] = {
        "task": args.task,
        "language": args.language,
        "tags": args.tags or [],
        "desired_functions": args.function or [],
        "limit": args.limit,
    }
    context = {
        key: value
        for key, value in {
            "user": args.user,
            "profile": args.profile,
            "knowledge": args.knowledge,
            "module": args.module,
        }.items()
        if value
    }
    if context:
        data["context"] = context
    store = ArchSmithStore()
    try:
        return store.recommend_reuse(data)
    finally:
        store.close()


def reuse(args: argparse.Namespace) -> dict[str, Any]:
    store = ArchSmithStore()
    try:
        return store.record_reuse(
            {
                "revision_id": args.revision_id,
                "function_id": args.function_id,
                "project_path": args.project_path,
                "client": args.client,
                "notes": args.notes,
            }
        )
    finally:
        store.close()


def estimate(args: argparse.Namespace) -> dict[str, Any]:
    name = args.name or args.positional_name
    if not name:
        raise ArchSmithError("name is required")
    data: dict[str, Any] = {
        "name": name,
        "destination_path": args.dest,
        "language": args.language,
        "tags": args.tags or [],
    }
    context = {
        key: value
        for key, value in {
            "user": args.user,
            "profile": args.profile,
            "knowledge": args.knowledge,
            "module": args.module,
        }.items()
        if value
    }
    if context:
        data["context"] = context
    store = ArchSmithStore()
    try:
        return store.estimate_savings(data)
    finally:
        store.close()


def project(args: argparse.Namespace) -> dict[str, Any]:
    data = load_spec(args.spec)
    if args.dest:
        data["destination_path"] = args.dest
    if args.confirm_write:
        data["confirm_write"] = True
    data["record_reuse"] = args.record_reuse or not args.no_record_reuse
    if args.overwrite:
        data["overwrite"] = True
    if args.client:
        data["client"] = args.client
    if args.notes:
        data["notes"] = args.notes
    store = ArchSmithStore()
    try:
        return store.materialize_project(data)
    finally:
        store.close()


def plan_project(args: argparse.Namespace) -> dict[str, Any]:
    data = load_spec(args.spec)
    if args.dest:
        data["destination_path"] = args.dest
    store = ArchSmithStore()
    try:
        return store.plan_project(data)
    finally:
        store.close()


def estimate_project(args: argparse.Namespace) -> dict[str, Any]:
    data = load_spec(args.spec)
    if args.dest:
        data["destination_path"] = args.dest
    store = ArchSmithStore()
    try:
        return store.estimate_project_savings(data)
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archsmith")
    subparsers = parser.add_subparsers(dest="command", required=True)

    materialize_parser = subparsers.add_parser("materialize")
    materialize_parser.add_argument("positional_name", nargs="?")
    materialize_parser.add_argument("--name")
    materialize_parser.add_argument("--dest", "--destination", dest="dest", required=True)
    materialize_parser.add_argument("--filename")
    materialize_parser.add_argument("--overwrite", action="store_true")
    materialize_parser.add_argument("--confirm-write", action="store_true")
    materialize_parser.add_argument("--dry-run", action="store_true")
    materialize_parser.add_argument("--record-reuse", action="store_true")
    materialize_parser.add_argument("--no-record-reuse", action="store_true")
    materialize_parser.add_argument("--minimal", action="store_true")
    materialize_parser.add_argument("--include-hash", action="store_true")
    materialize_parser.add_argument("--project-path")
    materialize_parser.add_argument("--client")
    materialize_parser.add_argument("--notes")
    materialize_parser.add_argument("--allow-fuzzy", action="store_true")
    add_context_flags(materialize_parser)

    recommend_parser = subparsers.add_parser("recommend")
    recommend_parser.add_argument("--task", required=True)
    recommend_parser.add_argument("--function", action="append")
    recommend_parser.add_argument("--limit", type=int, default=5)
    add_context_flags(recommend_parser)

    reuse_parser = subparsers.add_parser("reuse")
    reuse_parser.add_argument("--revision-id", type=int)
    reuse_parser.add_argument("--function-id", type=int)
    reuse_parser.add_argument("--project-path")
    reuse_parser.add_argument("--client")
    reuse_parser.add_argument("--notes")

    estimate_parser = subparsers.add_parser("estimate")
    estimate_parser.add_argument("positional_name", nargs="?")
    estimate_parser.add_argument("--name")
    estimate_parser.add_argument("--dest", "--destination", dest="dest")
    add_context_flags(estimate_parser)

    project_parser = subparsers.add_parser("project")
    project_parser.add_argument("--spec", required=True)
    project_parser.add_argument("--dest", "--destination", dest="dest")
    project_parser.add_argument("--overwrite", action="store_true")
    project_parser.add_argument("--confirm-write", action="store_true")
    project_parser.add_argument("--record-reuse", action="store_true")
    project_parser.add_argument("--no-record-reuse", action="store_true")
    project_parser.add_argument("--minimal", action="store_true")
    project_parser.add_argument("--client")
    project_parser.add_argument("--notes")

    plan_project_parser = subparsers.add_parser("plan-project")
    plan_project_parser.add_argument("--spec", required=True)
    plan_project_parser.add_argument("--dest", "--destination", dest="dest")

    estimate_project_parser = subparsers.add_parser("estimate-project")
    estimate_project_parser.add_argument("--spec", required=True)
    estimate_project_parser.add_argument("--dest", "--destination", dest="dest")

    args = parser.parse_args(argv)
    try:
        if args.command == "materialize":
            result = materialize(args)
            if args.minimal:
                result = minimal_result(result)
        elif args.command == "recommend":
            result = recommend(args)
        elif args.command == "reuse":
            result = reuse(args)
        elif args.command == "estimate":
            result = estimate(args)
        elif args.command == "project":
            result = project(args)
            if args.minimal:
                result = minimal_project_result(result)
        elif args.command == "plan-project":
            result = plan_project(args)
        elif args.command == "estimate-project":
            result = estimate_project(args)
        else:
            parser.error("unknown command")
    except ArchSmithError as exc:
        print(compact(exc.payload()), file=sys.stderr)
        return 2
    print(compact(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
