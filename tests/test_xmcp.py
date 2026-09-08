"""Tests for the x-mcp consumer: expose-based exclusion, risk annotations from
x-mcp, tool title from x-mcp or the OpenAPI summary, description overridable via
x-mcp."""

import asyncio

import httpx
import pytest
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
    # Title falls back to the OpenAPI summary when x-mcp carries no title.
    assert tool.title == "List things"
    # Description stays the endpoint's own text when x-mcp carries no description.
    assert tool.description == "List things endpoint description."
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.openWorldHint is False
    assert tool.annotations.title == "List things"
    # idempotentHint is intentionally not carried by x-mcp.
    assert tool.annotations.idempotentHint is None


def test_xmcp_title_and_description_override_the_openapi_text():
    xmcp = {
        **XMCP,
        "title": "Browse things",
        "description": "Lists things. Use to browse. No side effects. None.",
    }
    tool = _tools_by_name(_spec(("/things", "things_list", "List things", xmcp)))["things_list"]
    assert tool.title == "Browse things"
    assert tool.description == "Lists things. Use to browse. No side effects. None."
    assert tool.annotations.title == "Browse things"


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


def test_overlay_description_overrides_spec_and_xmcp(monkeypatch):
    from wavix_mcp import server

    overlay = "Overlay text. Use to browse. Read-only; no side effects. Requires read scope."
    monkeypatch.setattr(server, "TOOL_DESCRIPTIONS", {"things_list": overlay})
    xmcp = {**XMCP, "description": "Spec x-mcp text that must lose to the overlay."}
    tool = _tools_by_name(_spec(("/things", "things_list", "List things", xmcp)))["things_list"]
    assert tool.description == overlay


def test_missing_overlay_entry_falls_back_to_xmcp_description(monkeypatch):
    from wavix_mcp import server

    monkeypatch.setattr(server, "TOOL_DESCRIPTIONS", {})
    xmcp = {**XMCP, "description": "Spec x-mcp fallback."}
    tool = _tools_by_name(_spec(("/things", "things_list", "List things", xmcp)))["things_list"]
    assert tool.description == "Spec x-mcp fallback."


def test_shipped_overlay_covers_sms_tools_with_nonempty_strings():
    from wavix_mcp.server import TOOL_DESCRIPTIONS

    expected = {
        "sms_and_mms_messages_send",
        "sms_and_mms_messages_list",
        "sms_and_mms_messages_get",
        "sms_and_mms_sender_ids_list",
        "sms_and_mms_sender_ids_create",
        "sms_and_mms_sender_ids_get",
        "sms_and_mms_sender_ids_delete",
        "sms_and_mms_opt_outs_list",
        "sms_and_mms_opt_outs_create",
    }
    assert expected <= set(TOOL_DESCRIPTIONS)
    assert all(isinstance(v, str) and v.strip() for v in TOOL_DESCRIPTIONS.values())


def test_parse_tool_descriptions_accepts_a_mapping():
    from wavix_mcp.server import _parse_tool_descriptions

    assert _parse_tool_descriptions("foo: bar", "x") == {"foo": "bar"}


def test_parse_tool_descriptions_rejects_non_mapping():
    from wavix_mcp.server import _parse_tool_descriptions

    with pytest.raises(RuntimeError):
        _parse_tool_descriptions("- a\n- b", "x")


def test_parse_tool_descriptions_rejects_empty_or_non_string_value():
    from wavix_mcp.server import _parse_tool_descriptions

    with pytest.raises(RuntimeError):
        _parse_tool_descriptions("foo: ''", "x")
    with pytest.raises(RuntimeError):
        _parse_tool_descriptions("foo:\n  nested: 1", "x")


def test_spec_operation_ids_collects_declared_ids():
    from wavix_mcp.server import _spec_operation_ids

    spec = _spec(
        ("/things", "things_list", "List things", XMCP),
        ("/others", "others_get", "Get other", XMCP),
    )
    assert _spec_operation_ids(spec) == {"things_list", "others_get"}


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
