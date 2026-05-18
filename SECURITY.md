# Security Policy

## Reporting a vulnerability

If you discover a security issue in the Wavix MCP server or anything documented in this repository, report it **privately** to <support@wavix.com> with the subject line:

```
Security: <short summary>
```

Please include:

- A clear description of the issue.
- Steps to reproduce or a proof-of-concept.
- The potential impact and any mitigations you suggest.
- Whether the issue is already public.

GitHub Issues are disabled on this repository regardless, but please also avoid disclosing the issue in public forks, social media, or any other public channel before we have shipped a fix.

## Acknowledgement and disclosure

Wavix will acknowledge receipt within two business days and provide a status update within seven business days. Fixes are deployed to the hosted MCP server at `https://mcp.wavix.com/mcp` without requiring action on the user's side.

We coordinate public disclosure with the reporter after a fix is deployed.

## Scope

**In scope:**

- Vulnerabilities affecting the hosted MCP server endpoint at `https://mcp.wavix.com/mcp`.
- Code-level vulnerabilities in this repository that affect both the hosted endpoint and self-hosted deployments built from `main` (auth bypass, token leakage, request smuggling, SSRF, dependency-level CVEs, etc.).
- Leakage or improper handling of the `Authorization` Bearer token.
- Inaccurate or unsafe guidance in this documentation (for example, advice that could cause a user to expose credentials).

**Out of scope:**

- Third-party MCP clients (Claude, Cursor, VS Code, Windsurf, etc.) — report those to their respective vendors.
- Issues in the upstream [FastMCP](https://github.com/jlowin/fastmcp) framework — report those upstream.
- The Wavix product itself outside the MCP server endpoint — use the standard Wavix support channel.
- **Self-hosted operational concerns** — keeping your image patched, TLS termination, network policy, secret storage, and reverse proxy hardening are the operator's responsibility.

## Supported versions

Only the **latest** commit on `main` and the live hosted server at `https://mcp.wavix.com/mcp` are supported. We do not backport fixes to historical commits or release branches; if you self-host, track `main` and rebuild on every release.
