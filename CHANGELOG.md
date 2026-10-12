# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Entries are derived from the commit history, which follows
[Conventional Commits](https://www.conventionalcommits.org/).

## [Unreleased]

### Added

- `call_control_create` and `voice_campaigns_create` ask the user to confirm the
  caller ID and destination through the MCP client (elicitation) and dial only
  after the user accepts.

### Changed

- **Breaking:** clients without form elicitation can no longer place calls with
  `call_control_create` and `voice_campaigns_create`; they get an error pointing
  to the Wavix portal. Phone numbers must be E.164 digits, with an optional
  leading `+`, without spaces or punctuation.
- `call_control_create` and `voice_campaigns_create` no longer publish an output
  schema, so a declined call returns as a normal result.
- Requires `fastmcp>=3.4.8`; `anyio`, `mcp` and `pydantic` are declared dependencies.

### Fixed

- A connection signed in with a Wavix account no longer answers every action with
  "Access denied" once its access token expires. The server now replies with an
  OAuth challenge, so the client renews the token or asks the user to sign in
  again. Connections that use a Wavix API key are unchanged.

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
