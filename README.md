<p align="center">
  <img src="assets/logo.png" alt="ArchSmith logo" width="420">
</p>

# ArchSmith

ArchSmith is a local-first engineering memory plugin and MCP server for reusing approved code instead of asking an AI agent to recreate the same functions again and again.

It stores approved functions, metadata, recipes, version history, and reuse logs on your machine. The MCP server does not make network calls. By default, data lives in:

```text
%USERPROFILE%\.codex\archsmith
```

## Why

AI coding agents spend a lot of output tokens regenerating code that already exists in a user's projects. ArchSmith changes that workflow:

1. Save a function once.
2. Approve it explicitly.
3. Reuse or materialize it later by name.
4. Adapt small project-specific details only when the normalized diff stays within the configured mutation threshold.

The default mutation threshold is `20%`. Larger changes become new candidates and require explicit approval.

## Previously Approved Memory

ArchSmith is designed to act as previously approved engineering memory.

That means the agent should not treat stored functions as vague suggestions or conversational notes. A function only becomes reusable memory after it has been explicitly proposed, reviewed, and approved. Once approved, the agent can retrieve its contract, metadata, version, and local source file without spending tokens reconstructing the same implementation from scratch.

This makes ArchSmith closer to a local catalog of approved building blocks than a generic memory system:

- approved code is reused by name, context, and version;
- stored code is not printed into chat unless inspection is required;
- small adaptations are allowed only within the mutation threshold;
- larger changes become new candidate versions and need approval;
- reuse is logged locally so the history remains auditable.

## What ArchSmith Provides

- A local SQLite store for users, profiles, knowledge areas, modules, functions, revisions, and reuse logs.
- A stdio MCP server named `archsmith-mcp`.
- A Codex plugin named `archsmith` with the `memory` skill.
- Low-token tools for direct reuse:
  - `archsmith_materialize_by_name`
  - `archsmith_materialize_project`
  - `archsmith_estimate_savings`
  - `archsmith_estimate_project_savings`
- Approval-first versioning for reusable code.
- Local path validation and path traversal protection.
- Secret-like content rejection for obvious passwords, tokens, cookies, private keys, and full connection strings.

ArchSmith ships with no preloaded memory, no sample database, and no bundled project data.

## Install

### Requirements

- Python 3.10 or newer.
- A client that can run stdio MCP servers, such as Codex, Claude Desktop, or opencode.

### Standalone MCP

Clone the repository and point your MCP client at the server:

