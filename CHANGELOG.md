# Changelog

All notable changes to DIANA-MCP are documented here. Versions follow Semantic Versioning after the first public release.

## [Unreleased]

### Changed

- Prepared public documentation and separated machine-specific configuration, user knowledge and embedding models from source control.
- Corrected DianaIE autostart paths for Linux/Wine deployments.
- Adopted the Apache License 2.0 and added public-repository governance files.

## [0.9.0] — 2026-10-21

### Added

- Mandatory DianaIE GUI review before development-stage solver submission.
- Trusted baseline certification after a clean validation solve.
- Human-approved parameter-study envelopes with pre-solve and post-solve checks.
- One-use, SHA-256-bound solver permits and an append-only audit trail.
- Split `compute` and `knowledge` server profiles for remote deployments.

### Security

- Blocked direct solver entry points in `diana_execute` and in the DianaIE bridge.
- Required every solver process to enter through the central submission gate.

## [0.7.0]

### Added

- Local experience handbook, approval workflow, verification scripts and handbook export.
- Pluggable semantic retrieval with local BAAI/bge-m3 support.

## [0.4.0]

### Added

- GUI command journal, model snapshots, change attribution and automatic bridge startup.

## [0.3.0]

### Added

- DIANA manual, SCHEMA and API lookup.
- Robust `.tb` and `.out` parsers, including unpaged TABULA output.

## [0.2.0]

### Added

- Background solver jobs and the simply supported beam end-to-end example.

## [0.1.0]

### Added

- Initial live DianaIE bridge and MCP server.
