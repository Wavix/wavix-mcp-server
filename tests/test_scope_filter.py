"""Tool-list filtering and call gating by OAuth scope (DEV-11668)."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext

from wavix_mcp import scope_filter
from wavix_mcp.scope_filter import ScopeFilterMiddleware
from wavix_mcp.server import _TOOL_ROUTES, _xmcp_component_fn

TOOL_ROUTES = {
    "my_numbers_list": ("GET", "/v1/mydids"),
    "buy_number": ("POST", "/v1/buy"),
    "send_sms": ("POST", "/v2/messages"),
}


def _middleware():
    return ScopeFilterMiddleware(dict(TOOL_ROUTES))


def _tools(*names):
    return [SimpleNamespace(name=n) for n in names]


def _token(*granted, claim_scope=None):
    return SimpleNamespace(
        scopes=list(granted), claims={"scope": claim_scope} if claim_scope else {}
    )


ALL_TOOLS = ("my_numbers_list", "buy_number", "send_sms", "docs_helper")


def _list_tools(monkeypatch, token):
    monkeypatch.setattr(scope_filter, "get_access_token", lambda: token)

    async def call_next(_ctx):
        return _tools(*ALL_TOOLS)

    ctx = MiddlewareContext(message=SimpleNamespace())
    result = asyncio.run(_middleware().on_list_tools(ctx, call_next))
    return {t.name for t in result}


def _call_tool(monkeypatch, token, tool_name):
    monkeypatch.setattr(scope_filter, "get_access_token", lambda: token)

    async def call_next(_ctx):
        return "called"

    ctx = MiddlewareContext(message=SimpleNamespace(name=tool_name))
    return asyncio.run(_middleware().on_call_tool(ctx, call_next))


def test_no_token_lists_every_tool(monkeypatch):
    assert _list_tools(monkeypatch, None) == set(ALL_TOOLS)


def test_read_only_grant_hides_write_tools(monkeypatch):
    assert _list_tools(monkeypatch, _token("numbers:read")) == {"my_numbers_list", "docs_helper"}


def test_write_grant_shows_read_and_write_of_that_group(monkeypatch):
    assert _list_tools(monkeypatch, _token("numbers:write")) == {
        "my_numbers_list",
        "buy_number",
        "docs_helper",
    }


def test_ungranted_group_is_hidden(monkeypatch):
    assert "send_sms" not in _list_tools(monkeypatch, _token("numbers:write"))


def test_unrouted_tool_stays_visible(monkeypatch):
    assert "docs_helper" in _list_tools(monkeypatch, _token("numbers:read"))


def test_scope_from_claim_string_is_honored(monkeypatch):
    token = _token(claim_scope="messages:write numbers:read")
    assert _list_tools(monkeypatch, token) == {"my_numbers_list", "send_sms", "docs_helper"}


def test_call_denied_for_ungranted_scope(monkeypatch):
    with pytest.raises(ToolError, match="numbers:write"):
        _call_tool(monkeypatch, _token("numbers:read"), "buy_number")


def test_call_allowed_for_granted_scope(monkeypatch):
    assert _call_tool(monkeypatch, _token("numbers:write"), "buy_number") == "called"


def test_call_passthrough_without_token(monkeypatch):
    assert _call_tool(monkeypatch, None, "buy_number") == "called"


def test_call_to_unrouted_tool_is_allowed(monkeypatch):
    assert _call_tool(monkeypatch, _token("numbers:read"), "docs_helper") == "called"


_E2E_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/v1/mydids": {
            "get": {"operationId": "my_numbers_list", "responses": {"200": {"description": "ok"}}}
        },
        "/v1/buy": {
            "post": {"operationId": "buy_number", "responses": {"200": {"description": "ok"}}}
        },
        "/v2/messages": {
            "post": {"operationId": "send_sms", "responses": {"200": {"description": "ok"}}}
        },
    },
}


def test_end_to_end_filters_through_generated_server(monkeypatch):
    _TOOL_ROUTES.clear()
    mcp = FastMCP.from_openapi(
        openapi_spec=_E2E_SPEC,
        client=httpx.AsyncClient(base_url="http://api.test"),
        name="t",
        mcp_component_fn=_xmcp_component_fn,
    )
    mcp.add_middleware(ScopeFilterMiddleware(dict(_TOOL_ROUTES)))
    monkeypatch.setattr(scope_filter, "get_access_token", lambda: _token("numbers:read"))

    async def visible():
        async with Client(mcp) as client:
            return {t.name for t in await client.list_tools()}

    assert asyncio.run(visible()) == {"my_numbers_list"}
