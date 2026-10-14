# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Entries are derived from the commit history, which follows
[Conventional Commits](https://www.conventionalcommits.org/).

## [Unreleased]

## [1.3.0] - 2026-10-14

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
- `whatsapp_wabas_list` and `whatsapp_wabas_get` tools.

### Changed

- The tools above no longer act on the first call; a client must call them
  again with the `confirmation_token` from the preview.
- The tools above no longer publish an output schema, so a preview returns as a
  normal result.
- Requires `fastmcp>=3.4.8`; `anyio`, `mcp` and `pydantic` are declared dependencies.
- Call tools (`call_control_create`, `voice_campaigns_create`,
  `call_control_audio_play`, `call_control_collect`,
  `call_control_streams_create`) and the server instructions say that the
  Wavix Terms of Service require the customer to disclose an automated or
  AI-handled call at the start of the call.
- `sip_trunks_update` and `sub_accounts_update` say that only the provided
  fields change; omitted fields no longer revert to their defaults.
  `sip_trunks_update` also says that new `allowed_ips` entries are added to the
  list and that trial accounts get the trial limits on every update.
- `cdrs_retranscribe` asks for confirmation like the other destructive tools.
  It can transcribe an already transcribed call again; the new transcription
  replaces the earlier one.
- `whatsapp_senders_get` no longer mentions a messaging limit tier; the
  messaging limit is on the WhatsApp Business Account (`whatsapp_wabas_get`).
- `sub_accounts_create` and `sub_accounts_update` limit `name` to 75 characters.

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

[Unreleased]: https://github.com/Wavix/wavix-mcp-server/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/Wavix/wavix-mcp-server/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Wavix/wavix-mcp-server/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Wavix/wavix-mcp-server/compare/v1.0.0...v1.1.0