```json
{
  "mcpServers": {
    "archsmith-mcp": {
      "command": "python",
      "args": ["/absolute/path/to/plugins/archsmith/mcp/server.py", "--stdio"],
      "env": {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

On Windows, use an absolute path such as:

```json
{
  "mcpServers": {
    "archsmith-mcp": {
      "command": "python",
      "args": ["D:\\path\\to\\plugins\\archsmith\\mcp\\server.py", "--stdio"],
      "env": {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

### Codex Plugin

This repository includes a marketplace file at `.agents/plugins/marketplace.json` and the plugin source at `plugins/archsmith`.

After cloning:

```bash
codex plugin marketplace add /absolute/path/to/archsmith
codex plugin add archsmith@archsmith
```

Then start a new Codex thread so the MCP tools and skill metadata are loaded fresh.

## CLI Fallback

If your client does not expose MCP tools in a session, use the bundled CLI.

Materialize one approved function:

```bash
python plugins/archsmith/mcp/cli.py materialize function_name \
  --destination /path/to/project \
  --confirm-write \
  --record-reuse \
  --minimal \
  --client codex
```

Estimate savings for one approved function:

```bash
python plugins/archsmith/mcp/cli.py estimate function_name \
  --destination /path/to/project
```

Create a project from multiple approved functions:

```bash
python plugins/archsmith/mcp/cli.py project \
  --spec /path/to/project-spec.json \
  --confirm-write \
  --record-reuse \
  --minimal \
  --client codex
```

Estimate project-level savings:

```bash
python plugins/archsmith/mcp/cli.py estimate-project \
  --spec /path/to/project-spec.json
```

## Chat Prompt Examples

Use prompts like these with an MCP-enabled coding agent:

- "Use ArchSmith to create a new script with the saved functions `load_company_settings`, `normalize_customer_record`, and `write_project_report` from Project 123 at Company X. Change the company label from `Company X` to `Company Y` in the generated script only."
- "Use ArchSmith to materialize the approved function `parse_billing_csv` from Company X's Billing Toolkit into this project. Record reuse and do not print the stored code in chat."
- "Create a new project from the cataloged ArchSmith functions `read_job_manifest`, `prepare_output_dir`, `build_batch_summary`, `write_json_artifact`, and `format_status_line` for Company X. Change the output folder constant from `output` to `artifacts`."
- "Search ArchSmith for approved Python functions in Company X / Project 123 related to reporting. Show summaries and signatures only; load code only after I choose one."
- "Propose the current implementation as a reusable ArchSmith function under User X / Company X / Project 123 / reporting, but wait for my approval before marking it approved."

## Project Spec

`archsmith_materialize_project` and the CLI `project` command accept a JSON spec:

```json
{
  "destination_path": "/path/to/new-project",
  "context": {
    "user": "User X",
    "profile": "Company X",
    "knowledge": "Operations Toolkit",
    "module": "core"
  },
  "functions": [
    {
      "name": "load_company_settings",
      "filename": "settings.py",
      "replacements": {
        "DEFAULT_COMPANY = \"Company X\"": "DEFAULT_COMPANY = \"Company Y\""
      }
    },
    {
      "name": "normalize_customer_record",
      "filename": "records.py"
    },
    {
      "name": "write_project_report",
      "filename": "reports.py"
    }
  ]
}
```

Replacements are exact string substitutions applied only to the materialized output. They do not mutate the approved stored revision. If the normalized diff exceeds `20%`, ArchSmith blocks the write and requires a new approved version.

## Token Savings Samples

The tables below use synthetic functions and ArchSmith's local approximate tokenizer (`approx_regex_tokens`). Actual model tokenization varies, but each comparison is counted with the same method.

### Scenario 1: Three-Function Project

Create a new project from three approved functions under `User X / Company X / Operations Toolkit / core`, with one minor replacement.

| Scenario | Input | Output | Total |
| --- | ---: | ---: | ---: |
| With ArchSmith | 137 | 260 | 397 |
| Without ArchSmith | 688 | 428 | 1,116 |

Approximate reduction: `719` tokens, or about `64.4%`.

### Scenario 2: Single CSV Parser

Materialize one approved billing CSV parser without printing stored code in the chat.

| Scenario | Input | Output | Total |
| --- | ---: | ---: | ---: |
| With ArchSmith | 59 | 66 | 125 |
| Without ArchSmith | 249 | 474 | 723 |

Approximate reduction: `598` tokens, or about `82.7%`.

### Scenario 3: Five-Function Automation Project

Create a new automation project from five approved functions and apply one minor constant replacement.

| Scenario | Input | Output | Total |
| --- | ---: | ---: | ---: |
| With ArchSmith | 189 | 398 | 587 |
| Without ArchSmith | 1,051 | 440 | 1,491 |

Approximate reduction: `904` tokens, or about `60.6%`.

The larger the approved function is, and the more often it is reused, the more ArchSmith saves. The biggest gain is avoiding regenerated code output.

## Recommended Agent Workflow

For a simple reuse request:

1. Call `archsmith_materialize_by_name`.
2. Pass the function name, destination path, context filters when known, `confirm_write=true`, and `record_reuse=true`.
3. Do not load the stored code into the conversation unless inspection is required.

For a project request:

1. Call `archsmith_materialize_project`.
2. Provide a destination and the list of approved function names.
3. Use `replacements` only for small project-specific changes.
4. If ArchSmith reports a diff above `20%`, propose a new function version and ask for approval.

For token accounting:

1. Call `archsmith_estimate_savings` for one function.
2. Call `archsmith_estimate_project_savings` for a batch.
3. Do not manually read code or database rows just to count tokens.

## Security Model

- ArchSmith is local-first and offline by design.
- The MCP server does not make network calls.
- Stored code and metadata remain under the local data directory unless the user materializes them into a project.
- ArchSmith rejects common secret-like assignments and private key blocks.
- Do not store passwords, cookies, tokens, private keys, or full connection strings.
- Store environment variable names, preconditions, dependencies, and operational notes instead.

## Development Validation

Run the test suite from the repository root:

```bash
python -B -m unittest discover -s plugins/archsmith/tests -v
```

The tests cover:

- Context creation and lookup.
- Function approval and search without code by default.
- Named materialization.
- Project materialization with minor replacements.
- Project-level token estimates.
- The `20%` mutation threshold.
- Path traversal blocking.
- stdio MCP `tools/list` and `tools/call`.
- CLI materialization, reuse logging, and estimates.
