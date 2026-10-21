# Contributing to DIANA-MCP

Thank you for improving DIANA-MCP. Changes that affect model creation or solver submission can change engineering results, so evidence and tests matter more than patch size.

## Before starting

1. Open an issue describing the problem, proposed behaviour and DIANA version.
2. Do not attach confidential models, license files, full manuals or unredacted solver logs.
3. For changes based on DIANA behaviour, state whether the behaviour comes from the manual, a shipped verification example, or a real test.

## Development setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Add `.[vector]` only when working on semantic retrieval.

## Tests

Run the non-live suite before every pull request:

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not live"
```

Live tests require a licensed local DIANA installation and open GUI windows:

```powershell
$env:DIANA_MCP_LIVE_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest tests\test_live.py
```

Do not enable live tests in public CI unless the runner is private and licensed.

## Pull request requirements

- Keep Python 3.7 compatibility in files executed inside DianaIE (`src/diana_mcp/bridge/`).
- Keep normal server code compatible with Python 3.10+.
- Add regression tests for parser, bridge, authorization and path-handling changes.
- Update the relevant guide and `CHANGELOG.md` in the same pull request.
- Preserve the human-review gate. A new solver entry point must pass through `JobManager.submit` and a one-use permit.
- Do not mark an experience as verified without explicit user approval and evidence.
- Keep tool output bounded; export large tables to files.

## Commit and review hygiene

- Use focused commits with an imperative subject.
- Explain the engineering risk and verification performed.
- Remove machine-specific paths, IP addresses, tokens and project names.
- Confirm that `models/`, `knowledge/`, `.scratch/`, `.mcp.json` and generated DIANA files are not staged.

## Experience handbook contributions

The handbook is user-owned data and is not part of the source repository by default. Propose generally useful lessons as redacted Markdown in a separate pull request or a dedicated public handbook repository. Historical-log drafts are not publishable evidence by themselves.
