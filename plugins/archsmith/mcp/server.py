from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

sys.dont_write_bytecode = True

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.storage import ArchSmithError, ArchSmithStore, NotFoundError, ValidationError


SERVER_NAME = "archsmith-mcp"
PROTOCOL_VERSION = "2025-06-18"


def text_schema(description: str, required: list[str] | None = None, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


CONTEXT_PROPERTIES = {
    "user": {"type": "string"},
    "profile": {"type": "string"},
    "knowledge": {"type": "string"},
    "module": {"type": "string"},
}

JSON_OBJECT_SCHEMA = {"type": "object", "additionalProperties": True}
STRING_ARRAY_SCHEMA = {"type": "array", "items": {"type": "string"}}
PROJECT_FUNCTION_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "context": text_schema("Context filters.", properties=CONTEXT_PROPERTIES),
        "user": {"type": "string"},
        "profile": {"type": "string"},
        "knowledge": {"type": "string"},
        "module": {"type": "string"},
        "language": {"type": "string"},
        "tags": STRING_ARRAY_SCHEMA,
        "filename": {"type": "string"},
        "replacements": JSON_OBJECT_SCHEMA,
        "allow_fuzzy": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["name"],
    "additionalProperties": False,
}


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "archsmith_list_contexts",
        "description": "List stored context records without returning code.",
        "inputSchema": text_schema(
            "Optional context filters.",
            properties=CONTEXT_PROPERTIES,
        ),
    },
    {
        "name": "archsmith_upsert_context",
        "description": "Create or update a local user/profile/knowledge/module context.",
        "inputSchema": text_schema(
            "Context fields.",
            required=["user", "profile", "knowledge"],
            properties=CONTEXT_PROPERTIES,
        ),
    },
    {
        "name": "archsmith_search_functions",
        "description": "Search approved functions and return summaries, contracts, and metadata without code by default.",
        "inputSchema": text_schema(
            "Search filters.",
            properties={
                "context": text_schema("Context filters.", properties=CONTEXT_PROPERTIES),
                "user": {"type": "string"},
                "profile": {"type": "string"},
                "knowledge": {"type": "string"},
                "module": {"type": "string"},
                "query": {"type": "string"},
                "tags": STRING_ARRAY_SCHEMA,
                "language": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "detail": {"type": "boolean"},
            },
        ),
    },
    {
        "name": "archsmith_recommend_reuse",
        "description": "Recommend approved reusable functions for a task, returning confidence, reasons, and next action without code.",
        "inputSchema": text_schema(
            "Reuse recommendation request.",
            required=["task"],
            properties={
                "task": {"type": "string"},
                "context": text_schema("Context filters.", properties=CONTEXT_PROPERTIES),
                "user": {"type": "string"},
                "profile": {"type": "string"},
                "knowledge": {"type": "string"},
                "module": {"type": "string"},
                "language": {"type": "string"},
                "tags": STRING_ARRAY_SCHEMA,
                "desired_functions": STRING_ARRAY_SCHEMA,
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
        ),
    },
    {
        "name": "archsmith_get_function",
        "description": "Get function metadata, recipe, and optionally code.",
        "inputSchema": text_schema(
            "Function selector.",
            properties={
                "function_id": {"type": "integer"},
                "revision_id": {"type": "integer"},
                "include_code": {"type": "boolean"},
            },
        ),
    },
    {
        "name": "archsmith_propose_function",
        "description": "Store a draft function or draft change for explicit approval.",
        "inputSchema": text_schema(
            "Function proposal.",
            required=["name", "summary", "language", "code"],
            properties={
                "context": text_schema("Context fields.", properties=CONTEXT_PROPERTIES),
                "function_id": {"type": "integer"},
                "base_function_id": {"type": "integer"},
                "name": {"type": "string"},
                "summary": {"type": "string"},
                "language": {"type": "string"},
                "code": {"type": "string"},
                "runtime": {"type": "string"},
                "signature": {"type": "string"},
                "inputs": JSON_OBJECT_SCHEMA,
                "outputs": JSON_OBJECT_SCHEMA,
                "dependencies": JSON_OBJECT_SCHEMA,
                "environment": JSON_OBJECT_SCHEMA,
                "side_effects": {"type": "string"},
                "usage_notes": {"type": "string"},
                "limitations": {"type": "string"},
                "tags": STRING_ARRAY_SCHEMA,
                "proposed_by": {"type": "string"},
                "client": {"type": "string"},
                "metadata": JSON_OBJECT_SCHEMA,
            },
        ),
    },
    {
        "name": "archsmith_approve_function",
        "description": "Approve a draft function or draft change after explicit user approval.",
        "inputSchema": text_schema(
            "Approval fields.",
            required=["revision_id", "approved_by"],
            properties={
                "revision_id": {"type": "integer"},
                "approved_by": {"type": "string"},
                "decision_note": {"type": "string"},
            },
        ),
    },
    {
        "name": "archsmith_materialize_function",
        "description": "Write approved function code to a validated local destination.",
        "inputSchema": text_schema(
            "Materialization target.",
            required=["destination_path", "confirm_write"],
            properties={
                "function_id": {"type": "integer"},
                "revision_id": {"type": "integer"},
                "destination_path": {"type": "string"},
                "filename": {"type": "string"},
                "overwrite": {"type": "boolean"},
                "confirm_write": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
                "include_hash": {"type": "boolean"},
            },
        ),
    },
    {
        "name": "archsmith_materialize_by_name",
        "description": "Primary low-token ArchSmith call for requests like materialize a function by name. Finds one approved function, writes it locally, and can record reuse without loading the skill or code.",
        "inputSchema": text_schema(
            "Named materialization target.",
            required=["name", "destination_path", "confirm_write"],
            properties={
                "name": {"type": "string"},
                "context": text_schema("Context filters.", properties=CONTEXT_PROPERTIES),
                "user": {"type": "string"},
                "profile": {"type": "string"},
                "knowledge": {"type": "string"},
                "module": {"type": "string"},
                "language": {"type": "string"},
                "tags": STRING_ARRAY_SCHEMA,
                "destination_path": {"type": "string"},
                "filename": {"type": "string"},
                "overwrite": {"type": "boolean"},
                "confirm_write": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
                "record_reuse": {"type": "boolean"},
                "project_path": {"type": "string"},
                "client": {"type": "string"},
                "used_by": {"type": "string"},
                "notes": {"type": "string"},
                "allow_fuzzy": {"type": "boolean"},
                "include_hash": {"type": "boolean"},
            },
        ),
    },
    {
        "name": "archsmith_plan_project",
        "description": "Plan a project from approved functions without writing files or returning code.",
        "inputSchema": text_schema(
            "Project planning target.",
            required=["destination_path", "functions"],
            properties={
                "destination_path": {"type": "string"},
                "functions": {"type": "array", "items": PROJECT_FUNCTION_SCHEMA, "minItems": 1},
                "context": text_schema("Shared context filters.", properties=CONTEXT_PROPERTIES),
                "user": {"type": "string"},
                "profile": {"type": "string"},
                "knowledge": {"type": "string"},
                "module": {"type": "string"},
                "overwrite": {"type": "boolean"},
                "client": {"type": "string"},
                "notes": {"type": "string"},
            },
        ),
    },
    {
        "name": "archsmith_record_reuse",
        "description": "Record successful reuse of an approved revision.",
        "inputSchema": text_schema(
            "Reuse metadata.",
            properties={
                "function_id": {"type": "integer"},
                "revision_id": {"type": "integer"},
                "project_path": {"type": "string"},
                "client": {"type": "string"},
                "notes": {"type": "string"},
            },
        ),
    },
    {
        "name": "archsmith_materialize_project",
        "description": "Create a local project by materializing multiple approved functions, with optional minor replacements capped by the 20% mutation threshold.",
        "inputSchema": text_schema(
            "Project materialization target.",
            required=["destination_path", "functions", "confirm_write"],
            properties={
                "destination_path": {"type": "string"},
                "functions": {"type": "array", "items": PROJECT_FUNCTION_SCHEMA, "minItems": 1},
                "context": text_schema("Shared context filters.", properties=CONTEXT_PROPERTIES),
                "user": {"type": "string"},
                "profile": {"type": "string"},
                "knowledge": {"type": "string"},
                "module": {"type": "string"},
                "overwrite": {"type": "boolean"},
                "confirm_write": {"type": "boolean"},
                "record_reuse": {"type": "boolean"},
                "client": {"type": "string"},
                "notes": {"type": "string"},
            },
        ),
    },
    {
        "name": "archsmith_estimate_savings",
        "description": "Estimate input/output token savings for a named approved function without reading stored code into the conversation.",
        "inputSchema": text_schema(
            "Savings estimate target.",
            required=["name"],
            properties={
                "name": {"type": "string"},
                "context": text_schema("Context filters.", properties=CONTEXT_PROPERTIES),
                "user": {"type": "string"},
                "profile": {"type": "string"},
                "knowledge": {"type": "string"},
                "module": {"type": "string"},
                "language": {"type": "string"},
                "tags": STRING_ARRAY_SCHEMA,
                "destination_path": {"type": "string"},
            },
        ),
    },
    {
        "name": "archsmith_estimate_project_savings",
        "description": "Estimate input/output token savings for creating a project from multiple approved functions without returning stored code.",
        "inputSchema": text_schema(
            "Project savings estimate target.",
            required=["destination_path", "functions"],
            properties={
                "destination_path": {"type": "string"},
                "functions": {"type": "array", "items": PROJECT_FUNCTION_SCHEMA, "minItems": 1},
                "context": text_schema("Shared context filters.", properties=CONTEXT_PROPERTIES),
                "user": {"type": "string"},
                "profile": {"type": "string"},
                "knowledge": {"type": "string"},
                "module": {"type": "string"},
            },
        ),
    },
    {
        "name": "archsmith_deprecate_function",
        "description": "Mark an approved function or revision as deprecated without deleting history.",
        "inputSchema": text_schema(
            "Deprecation target.",
            properties={
                "function_id": {"type": "integer"},
                "revision_id": {"type": "integer"},
                "reason": {"type": "string"},
                "deprecated_by": {"type": "string"},
            },
        ),
    },
]


