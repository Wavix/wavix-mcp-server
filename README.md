# Wavix MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/Model_Context_Protocol-supported-blue.svg)](https://modelcontextprotocol.io)

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives LLMs and AI agents direct access to the **Wavix telecom platform** — SMS/MMS, voice calls, 2FA, SIP trunking, phone-number management, 10DLC registration, call recordings, speech analytics, and billing.

[Wavix](https://wavix.com) is a global communications platform for sending SMS, placing voice calls, and running 2FA flows over a single API. A [free trial](https://wavix.com) is available; paid usage follows the Wavix [pricing](https://wavix.com/pricing) plan attached to your account.

The fastest way to use this MCP server is the **hosted endpoint** at `https://mcp.wavix.com/mcp` — point any MCP-compatible client at it and authenticate with your Wavix API key. If you need to self-host (custom Wavix deployment, behind a firewall, dedicated instance), see [Run your own](#run-your-own).

## Table of contents

- [Endpoint](#endpoint)
- [Install](#install) — [one-click](#one-click-install), [Claude Code](#claude-code), [Claude Desktop / Web](#claude-desktop--claude-web), [Cursor](#cursor-manual), [VS Code](#vs-code-manual-github-copilot-chat), [Codex CLI](#codex-cli), [Windsurf](#windsurf--other-clients)
- [Run your own](#run-your-own) (self-host)
- [Examples](#examples)
- [Tools](#tools) → [full catalogue in TOOLS.md](TOOLS.md)
- [Resources](#resources)
- [Authentication](#authentication) ([best practices](#best-practices), [if a token is compromised](#if-a-token-is-compromised))
- [Troubleshooting](#troubleshooting)
- [Compatibility & limits](#compatibility--limits)
- [Support](#support), [Contributing](#contributing), [Security](#security), [License](#license)

## Endpoint

| Field | Value |
| --- | --- |
| URL | `https://mcp.wavix.com/mcp` |
| Transport | Streamable HTTP |
| Auth | `Authorization: Bearer <api_key>` |
| Tools | see [TOOLS.md](TOOLS.md) |
| Resources | Wavix docs + OpenAPI spec (auto-discovered) |

Get a Wavix API key from the [Wavix Console](https://wavix.com) → **Administration → API keys → Create new**.

## Install

**Before you start:** grab your Wavix API key.

1. Sign in at <https://wavix.com>.
2. Open **Administration → API keys**.
3. Click **Create new** (or copy an existing key). Keep it handy — you'll paste it in place of `YOUR_API_KEY` below.

### One-click install

> ⚠️ **The buttons below seed your editor's MCP config with a placeholder token `YOUR_API_KEY`.** After the editor finishes installing, open the generated config and replace the placeholder with your real API key before sending any request — otherwise every call will return `401 Unauthorized`.

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install-007ACC?style=flat-square&logo=visualstudiocode&logoColor=white)](vscode:mcp/install?%7B%22name%22%3A%22wavix%22%2C%22type%22%3A%22http%22%2C%22url%22%3A%22https%3A//mcp.wavix.com/mcp%22%2C%22headers%22%3A%7B%22Authorization%22%3A%22Bearer%20YOUR_API_KEY%22%7D%7D)
[![Install in Cursor](https://img.shields.io/badge/Cursor-Install-000000?style=flat-square&logo=cursor&logoColor=white)](cursor://anysphere.cursor-deeplink/mcp/install?name=wavix&config=eyJ1cmwiOiJodHRwczovL21jcC53YXZpeC5jb20vbWNwIiwiaGVhZGVycyI6eyJBdXRob3JpemF0aW9uIjoiQmVhcmVyIFlPVVJfQVBJX0tFWSJ9fQ==)

**To remove later:** open the same config file (`~/.cursor/mcp.json`, `.vscode/mcp.json`, or the equivalent for your editor) and delete the `wavix` entry, or remove the connector through your editor's MCP / Connectors UI.

### Claude Code

```bash
claude mcp add --transport http wavix https://mcp.wavix.com/mcp \
  --header "Authorization: Bearer YOUR_API_KEY"
```

Use `claude mcp list` to verify and `/mcp` inside a session for status.

### Claude Desktop / Claude Web

Settings → **Connectors** → **Add custom connector**:

- Name: `Wavix`
- URL: `https://mcp.wavix.com/mcp`
- Transport: `Streamable HTTP`
- Authentication header: `Authorization: Bearer <api_key>`

### Cursor (manual)

Add to `~/.cursor/mcp.json` (or project-level `.cursor/mcp.json`):

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

Cursor 2.4+ exposes the full catalogue; earlier versions cap at 40.

### VS Code (manual, GitHub Copilot Chat)

Create `.vscode/mcp.json` in your workspace (or add the same `servers` object under the `"mcp"` key in user `settings.json`):

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

See the [VS Code MCP servers guide](https://code.visualstudio.com/docs/copilot/chat/mcp-servers) for the up-to-date schema.

### Codex CLI

Codex CLI supports MCP over stdio. Bridge to the hosted server via [`mcp-remote`](https://www.npmjs.com/package/mcp-remote). Edit `~/.codex/config.toml`:

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
WAVIX_API_KEY = "YOUR_API_KEY"
```

### Windsurf / other clients

Any MCP client that supports **Streamable HTTP** transport with custom headers will work. Use:

- URL: `https://mcp.wavix.com/mcp`
- Header: `Authorization: Bearer <api_key>`

**Setting up via an AI agent?** Point your agent at [`llms-install.md`](llms-install.md) — it's a machine-readable install guide that gives the model the URL, header, and per-client configuration in a deterministic format so it doesn't improvise endpoint values.

## Run your own

The hosted server works out of the box for most users. Self-host if you need to point at a non-public Wavix deployment, run behind a firewall, or operate inside your own infrastructure.

### Docker

```bash
docker build -t wavix-mcp-server .
docker run --rm -p 8000:8000 wavix-mcp-server
```

The server listens on port 8000 and exposes the MCP endpoint at `/mcp`. Point your client at `http://<host>:8000/mcp`.

### From source

```bash
git clone https://github.com/Wavix/wavix-mcp-server.git
cd wavix-mcp-server
pip install -e .
wavix-mcp
```

Requires Python 3.10+.

### Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `WAVIX_API_BASE_URL` | `https://api.wavix.com` | Override the upstream Wavix API endpoint (for internal deployments or staging) |

No Wavix credentials are required to **run** the server — they are forwarded per-request from the MCP client's `Authorization: Bearer <api_key>` header. Self-hosters are responsible for terminating TLS in front of the server (nginx, Caddy, cloud load balancer) before exposing it publicly.

## Examples

Concrete prompts you can drop into any connected client.

> Phone numbers below (`+1 310 555 0100`, `+44 7700 900123`) are in reserved test ranges (NANP `555` and Ofcom `070 09xx`) — safe to copy verbatim, no real subscribers are reachable through them.

### Send a transactional SMS

> *Prompt:* "Send an SMS from +13105550100 to +447700900123 saying 'Your verification code is 4821'."

The agent calls `sms_and_mms_messages_send` with `from`, `to`, and `text`. Returns the message ID and delivery status.

### Run a 2FA verification

> *Prompt:* "Send a 2FA verification code to +13105550100 via SMS. When I give you the code I receive, check whether it's correct."

The agent calls `two_fa_verification_create`, waits for you to share the code that arrives via SMS, then calls `two_fa_verification_check`. Useful for prototyping passwordless flows without writing integration code.

### Find and buy a phone number

> *Prompt:* "Find an available US toll-free number with SMS capability, add it to my cart, and check out."

The agent chains `buy_numbers_list` (filtering by country and feature), `cart_add`, and `cart_checkout`. Confirm with the user before checkout — it charges the account.

### Search call transcripts

> *Prompt:* "Show me all inbound calls from yesterday longer than two minutes where the caller mentioned 'refund'."

The agent uses `cdrs_search` against transcriptions, then enriches each result via `cdrs_get` for full call metadata.

### Pull a recording and transcribe it

> *Prompt:* "Get the recording for call abc-123, ask Wavix to transcribe it, and return the transcription."

The agent calls `call_recording_get` (returns a pre-signed download URL), `cdrs_retranscribe`, then polls `cdrs_transcription_get`.

### Audit billing

> *Prompt:* "How much did we spend on SMS last month? Give me a download link for the most recent invoice PDF."

The agent calls `billing_transactions_list` filtered by type and date, then `billing_invoices_list` + `billing_invoices_download`. The download tool returns a **pre-signed URL** to the PDF, not the file itself — open the URL in a browser or pass it to your client to fetch the actual document.

## Tools

<!-- tools:start -->
122 tools, generated from the [Wavix OpenAPI spec](https://github.com/Wavix/wavix-openapi). Arguments mirror request parameters and body fields.

| Group | # | Coverage |
| --- | ---: | --- |
| [SMS and MMS](TOOLS.md#sms-and-mms) | 10 | Send, list, retrieve messages; sender IDs; opt-outs |
| [Call control](TOOLS.md#call-control) | 9 | Start / answer / end calls; play audio; collect DTMF |
| [Call recording](TOOLS.md#call-recording) | 3 | List, download (pre-signed URL), delete |
| [Call streaming](TOOLS.md#call-streaming) | 2 | Start / stop media stream |
| [Call webhooks](TOOLS.md#call-webhooks) | 3 | List, create, delete |
| [CDRs](TOOLS.md#cdrs) | 6 | List, export, retrieve; transcription search and retranscribe |
| [Speech Analytics](TOOLS.md#speech-analytics) | 4 | Upload, transcribe, retrieve original file |
| [2FA](TOOLS.md#2fa) | 6 | Create / check / cancel / resend verification; events |
| [My numbers](TOOLS.md#my-numbers) | 7 | List, update, release; SMS / voice routing; document upload |
| [Buy](TOOLS.md#buy) | 5 | Countries, regions, cities; available number search |
| [Cart](TOOLS.md#cart) | 4 | Add, remove, retrieve, checkout |
| [Number validator](TOOLS.md#number-validator) | 3 | Single and bulk validation |
| [SIP trunks](TOOLS.md#sip-trunks) | 5 | Full CRUD |
| [10DLC](TOOLS.md#10dlc) | 30 | Brands, campaigns, vetting, evidence, event subscriptions |
| [Profile](TOOLS.md#profile) | 3 | Get / update profile; account config |
| [API Keys](TOOLS.md#api-keys) | 5 | List, create, activate / deactivate, delete |
| [Sub-accounts](TOOLS.md#sub-accounts) | 5 | List, create, get, update; transactions |
| [Billing](TOOLS.md#billing) | 3 | Transactions, invoices, statement download |
| [Voice campaigns](TOOLS.md#voice-campaigns) | 2 | Trigger and retrieve |
| [Wavix Embeddable (WebRTC)](TOOLS.md#wavix-embeddable-webrtc) | 5 | Widget tokens CRUD |
| [Link shortener](TOOLS.md#link-shortener) | 2 | Create short links; metrics |

<!-- tools:end -->

See **[TOOLS.md](TOOLS.md)** for the complete tool list with one-line descriptions. The authoritative source is the [Wavix OpenAPI spec](https://github.com/Wavix/wavix-openapi) — your client always sees the current live catalogue.

## Resources

In addition to tools, the server exposes Wavix documentation as MCP **Resources**, so the model can pull authoritative context on demand instead of guessing from prior knowledge.

| URI scheme | Contents |
| --- | --- |
| `wavix://docs/<path>` | Documentation pages from [docs.wavix.com](https://docs.wavix.com) (auto-discovered via [`llms.txt`](https://llmstxt.org/)). |
| `wavix://api/openapi.yaml` | The full Wavix OpenAPI 3.0 specification. |

Both sources — [docs.wavix.com](https://docs.wavix.com) and the [Wavix OpenAPI spec](https://github.com/Wavix/wavix-openapi) — are publicly available and can be browsed directly without authentication.

Resources are fetched lazily on `resources/read` and cached server-side with a 1-hour TTL. The upstream Bearer token is **never** forwarded to documentation hosts — only to `api.wavix.com`.

## Authentication

Every request from the client must include:

```
Authorization: Bearer <api_key>
```

The server forwards this header to `api.wavix.com` per-request. The token:

- is **never** logged,
- is **never** forwarded on cross-host redirects (e.g. pre-signed S3 download URLs),
- is **never** sent to documentation hosts.

If your client follows a pre-signed download URL returned by `call_recording_get`, `billing_invoices_download`, `speech_analytics_file_get`, or `ten_dlc_brand_evidence_get`, fetch it directly without the `Authorization` header.

### Best practices

- **Use a dedicated API key for MCP.** Create a separate API key at <https://wavix.com> → **Administration → API keys** (or via the `api_keys_create` tool itself, from another session). This lets you revoke MCP access without disrupting other integrations.
- **Rotate periodically.** Treat the API key like any production secret: rotate on schedule and on any suspicion of leakage.
- **Keep API keys out of git.** MCP client configs are easy to commit by accident, taking the token with them into history and CI logs. Most clients support `${env:VAR}` substitution in the header value — store the API key in an env var or your OS keychain and reference it from the config. As a safety net, add the common client-config paths to your project's `.gitignore`:

  ```gitignore
  .cursor/mcp.json
  .vscode/mcp.json
  claude_desktop_config.json
  .claude/mcp.json
  .codex/config.toml
  ```

### If a token is compromised

1. In the Wavix Console, deactivate the key immediately (or call `api_keys_deactivate`).
2. Create a replacement via `api_keys_create` or the Console.
3. Update the client's config and reconnect.
4. Review `billing_transactions_list` and `cdrs_list` for unexpected activity.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `401 Unauthorized` from any tool | Missing or invalid `Authorization: Bearer …` header. Verify the API key is active in the Wavix Console. |
| Tool returns a `download_url`, not the file itself | Expected. Recording, invoice, speech-analytics, and 10DLC evidence endpoints return pre-signed URLs (see [Authentication](#authentication)). Fetch the URL directly without the `Authorization` header. |
| Client only shows ~40 tools, not the full catalogue | Older clients enforce a per-server tool cap. Upgrade (Cursor 2.4+, latest VS Code, latest Claude). |
| `Tool not found` for a tool listed in this README | The local client may be caching an old tool list. Restart the client, or remove and re-add the server. |
| 4xx with an `errors` array | Validation error from Wavix API. Inspect `errors`; cross-reference the relevant `wavix://docs/*` page or the OpenAPI spec. |
| Cannot reach the server | Confirm DNS and outbound HTTPS to `mcp.wavix.com:443`. |
| Agent calls a destructive tool unexpectedly | Most clients can require confirmation before tool calls — enable that setting and rotate to a dedicated MCP API key (see [Best practices](#best-practices)). |

## Compatibility & limits

- Compatible with any MCP client supporting **Streamable HTTP** transport (Claude Desktop / Web / Code, Cursor 2.4+, VS Code, Windsurf, custom MCP SDKs) and any agent framework with an MCP client adapter.
- Older clients may enforce a per-server tool cap; upgrade to a recent version to access the full catalogue.
- Rate limits and usage charges follow your Wavix account plan. See [Wavix pricing](https://wavix.com/pricing).

## Changelog

The hosted server is updated continuously as the Wavix OpenAPI spec evolves; new tools appear automatically and existing tool arguments may gain optional fields. Documentation changes for this repository are tracked under [Releases](https://github.com/Wavix/wavix-mcp-server/releases). For substantial behavior changes affecting tool inputs or auth, we will publish a notice both there and in the [Wavix release notes](https://docs.wavix.com/release-notes).

## Support

- Product docs: <https://docs.wavix.com>
- API reference: <https://docs.wavix.com/api-reference>
- Questions / feedback: <support@wavix.com>

## Contributing

This repository is **source-available** but **not open to external contributions**. Pull requests are auto-closed, and Issues / Discussions are disabled. Send bug reports, feature requests, and feedback to <support@wavix.com>. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for details.

If you find a bug in the underlying [FastMCP](https://github.com/jlowin/fastmcp) framework, please report it upstream there.

## Security

To report a security vulnerability, please email <support@wavix.com> with the subject `Security: <short summary>` rather than opening a public issue. See [`SECURITY.md`](SECURITY.md) for details.

## License

[MIT](LICENSE) © Wavix
