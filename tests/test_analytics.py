"""Baseline `posthog.mcp.instrument()` wiring: one event per tool call, no
schema change, and no behavior change when PostHog is unset or unreachable."""

import asyncio
import time

import httpx
import mcp.types as mcp_types
from fastmcp import Client, FastMCP

import wavix_mcp.analytics as analytics

_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/v1/ping": {
            "get": {
                "operationId": "ping_tool",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
        "/v1/boom": {
            "get": {
                "operationId": "boom_tool",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
    },
}


class _Api:
    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/boom":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"ok": True})


class _FakePosthog:
    def __init__(self) -> None:
        self.captured: list[dict] = []

    def capture(self, event, **kwargs) -> None:
        self.captured.append({"event": event, **kwargs})


def _instrumented_server(
    monkeypatch,
    *,
    api_key: str | None = "phc_test",
    posthog_fake: "_FakePosthog | None" = None,
    base_url: str = "http://api.test",
) -> FastMCP:
    if api_key is None:
        monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    else:
        monkeypatch.setenv("POSTHOG_API_KEY", api_key)
    if posthog_fake is not None:
        monkeypatch.setattr(analytics, "Posthog", lambda *a, **k: posthog_fake)
    client = httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(_Api()))
    mcp = FastMCP.from_openapi(openapi_spec=_SPEC, client=client, name="t")
    # instrument()'s own captures fire via asyncio.create_task and are not awaited
    # inline (posthog==7.83.1 posthog/mcp/_instrumentation.py fire_and_forget), so a
    # caller that asserts on them right after a tool call must drain them first via
    # the returned handle's flush() — stashed here since _instrumented_server only
    # returns the FastMCP instance.
    mcp._wavix_analytics_handle = analytics.instrument(mcp, base_url=base_url)
    return mcp


CLIENT_INFO = mcp_types.Implementation(name="pytest-client", version="9.9")


async def _call(mcp: FastMCP, name: str, arguments: dict):
    async with Client(mcp, client_info=CLIENT_INFO) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False)
    handle = getattr(mcp, "_wavix_analytics_handle", None)
    if handle is not None:
        await handle.flush()
    return result


async def _list_tools(mcp: FastMCP):
    async with Client(mcp, client_info=CLIENT_INFO) as client:
        return await client.list_tools()


def test_one_tool_call_captures_one_event_with_core_properties(monkeypatch):
    monkeypatch.setenv("POSTHOG_URL", "http://ingest.test")
    fake = _FakePosthog()
    mcp = _instrumented_server(monkeypatch, posthog_fake=fake)

    asyncio.run(_call(mcp, "ping_tool", {}))

    assert len(fake.captured) == 1
    event = fake.captured[0]
    assert event["event"] == "$mcp_tool_call"
    properties = event["properties"]
    assert properties["$mcp_tool_name"] == "ping_tool"
    assert "$mcp_duration_ms" in properties
    assert properties["$mcp_is_error"] is False
    assert properties["$mcp_client_name"] == "pytest-client"
    assert properties["$mcp_client_version"] == "9.9"
    assert "$session_id" in properties


def test_instrumentation_adds_no_schema_fields(monkeypatch):
    tools_on = asyncio.run(
        _list_tools(_instrumented_server(monkeypatch, posthog_fake=_FakePosthog()))
    )
    tools_off = asyncio.run(_list_tools(_instrumented_server(monkeypatch, api_key=None)))

    assert [(t.name, t.inputSchema, t.outputSchema) for t in tools_on] == [
        (t.name, t.inputSchema, t.outputSchema) for t in tools_off
    ]


def test_no_posthog_api_key_disables_analytics_entirely(monkeypatch):
    monkeypatch.setattr(
        analytics,
        "Posthog",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Posthog() must not be constructed")),
    )
    mcp = _instrumented_server(monkeypatch, api_key=None)

    result = asyncio.run(_call(mcp, "ping_tool", {}))

    assert result.is_error is False


def test_unreachable_posthog_host_does_not_delay_a_tool_call(monkeypatch):
    monkeypatch.setenv("POSTHOG_URL", "http://127.0.0.1:1")
    mcp = _instrumented_server(monkeypatch, posthog_fake=None)

    start = time.monotonic()
    result = asyncio.run(_call(mcp, "ping_tool", {}))
    elapsed = time.monotonic() - start

    assert result.is_error is False
    assert elapsed < 2.0
