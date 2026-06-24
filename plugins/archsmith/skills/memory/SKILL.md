---
name: memory
description: Use when authoring, approving, updating, or troubleshooting ArchSmith memory. For a simple request to materialize an approved function by name, do not load this skill; call archsmith_materialize_by_name directly.
---

# ArchSmith

Use ArchSmith to preserve and reuse approved local engineering work instead of recreating the same function repeatedly.

## Direct Calls

1. For a named function request, call `archsmith_materialize_by_name` first. Pass `name`, `destination_path`, `confirm_write=true`, `record_reuse=true`, `client`, and known context filters.
2. For a new project from multiple approved functions, call `archsmith_materialize_project`. Pass `destination_path`, `confirm_write=true`, `record_reuse=true`, shared context filters, and a `functions` array.
3. For token accounting, call `archsmith_estimate_savings` for one function or `archsmith_estimate_project_savings` for a batch. Do not read stored code just to count tokens.
4. Search with `archsmith_search_functions` only when the name is missing, ambiguous, or the user asks for discovery.
5. Load code with `archsmith_get_function(include_code=true)` only when the task truly requires inspection or modification.
6. Ask for explicit user approval before `archsmith_approve_function`.

## Low-Token Fallback

- If ArchSmith MCP tools are not exposed in the current session, use the bundled CLI at `mcp/cli.py`.
- Do not inspect or print `mcp/storage.py`, run CLI help, list contexts, read database rows, or read stored code for simple materialization.
- For named materialization, run one CLI call: `python mcp/cli.py materialize <name> --destination <path> --confirm-write --record-reuse --minimal --client <client>`, plus context filters when known.
- When `--record-reuse` is used from a sandboxed session, request elevated permission before the CLI call because it writes the local ArchSmith data directory.
- If reuse still fails, do not materialize again. Run `python mcp/cli.py reuse --revision-id <id> --project-path <path> --client <client>` using the returned revision ID.
- For project batches, use `python mcp/cli.py project --spec <json-file> --confirm-write --record-reuse --minimal --client <client>`.
- Emit one short progress update before materialization and one concise final result with the written path and reuse status.
- Do not list all contexts or search results unless the requested name is ambiguous or missing.
- Do not run extra syntax checks unless the user asks or the task requires executing the materialized file.

## Safety

- Do not store secrets, credentials, cookies, tokens, full connection strings, or private keys.
- Store non-secret operational metadata such as environment variable names, required dependencies, preconditions, and usage notes.
- Treat code returned by ArchSmith as local user data. Do not send it to external services.
- Keep output token usage low: summarize found functions first, and avoid reprinting approved code unless the user asks or the task requires it.

## Versioning

- Changes with a normalized difference of 20% or less stay in the same public version and retain revision history.
- Changes above 20% become a new public version candidate and require explicit approval.
- Do not replace approved history. Use the MCP tools so hashes, files, revision metadata, and reuse logs stay consistent.
