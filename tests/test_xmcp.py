"""Tests for the x-mcp consumer: expose-based exclusion + risk annotations from
x-mcp, tool title from the OpenAPI summary, description from the endpoint."""

import asyncio

import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap
from fastmcp.utilities.openapi.models import HTTPRoute

from wavix_mcp.server import _xmcp, _xmcp_component_fn, _xmcp_route_map_fn

XMCP = {"readOnly": True, "destructive": False, "openWorld": False, "expose": True}


def _route(operation_id: str, xmcp: dict | None) -> HTTPRoute:
    extensions = {"x-mcp": xmcp} if xmcp is not None else {}
    return HTTPRoute(
        path="/things",
        method="GET",
        operation_id=operation_id,
        summary="List things",
        extensions=extensions,
        responses={},
    )


def _spec(*operations: tuple[str, str, str, dict | None]) -> dict:
    paths: dict = {}
    for path, op_id, summary, xmcp in operations:
        op: dict = {
            "operationId": op_id,
            "summary": summary,
            "description": f"{summary} endpoint description.",
            "responses": {"200": {"description": "ok"}},
        }
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
    assert _xmcp(_route("things_list", XMCP)) == XMCP


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
    xmcp = {**XMCP, "expose": False}
    assert _xmcp_route_map_fn(_route("secret_get", xmcp), MCPType.TOOL) is MCPType.EXCLUDE


def test_route_map_keeps_expose_true():
    assert _xmcp_route_map_fn(_route("things_list", XMCP), MCPType.TOOL) is None


def test_route_map_keeps_missing_expose():
    xmcp = {k: v for k, v in XMCP.items() if k != "expose"}
    assert _xmcp_route_map_fn(_route("things_list", xmcp), MCPType.TOOL) is None


def test_route_map_noop_without_xmcp():
    assert _xmcp_route_map_fn(_route("things_list", None), MCPType.TOOL) is None


def test_component_title_from_summary_annotations_from_xmcp():
    tool = _tools_by_name(_spec(("/things", "things_list", "List things", XMCP)))["things_list"]
    # Title falls back to the OpenAPI summary, not an x-mcp field.
    assert tool.title == "List things"
    # Description stays the endpoint's own text (FastMCP default), not duplicated.
    assert tool.description == "List things endpoint description."
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.openWorldHint is False
    assert tool.annotations.title == "List things"
    # idempotentHint is intentionally not carried by x-mcp.
    assert tool.annotations.idempotentHint is None


def test_destructive_open_world_tool_annotated():
    xmcp = {"readOnly": False, "destructive": True, "openWorld": True, "expose": True}
    tool = _tools_by_name(_spec(("/messages", "messages_send", "Send SMS", xmcp)))["messages_send"]
    assert tool.title == "Send SMS"
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is True
    assert tool.annotations.openWorldHint is True


def test_expose_false_excludes_tool():
    hidden = {**XMCP, "expose": False}
    tools = _tools_by_name(
        _spec(
            ("/things", "things_list", "List things", XMCP),
            ("/secret", "secret_get", "Get secret", hidden),
        )
    )
    assert "things_list" in tools
    assert "secret_get" not in tools


def test_spec_without_xmcp_derives_hints_from_the_http_method():
    tools = _tools_by_name(_spec(("/things", "things_list", "List things", None)))
    tool = tools["things_list"]
    # Title and description still come from the operation...
    assert tool.title == "List things"
    assert tool.description == "List things endpoint description."
    # ...and the hints fall back to the method rather than staying unset.
    # This assertion used to require annotations to be None. It changed because
    # no operation in the published spec carries x-mcp (0 of 122), so leaving
    # them unset shipped 116 tools with no hints at all — which Anthropic's
    # connector review rejects outright.
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False


def test_static_route_map_exclusion_takes_precedence():
    spec = _spec(("/things", "things_list", "List things", XMCP))
    client = httpx.AsyncClient(base_url="http://api.test")
    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="t",
        route_maps=[RouteMap(methods=["GET"], pattern=r"^/things$", mcp_type=MCPType.EXCLUDE)],
        route_map_fn=_xmcp_route_map_fn,
        mcp_component_fn=_xmcp_component_fn,
    )
    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "things_list" not in tools
