# DIANA-MCP

> Control DIANA FEA from an MCP-compatible AI assistant while keeping model approval and solver submission under human supervision.

[中文](README.md) · [Quick start (Chinese)](docs/zh/quickstart.md) · [User guide (Chinese)](docs/zh/user-guide.md) · [Configuration](docs/zh/configuration.md)

DIANA-MCP connects to the live DianaIE graphical interface and can run `diana.exe` as a background solver. It provides model editing, GUI change tracking, review-gated solver submission, result parsing, local manual search, and a user-approved experience handbook.

This project does not include DIANA, a DIANA license, or the DIANA user manual. It reads documentation and API metadata from the user's licensed local installation at runtime. DIANA-MCP is independently developed and is not affiliated with DIANA FEA BV.

## Highlights

- Drive a live DianaIE session through its embedded Python API.
- Observe user edits, undo/redo operations, and before/after parameter values.
- Require a human GUI review before a development model can be solved.
- Promote a model to a trusted baseline only after a clean validation solve.
- Automate parameter variants only inside a human-approved envelope.
- Parse `.out` and `.tb` files and export large result tables to CSV.
- Search the installed manual, shipped examples, SCHEMA keywords, API docs, and local lessons.
- Draft lessons automatically while reserving the “verified” status for explicit user approval.
- Optionally run local bilingual semantic search with BAAI/bge-m3.

## Quick start

Requirements: DIANA 10.5 with a valid license, Python 3.10+, and an MCP stdio client.

```powershell
git clone https://github.com/YOUR_ACCOUNT/diana-mcp.git
Set-Location diana-mcp
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
(Resolve-Path .\.venv\Scripts\python.exe).Path
```

Copy [.mcp.example.json](.mcp.example.json) to `.mcp.json` and replace its `command` with the absolute Python path printed above. Then enable the DianaIE bridge:

```powershell
.\.venv\Scripts\python.exe -m diana_mcp autostart enable
.\.venv\Scripts\python.exe -m diana_mcp autostart status
```

Restart DianaIE and the MCP client. Ask the assistant to call `diana_status`, `diana_start`, and `diana_guide(topic="workflow")`.

## Documentation

The detailed documentation is currently maintained in Chinese:

- [Quick start](docs/zh/quickstart.md)
- [Daily operation guide](docs/zh/user-guide.md)
- [Configuration reference](docs/zh/configuration.md)
- [Tool reference](docs/zh/tools.md)
- [Architecture](docs/zh/architecture.md)
- [Security and privacy](docs/zh/security-and-privacy.md)
- [Publishing runbook](docs/zh/publishing.md)

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -m "not live"
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change. Report security issues according to [SECURITY.md](SECURITY.md).

## License

DIANA-MCP is licensed under the [Apache License 2.0](LICENSE). The license covers only code and documentation that the project contributors are entitled to license; it grants no rights to DIANA software, manuals, trademarks, or user engineering data. See [NOTICE.md](NOTICE.md).
