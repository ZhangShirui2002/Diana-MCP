# Security policy

## Supported versions

Security fixes are applied to the latest released minor version. Older snapshots are not guaranteed to receive fixes.

## Report a vulnerability

Do not open a public GitHub issue for a vulnerability that could expose local files, execute code, bypass the solver review gate, reuse a one-time permit, or connect to another user's DianaIE session.

Before the public repository is created, report privately to the repository owner. After publication, configure GitHub Security Advisories and replace this paragraph with the repository's private advisory URL or security email.

Include:

- affected version and operating system;
- whether DIANA runs locally or through Wine/SSH;
- the shortest reproducible sequence;
- expected and observed behaviour;
- logs with tokens, project names, model paths, host names and commercial data removed.

## Security model

- The bridge listens only on `127.0.0.1` and requires a random per-session token.
- `diana_execute` can execute arbitrary Python inside DianaIE. Connect only trusted MCP clients.
- Session files contain bridge tokens and must never be committed or shared.
- Solver submission uses one-use permits bound to the SHA-256 hashes of the exact `.dat` and `.dcf` files.
- Human approval is accepted only from the native review panel inside DianaIE.
- Known direct solver entry points are blocked in both the MCP server and the bridge.
- SSH private keys, host names and passwords belong in local MCP configuration, never in tracked examples.

See [docs/zh/security-and-privacy.md](docs/zh/security-and-privacy.md) for deployment guidance.
