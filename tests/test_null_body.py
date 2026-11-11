"""An explicit null in a confirmed update reaches the API and shows in the preview."""

import asyncio
import json

import httpx
from fastmcp import Client, FastMCP

from wavix_mcp.confirmation import ConfirmedTool, preview_builders
from wavix_mcp.server import _normalize_tool_schemas, _xmcp_component_fn, _xmcp_route_map_fn

_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/v1/mydids/{id}": {
            "patch": {
                "operationId": "my_numbers_update",
                "summary": "Update a number",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "sms_enabled": {"type": "boolean"},
                                    "sms_relay_url": {"type": ["string", "null"]},
                                    "call_status_url": {"type": "string"},
                                },
                            }
                        }
                    }
                },
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


def _ungated(method: str, operation_id: str, path: str) -> dict:
    operation = {
        "operationId": operation_id,
        "summary": operation_id,
        "x-mcp": {"readOnly": False, "destructive": False},
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"label": {"type": ["string", "null"]}},
                    }
                }
            }
        },
        "responses": {"200": {"description": "ok"}},
    }
    return {path: {method: operation}}


_SPEC["paths"].update(_ungated("patch", "things_update", "/v1/things"))
_SPEC["paths"].update(_ungated("post", "things_create", "/v1/things/new"))


def _server(requests: list[httpx.Request]) -> FastMCP:
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(base_url="http://api.test", transport=httpx.MockTransport(respond))
    return FastMCP.from_openapi(
        openapi_spec=_normalize_tool_schemas(_SPEC),
        client=client,
        name="t",
        route_map_fn=_xmcp_route_map_fn,
        mcp_component_fn=_xmcp_component_fn,
    )


def _sent_body(arguments: dict, tool: str = "my_numbers_update") -> dict:
    requests: list[httpx.Request] = []
    tool_arguments = {"id": 7, **arguments} if tool == "my_numbers_update" else arguments

    async def call() -> None:
        async with Client(_server(requests)) as client:
            await client.call_tool(tool, tool_arguments)

    asyncio.run(call())
    return json.loads(requests[0].content) if requests[0].content else {}


def test_null_is_sent_by_an_update_tool_without_confirmation():
    assert _sent_body({"label": None}, "things_update") == {"label": None}


def test_null_is_dropped_by_a_create_tool():
    assert _sent_body({"label": None}, "things_create") == {}


def test_null_for_nullable_field_is_sent():
    assert _sent_body({"sms_relay_url": None}) == {"sms_relay_url": None}


def test_null_for_non_nullable_field_is_sent_so_the_api_can_reject_it():
    assert _sent_body({"call_status_url": None}) == {"call_status_url": None}


def test_omitted_fields_are_not_added():
    assert _sent_body({"sms_enabled": True}) == {"sms_enabled": True}


def _preview(clearable: frozenset[str], arguments: dict) -> str:
    tool = ConfirmedTool("Update a number", ("id",), clearable)
    build = preview_builders({"example_update": tool}, {})["example_update"]
    return asyncio.run(build(arguments, httpx.AsyncClient()))


def test_preview_lists_a_clearable_field_sent_as_null():
    text = _preview(frozenset({"sms_relay_url"}), {"id": 7, "sms_relay_url": None})
    assert "sms_relay_url: null" in text


def test_preview_omits_a_non_clearable_field_sent_as_null():
    text = _preview(frozenset({"sms_relay_url"}), {"id": 7, "call_status_url": None})
    assert "call_status_url" not in text
