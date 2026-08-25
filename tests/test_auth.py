"""Unit tests for the OAuth 2.1 protected-resource layer (PD-79)."""

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


def test_prm_advertises_resource_and_authorization_server(oauth_env):
    response = _client(build_auth_provider()).get(
        f"/.well-known/oauth-protected-resource{mcp_path()}"
    )

    assert response.status_code == 200
    body = response.json()
    # The `resource` value is the audience the AS must stamp into `aud`;
    # a mismatch makes every authorization request fail as invalid_target.
    assert body["resource"] == f"{RESOURCE}/mcp"
    assert body["authorization_servers"] == [f"{ISSUER}/"]
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


def test_garbage_token_is_rejected(oauth_env):
    response = _client(build_auth_provider()).post(
        mcp_path(),
        json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer not-a-jwt",
        },
    )

    assert response.status_code == 401


def test_mcp_path_override(monkeypatch):
    monkeypatch.setenv("MCP_PATH", "sse")

    assert mcp_path() == "/sse"
