# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Entries are derived from the commit history, which follows
[Conventional Commits](https://www.conventionalcommits.org/).

## [Unreleased]

## [1.1.0] - 2026-09-23

### Added

- WhatsApp tools: senders, templates, and send / list / retrieve messages.
- Agent-facing tool descriptions for the full tool surface, served from an
  overlay file (`src/wavix_mcp/tool_descriptions.yaml`) instead of the OpenAPI spec.
- OAuth 2.1 support: the server acts as a protected resource, and tools are
  filtered by the connecting token's scopes.
- `x-mcp` spec metadata consumption for per-tool annotations and tool exclusion.

### Changed

- Account sign-in is presented alongside API-key auth in the README, the agent
  install guide, and the registry listing.
- Tool schemas are normalized for strict MCP client portability.

### Fixed

- API-key clients keep working when OAuth is enabled.
- Binary download tools honor the `Location` header only on 2xx/3xx responses.
- Invoice download and 10DLC evidence upload are excluded from the tool surface.
- The OAuth issuer is advertised without an RFC 8414-breaking trailing slash.

[Unreleased]: https://github.com/Wavix/wavix-mcp-server/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Wavix/wavix-mcp-server/compare/v1.0.0...v1.1.0