PROMPT_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "reuse_named_function",
        "description": "Materialize one approved function by name with optional context filters.",
        "arguments": [
            {"name": "name", "description": "Approved function name.", "required": True},
            {"name": "destination_path", "description": "Local destination path.", "required": True},
            {"name": "context", "description": "Optional user/profile/knowledge/module context.", "required": False},
        ],
    },
    {
        "name": "recommend_reuse_for_task",
        "description": "Ask ArchSmith which approved functions should be reused for a task.",
        "arguments": [
            {"name": "task", "description": "Task or user request.", "required": True},
            {"name": "context", "description": "Optional context filters.", "required": False},
        ],
    },
    {
        "name": "create_project_from_approved_functions",
        "description": "Plan and then materialize a project from approved functions.",
        "arguments": [
            {"name": "destination_path", "description": "Local project directory.", "required": True},
            {"name": "functions", "description": "Approved function names and optional filenames/replacements.", "required": True},
        ],
    },
    {
        "name": "approve_candidate_function",
        "description": "Review a proposed candidate function and approve only after explicit user consent.",
        "arguments": [
            {"name": "revision_id", "description": "Candidate revision ID.", "required": True},
            {"name": "approved_by", "description": "Approving user or team.", "required": True},
        ],
    },
]


RESOURCE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "uri": "archsmith://contexts",
        "name": "ArchSmith Contexts",
        "description": "Stored user/profile/knowledge/module contexts.",
        "mimeType": "application/json",
    },
    {
        "uri": "archsmith://functions/approved",
        "name": "Approved Function Metadata",
        "description": "Approved function metadata without code.",
        "mimeType": "application/json",
    },
]


