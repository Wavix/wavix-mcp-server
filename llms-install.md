# Wavix MCP — Install Guide for AI Agents

This file gives LLMs and coding agents the minimum information needed to connect a user's MCP client to the Wavix MCP server. Follow it verbatim; do not improvise endpoint URLs or auth schemes.

## What this server does

Provides the full Wavix tool catalogue plus Wavix documentation as MCP resources (auto-discovered from `docs.wavix.com`) for the Wavix telecom platform: SMS/MMS, voice calls, 2FA, SIP trunking, phone-number provisioning, 10DLC brand and campaign registration, call recordings, speech analytics, CDRs, billing, and WebRTC token management.

## Server identity

- **Name:** `wavix`
- **Endpoint URL:** `https://mcp.wavix.com/mcp`
- **Transport:** `streamable-http` (a.k.a. Streamable HTTP, "HTTP Stream")
- **Hosted by Wavix.** No local install, no container, no package.

## Connecting

The hosted server supports two connection methods — pick whichever the client supports; neither is preferred:

**A. Sign in with a Wavix account (OAuth 2.1).** For OAuth-capable clients (Claude Desktop / Web). Add the connector for the endpoint URL below with **no** `Authorization` header; the client runs the OAuth 2.1 flow and the user signs in when prompted. No API key is stored. The available tools are scope-filtered to what the user approves and their account role permits.

**B. Wavix API key (Bearer).** For any Streamable-HTTP client. Obtain a key:

1. **Sign in** at <https://wavix.com>.
2. Open **Administration → API keys**.
3. Click **Create new** (or copy an existing key).

Pass it to the server as a Bearer token in the `Authorization` HTTP header:

```
Authorization: Bearer <api_key>
```

The server forwards this header per-request to `api.wavix.com`. Do not store the API key in the client repository or commit it to version control.

## Client configuration

The per-client examples below show **method B** (API key), which works in every Streamable-HTTP client. For **method A** (account sign-in), use the same URL but omit the `Authorization` header — the client obtains the token via OAuth when the user signs in.

### Claude Code

```bash
claude mcp add --transport http wavix https://mcp.wavix.com/mcp \
  --header "Authorization: Bearer <api_key>"
```

Verify with `claude mcp list`. Inside a session, `/mcp` shows live status.

### Claude Desktop / Claude Web

Open **Settings → Connectors → Add custom connector** and enter:

| Field | Value |
| --- | --- |
| Name | `Wavix` |
| URL | `https://mcp.wavix.com/mcp` |
| Transport | `Streamable HTTP` |
| Header | `Authorization: Bearer <api_key>` |

### Cursor

Edit `~/.cursor/mcp.json` (global) or `<project>/.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "wavix": {
      "url": "https://mcp.wavix.com/mcp",
      "headers": {
        "Authorization": "Bearer <api_key>"
      }
    }
  }
}
```

Cursor 2.4 or newer is required to expose the full tool catalogue; earlier versions cap at 40.

### VS Code (GitHub Copilot Chat / MCP-enabled extensions)

Create `.vscode/mcp.json` in the workspace (or place the same `servers` object under the `"mcp"` key in user `settings.json`):

```json
{
  "servers": {
    "wavix": {
      "type": "http",
      "url": "https://mcp.wavix.com/mcp",
      "headers": {
        "Authorization": "Bearer <api_key>"
      }
    }
  }
}
```

Refer to <https://code.visualstudio.com/docs/copilot/chat/mcp-servers> for the current VS Code MCP schema.

### Windsurf

In Windsurf settings, add an MCP server with:

- URL: `https://mcp.wavix.com/mcp`
- Header: `Authorization: Bearer <api_key>`

### Codex CLI

Codex CLI does not yet speak Streamable HTTP natively. Bridge through [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) in `~/.codex/config.toml`:

```toml
[mcp_servers.wavix]
command = "npx"
args = [
  "-y",
  "mcp-remote",
  "https://mcp.wavix.com/mcp",
  "--header",
  "Authorization:Bearer ${WAVIX_API_KEY}"
]

[mcp_servers.wavix.env]
WAVIX_API_KEY = "<api_key>"
```

### Any MCP client supporting Streamable HTTP

If the client supports custom HTTP headers, use the URL and `Authorization` header above. No `command`, no `args`, no `env` — this is a remote server. Consult the client's MCP documentation for the exact config syntax and file path.

## Verification

After configuration, ask the model to call `profile_get`. A successful response (an object with `id`, `email`, `first_name`, `timezone`, etc.) confirms both connectivity and authentication. A 401 means authentication failed: for method B the API key is missing or invalid; for method A the account session/token expired or was not completed — reconnect and sign in again.

## Documentation resources

The server exposes documentation as MCP Resources. When answering Wavix-specific questions, read these before relying on prior knowledge:

- `wavix://docs/*` — pages from <https://docs.wavix.com> (e.g. `wavix://docs/messaging/send-sms`, `wavix://docs/numbers/number-validator`).
- `wavix://api/openapi.yaml` — the full OpenAPI 3.0 spec.

List available resources via `resources/list` and read on demand. Resources are cached server-side; the user's Bearer token is **not** forwarded to documentation hosts.

## Safety guidance for agents

- Confirm with the user before calling destructive tools: `*_delete`, `my_numbers_delete` (releases numbers), `cart_checkout` (charges the account), `sms_and_mms_messages_send` to unverified recipients.
- Default to small page sizes (`per_page=10` … `25`) on `*_list` tools to keep context manageable.
- For bulk validation or large exports, prefer asynchronous variants (e.g. `number_validator_create_bulk` with `async=true`) and poll for results.
- Quote behavior from `wavix://docs/*` rather than inferring it — Wavix-specific semantics may differ from generic telecom assumptions.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| 401 Unauthorized | Authentication failed. Method B: missing/invalid `Authorization` header — check the API key is active in the Wavix Console. Method A: expired or incomplete sign-in — reconnect and sign in again. |
| Tool not found | Client tool-count limit hit (Cursor < 2.4). Upgrade the client. |
| Fewer tools available after account sign-in | Expected — account sign-in (method A) is scope-filtered: the user gets the tools their approved scopes and account role allow. An API key (method B) sees the full tool surface. Approve more scopes at sign-in, or use an API key with the needed scope groups. |
| 4xx with `errors` array | Validation error from Wavix API — read the `errors` array and the relevant `wavix://docs/*` page. |
| Cannot reach endpoint | Confirm DNS / network access to `mcp.wavix.com` on HTTPS (443). |

## Links

- Server documentation: <https://github.com/Wavix/wavix-mcp-server>
- Wavix product docs: <https://docs.wavix.com>
- Wavix API reference: <https://docs.wavix.com/api-reference>
- Support: <support@wavix.com>
