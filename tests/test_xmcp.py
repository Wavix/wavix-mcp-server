"""Tests for the x-mcp consumer: spec x-mcp metadata → FastMCP annotations,
descriptions, and expose-based tool exclusion."""

import asyncio

import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType
from fastmcp.utilities.openapi.models import HTTPRoute

from wavix_mcp.server import _xmcp, _xmcp_component_fn, _xmcp_route_map_fn

XMCP_EXPOSED = {
    "title": "List things",
    "readOnly": True,
    "destructive": False,
    "openWorld": False,
    "expose": True,
    "description": "Lists things. Use to browse. No side effects. None.",
}


def _route(operation_id: str, xmcp: dict | None) -> HTTPRoute:
    extensions = {"x-mcp": xmcp} if xmcp is not None else {}
    return HTTPRoute(
        path="/things",
        method="GET",
        operation_id=operation_id,
        extensions=extensions,
        responses={},
    )


def _spec(*operations: tuple[str, str, dict | None]) -> dict:
    paths: dict = {}
    for path, op_id, xmcp in operations:
        op: dict = {"operationId": op_id, "responses": {"200": {"description": "ok"}}}
        if xmcp is not None:
            op["x-mcp"] = xmcp
        paths[path] = {"get": op}
    return {"openapi": "3.1.0", "info": {"title": "t", "version": "1"}, "paths": paths}


def _tools_by_name(spec: dict) -> dict:
    client = httpx.AsyncClient(base_url="http://api.test")
    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="t",
        route_map_fn=_xmcp_route_map_fn,
        mcp_component_fn=_xmcp_component_fn,
    )
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_xmcp_returns_dict_when_present():
    assert _xmcp(_route("things_list", XMCP_EXPOSED)) == XMCP_EXPOSED


def test_xmcp_returns_none_when_absent():
    assert _xmcp(_route("things_list", None)) is None


def test_xmcp_returns_none_when_not_a_dict():
    route = HTTPRoute(
        path="/things",
        method="GET",
        operation_id="things_list",
        extensions={"x-mcp": "nonsense"},
        responses={},
    )
    assert _xmcp(route) is None


def test_route_map_excludes_expose_false():
    xmcp = {**XMCP_EXPOSED, "expose": False}
    assert _xmcp_route_map_fn(_route("secret_get", xmcp), MCPType.TOOL) is MCPType.EXCLUDE


def test_route_map_keeps_expose_true():
    assert _xmcp_route_map_fn(_route("things_list", XMCP_EXPOSED), MCPType.TOOL) is None


def test_route_map_keeps_missing_expose():
    xmcp = {k: v for k, v in XMCP_EXPOSED.items() if k != "expose"}
    assert _xmcp_route_map_fn(_route("things_list", xmcp), MCPType.TOOL) is None


def test_route_map_noop_without_xmcp():
    assert _xmcp_route_map_fn(_route("things_list", None), MCPType.TOOL) is None


def test_component_sets_annotations_and_description():
    tool = _tools_by_name(_spec(("/things", "things_list", XMCP_EXPOSED)))["things_list"]
    assert tool.description == XMCP_EXPOSED["description"]
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.openWorldHint is False
    assert tool.annotations.title == "List things"
    # idempotentHint is intentionally not carried by x-mcp.
    assert tool.annotations.idempotentHint is None


def test_destructive_open_world_tool_annotated():
    xmcp = {
        "title": "Send SMS",
        "readOnly": False,
        "destructive": True,
        "openWorld": True,
        "expose": True,
        "description": "Sends an SMS. Use to message a recipient. Billable, irreversible. Opt-in required.",
    }
    tool = _tools_by_name(_spec(("/messages", "messages_send", xmcp)))["messages_send"]
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is True
    assert tool.annotations.openWorldHint is True


def test_expose_false_excludes_tool():
    hidden = {**XMCP_EXPOSED, "expose": False}
    tools = _tools_by_name(
        _spec(
            ("/things", "things_list", XMCP_EXPOSED),
            ("/secret", "secret_get", hidden),
        )
    )
    assert "things_list" in tools
    assert "secret_get" not in tools


def test_spec_without_xmcp_is_noop():
    tool = _tools_by_name(_spec(("/things", "things_list", None)))["things_list"]
    assert tool.annotations is None or tool.annotations.readOnlyHint is None