class JsonRpcServer:
    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self.store = ArchSmithStore()
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "archsmith_list_contexts": self.store.list_contexts,
            "archsmith_upsert_context": self.store.upsert_context,
            "archsmith_search_functions": self.store.search_functions,
            "archsmith_recommend_reuse": self.store.recommend_reuse,
            "archsmith_get_function": self.store.get_function,
            "archsmith_propose_function": self.store.propose_function,
            "archsmith_approve_function": self.store.approve_function,
            "archsmith_materialize_function": self.store.materialize_function,
            "archsmith_materialize_by_name": self.store.materialize_by_name,
            "archsmith_record_reuse": self.store.record_reuse,
            "archsmith_plan_project": self.store.plan_project,
            "archsmith_materialize_project": self.store.materialize_project,
            "archsmith_estimate_savings": self.store.estimate_savings,
            "archsmith_estimate_project_savings": self.store.estimate_project_savings,
            "archsmith_deprecate_function": self.store.deprecate_function,
        }

    def serve(self) -> None:
        try:
            for message in read_messages(sys.stdin.buffer):
                response = self.handle_message(message)
                if response is not None:
                    write_message(sys.stdout.buffer, response)
        finally:
            self.store.close()

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        if method and method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                requested_protocol = (message.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
                result = {
                    "protocolVersion": requested_protocol,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "prompts": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                    },
                    "serverInfo": {"name": SERVER_NAME, "version": "0.2.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOL_DEFINITIONS}
            elif method == "tools/call":
                params = message.get("params") or {}
                result = self.call_tool(str(params.get("name") or ""), params.get("arguments") or {})
            elif method == "prompts/list":
                result = {"prompts": PROMPT_DEFINITIONS}
            elif method == "prompts/get":
                result = self.get_prompt(message.get("params") or {})
            elif method == "resources/list":
                result = {"resources": self.list_resources()}
            elif method == "resources/read":
                result = self.read_resource(message.get("params") or {})
            else:
                return error_response(request_id, -32601, "Method not found")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except ArchSmithError as exc:
            if method == "tools/call":
                return {"jsonrpc": "2.0", "id": request_id, "result": tool_error(exc)}
            return error_response(request_id, -32602, json.dumps(exc.payload(), ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        except Exception as exc:
            if self.debug:
                traceback.print_exc(file=sys.stderr)
            return error_response(request_id, -32603, str(exc))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.handlers:
            raise ValidationError("Unknown ArchSmith tool")
        result = self.handlers[name](arguments)
        return tool_result(result)

    def get_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        prompts = {
            "reuse_named_function": (
                "Use archsmith_materialize_by_name with confirm_write=true, record_reuse=true, "
                "known context filters, and no code inspection. Function: {name}. Destination: {destination_path}."
            ),
            "recommend_reuse_for_task": (
                "Call archsmith_recommend_reuse for this task. If status is ready, use the suggested action. "
                "If status is ambiguous or needs_context, ask the user for the missing context. Task: {task}."
            ),
            "create_project_from_approved_functions": (
                "Call archsmith_plan_project first. If can_materialize is true and the user has approved writing, "
                "call archsmith_materialize_project with the same function list. Destination: {destination_path}."
            ),
            "approve_candidate_function": (
                "Only call archsmith_approve_function after explicit user approval. Revision ID: {revision_id}. "
                "Approved by: {approved_by}."
            ),
        }
        if name not in prompts:
            raise NotFoundError("prompt not found")
        class PromptArguments(dict[str, str]):
            def __missing__(self, key: str) -> str:
                return f"<{key}>"

        text = prompts[name].format_map(PromptArguments({key: str(value) for key, value in arguments.items()}))
        return {
            "description": next(prompt["description"] for prompt in PROMPT_DEFINITIONS if prompt["name"] == name),
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }

    def list_resources(self) -> list[dict[str, Any]]:
        resources = list(RESOURCE_DEFINITIONS)
        for function in self.store.approved_functions_metadata(limit=200)["functions"]:
            resources.append(
                {
                    "uri": f"archsmith://functions/{function['function_id']}/metadata",
                    "name": f"{function['name']} metadata",
                    "description": "Approved function metadata without code.",
                    "mimeType": "application/json",
                }
            )
        return resources

    def read_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = str(params.get("uri") or "")
        if uri == "archsmith://contexts":
            value = self.store.list_contexts({})
        elif uri == "archsmith://functions/approved":
            value = self.store.approved_functions_metadata()
        else:
            match = uri.startswith("archsmith://functions/") and uri.endswith("/metadata")
            if not match:
                raise NotFoundError("resource not found")
            function_id_text = uri.removeprefix("archsmith://functions/").removesuffix("/metadata")
            value = self.store.function_metadata(int(function_id_text))
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": text}]}


def tool_result(value: Any) -> dict[str, Any]:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def tool_error(error: ArchSmithError) -> dict[str, Any]:
    text = json.dumps(error.payload(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return {"content": [{"type": "text", "text": text}], "isError": True}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def read_messages(stream: Any) -> Any:
    while True:
        first = stream.readline()
        if not first:
            break
        if first in (b"\r\n", b"\n"):
            continue
        if first.lower().startswith(b"content-length:"):
            length = int(first.split(b":", 1)[1].strip())
            while True:
                line = stream.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            payload = stream.read(length)
            if not payload:
                break
            yield json.loads(payload.decode("utf-8"))
        else:
            stripped = first.strip()
            if stripped:
                yield json.loads(stripped.decode("utf-8"))


def write_message(stream: Any, message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
    stream.write(payload)
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=SERVER_NAME)
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    if not args.stdio:
        parser.error("--stdio is required")
    JsonRpcServer(debug=args.debug).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
