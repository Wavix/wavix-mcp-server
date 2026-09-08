"""Unit tests for the OAuth 2.1 protected-resource layer (PD-79)."""

import asyncio

import httpx
import pytest
from fastmcp import FastMCP
from starlette.testclient import TestClient

from wavix_mcp.auth import build_auth_provider, mcp_path

ISSUER = "https://app.test.example"
RESOURCE = "https://mcp.test.example"


@pytest.fixture
def oauth_env(monkeypatch):
    monkeypatch.setenv("OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("OAUTH_RESOURCE", RESOURCE)
    monkeypatch.delenv("MCP_PATH", raising=False)


def _client(auth) -> TestClient:
    spec = {"openapi": "3.1.0", "info": {"title": "t", "version": "1"}, "paths": {}}
    server = FastMCP.from_openapi(
        openapi_spec=spec,
        client=httpx.AsyncClient(base_url="https://api.test.example"),
        name="t",
        auth=auth,
    )
    return TestClient(server.http_app(path=mcp_path()))


def test_disabled_without_env(monkeypatch):
    monkeypatch.delenv("OAUTH_ISSUER", raising=False)
    monkeypatch.delenv("OAUTH_RESOURCE", raising=False)

    assert build_auth_provider() is None


def test_disabled_when_only_issuer_set(monkeypatch):
    monkeypatch.setenv("OAUTH_ISSUER", ISSUER)
    monkeypatch.delenv("OAUTH_RESOURCE", raising=False)

    assert build_auth_provider() is None


def test_trailing_slashes_do_not_double_up(monkeypatch):
    monkeypatch.setenv("OAUTH_ISSUER", f"{ISSUER}/")
    monkeypatch.setenv("OAUTH_RESOURCE", f"{RESOURCE}/")

    body = (
        _client(build_auth_provider())
        .get(f"/.well-known/oauth-protected-resource{mcp_path()}")
        .json()
    )

    assert body["resource"] == f"{RESOURCE}/mcp"
    assert body["authorization_servers"] == [ISSUER]


def test_prm_advertises_resource_and_authorization_server(oauth_env):
    response = _client(build_auth_provider()).get(
        f"/.well-known/oauth-protected-resource{mcp_path()}"
    )

    assert response.status_code == 200
    body = response.json()
    # The `resource` value is the audience the AS must stamp into `aud`;
    # a mismatch makes every authorization request fail as invalid_target.
    assert body["resource"] == f"{RESOURCE}/mcp"
    # RFC 8414 §3.3: must byte-match the AS `issuer` (host-only, no trailing slash),
    # which is also the `issuer` the JWTVerifier validates the token `iss` against.
    assert body["authorization_servers"] == [ISSUER]
    assert "account:read" in body["scopes_supported"]
    assert "offline_access" in body["scopes_supported"]


def test_unauthenticated_request_challenges_with_resource_metadata(oauth_env):
    response = _client(build_auth_provider()).post(
        mcp_path(),
        json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        headers={"Accept": "application/json, text/event-stream"},
    )

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert (
        f'resource_metadata="{RESOURCE}/.well-known/oauth-protected-resource{mcp_path()}"'
        in challenge
    )


def _initialize(auth, token: str | None):
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    # As a context manager so the app lifespan runs: a request that passes the
    # auth layer reaches FastMCP's session manager, which is only initialized
    # by the lifespan. Requests that 401 never get that far.
    with _client(auth) as client:
        return client.post(
            mcp_path(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            headers=headers,
        )


def test_non_jwt_token_is_forwarded_when_passthrough_enabled(oauth_env):
    # An API key is opaque, so the JWT verifier cannot validate it. Rejecting it
    # here would 401 every client that authenticated this way before OAuth
    # existed; instead the request proceeds and the upstream API judges the key.
    response = _initialize(build_auth_provider(), "0123456789abcdef" * 4)

    assert response.status_code == 200


def test_non_jwt_token_is_rejected_when_passthrough_disabled(oauth_env, monkeypatch):
    monkeypatch.setenv("OAUTH_API_KEY_PASSTHROUGH", "false")

    response = _initialize(build_auth_provider(), "0123456789abcdef" * 4)

    assert response.status_code == 401


def test_passthrough_still_challenges_a_request_with_no_token(oauth_env):
    # The fallback must not weaken discovery: a client that sends nothing still
    # has to be told where the authorization server is.
    response = _initialize(build_auth_provider(), None)

    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]


def test_verified_jwt_is_returned_unchanged_by_the_passthrough_wrapper():
    from fastmcp.server.auth.auth import AccessToken

    from wavix_mcp.auth import PASSTHROUGH_CLIENT_ID, _ApiKeyPassthroughVerifier

    issued = AccessToken(token="jwt", client_id="real-client", scopes=["numbers:read"])

    class _Inner:
        required_scopes = None

        async def verify_token(self, token):
            return issued

    verified = asyncio.run(_ApiKeyPassthroughVerifier(_Inner()).verify_token("jwt"))

    assert verified is issued
    assert verified.client_id != PASSTHROUGH_CLIENT_ID


def test_passthrough_marks_the_token_so_scope_filtering_skips_it():
    from wavix_mcp.auth import PASSTHROUGH_CLIENT_ID, _ApiKeyPassthroughVerifier

    class _Inner:
        required_scopes = None

        async def verify_token(self, token):
            return None

    token = asyncio.run(_ApiKeyPassthroughVerifier(_Inner()).verify_token("an-api-key"))

    assert token is not None
    assert token.token == "an-api-key"
    assert token.client_id == PASSTHROUGH_CLIENT_ID
    assert token.scopes == []


def test_mcp_path_override(monkeypatch):
    monkeypatch.setenv("MCP_PATH", "sse")

    assert mcp_path() == "/sse"
