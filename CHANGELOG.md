# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Entries are derived from the commit history, which follows
[Conventional Commits](https://www.conventionalcommits.org/).

## [Unreleased]

### Added

- Every tool annotated with `destructiveHint`, plus `call_webhooks_create`,
  `sip_trunks_update` and `sub_accounts_update`, runs in two steps: the first
  call does nothing and returns a preview with a single-use `confirmation_token`
  valid for 5 minutes; the action runs only when the tool is called again with
  the same arguments and that token, after the user has seen the preview and
  approved it in a later message; repeating that confirmed call returns the
  first result instead of acting again. The preview lists the arguments and,
  for tools that act on an existing call, recording, trunk, key, Sender ID,
  10DLC brand, campaign or evidence file, names it; releasing numbers lists the
  numbers, checkout states the total, and SIP trunk and sub-account updates list
  the settings that change.

### Changed

- **Breaking:** the tools above no longer act on the first call; a client
  must call them again with the `confirmation_token` from the preview.
- The tools above no longer publish an output schema, so a preview returns as a
  normal result.
- Requires `fastmcp>=3.4.8`; `anyio`, `mcp` and `pydantic` are declared dependencies.

## [1.2.0] - 2026-10-07

### Added

- `whatsapp_senders_list` and `whatsapp_templates_get` tools.

### Changed

- Server instructions are descriptive text.
- WhatsApp tool descriptions match the current WhatsApp API.

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

[Unreleased]: https://github.com/Wavix/wavix-mcp-server/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/Wavix/wavix-mcp-server/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Wavix/wavix-mcp-server/compare/v1.0.0...v1.1.0
