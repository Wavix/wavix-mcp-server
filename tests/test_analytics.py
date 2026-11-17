"""Baseline `posthog.mcp.instrument()` wiring: one event per tool call, no
schema change, and no behavior change when PostHog is unset or unreachable."""

import asyncio
import time
from types import SimpleNamespace

import httpx
import mcp.types as mcp_types
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport

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
        "/v1/echo": {
            "get": {
                "operationId": "echo_tool",
                "parameters": [
                    {"name": "x", "in": "query", "required": True, "schema": {"type": "string"}}
                ],
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
        self.shutdown_called = False

    def capture(self, event, **kwargs) -> None:
        self.captured.append({"event": event, **kwargs})

    def shutdown(self) -> None:
        self.shutdown_called = True


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

    tool_call_events = [e for e in fake.captured if e["event"] == "$mcp_tool_call"]
    assert len(tool_call_events) == 1
    properties = tool_call_events[0]["properties"]
    assert properties["$mcp_tool_name"] == "ping_tool"
    assert "$mcp_duration_ms" in properties
    assert properties["$mcp_is_error"] is False
    assert properties["$mcp_client_name"] == "pytest-client"
    assert properties["$mcp_client_version"] == "9.9"
    assert "$session_id" in properties
    # The adapter's own session-level events stay on: a client that connects
    # or lists tools but never calls one would otherwise be invisible.
    assert any(e["event"] == "$mcp_initialize" for e in fake.captured)


def test_instrumentation_adds_no_schema_fields(monkeypatch):
    tools_on = asyncio.run(
        _list_tools(_instrumented_server(monkeypatch, posthog_fake=_FakePosthog()))
    )
    tools_off = asyncio.run(_list_tools(_instrumented_server(monkeypatch, api_key=None)))

    assert [(t.name, t.inputSchema, t.outputSchema) for t in tools_on] == [
        (t.name, t.inputSchema, t.outputSchema) for t in tools_off
    ]


def test_instrumentation_adds_no_result_fields(monkeypatch):
    result_on = asyncio.run(
        _call(_instrumented_server(monkeypatch, posthog_fake=_FakePosthog()), "ping_tool", {})
    )
    result_off = asyncio.run(
        _call(_instrumented_server(monkeypatch, api_key=None), "ping_tool", {})
    )

    assert [c.model_dump() for c in result_on.content] == [
        c.model_dump() for c in result_off.content
    ]
    assert result_on.structured_content == result_off.structured_content
    assert result_on.data == result_off.data
    assert result_on.meta == result_off.meta
    assert result_on.is_error == result_off.is_error


def test_no_posthog_api_key_disables_analytics_entirely(monkeypatch):
    # A spy, not a throwing stub: a broad `except Exception` around
    # `instrument()`'s init (degrade-gracefully-on-misconfiguration) would
    # swallow a raised sentinel and still look like "never constructed".
    # Recording the call makes the assertion immune to that.
    calls: list[tuple] = []
    monkeypatch.setattr(analytics, "Posthog", lambda *a, **k: calls.append((a, k)))
    mcp = _instrumented_server(monkeypatch, api_key=None)

    assert mcp._wavix_analytics_handle is None
    assert calls == []

    result = asyncio.run(_call(mcp, "ping_tool", {}))

    assert result.is_error is False
    assert calls == []


def test_unreachable_posthog_host_does_not_delay_a_tool_call(monkeypatch):
    monkeypatch.setenv("POSTHOG_URL", "http://127.0.0.1:1")
    mcp = _instrumented_server(monkeypatch, posthog_fake=None)

    start = time.monotonic()
    result = asyncio.run(_call(mcp, "ping_tool", {}))
    elapsed = time.monotonic() - start

    assert result.is_error is False
    assert elapsed < 2.0


def test_before_send_drops_values_keeps_input_keys(monkeypatch):
    fake = _FakePosthog()
    mcp = _instrumented_server(monkeypatch, posthog_fake=fake)

    asyncio.run(_call(mcp, "ping_tool", {}))

    tool_call_events = [e for e in fake.captured if e["event"] == "$mcp_tool_call"]
    assert len(tool_call_events) >= 1
    for entry in tool_call_events:
        properties = entry["properties"]
        assert "$mcp_parameters" not in properties
        assert "$mcp_response" not in properties
        assert "$mcp_input_keys" in properties


def test_before_send_drops_error_message_keeps_error_type(monkeypatch):
    fake = _FakePosthog()
    mcp = _instrumented_server(monkeypatch, posthog_fake=fake)

    result = asyncio.run(_call(mcp, "boom_tool", {}))

    assert result.is_error is True
    tool_call_events = [e for e in fake.captured if e["event"] == "$mcp_tool_call"]
    assert len(tool_call_events) == 1
    properties = tool_call_events[0]["properties"]
    assert properties["$mcp_is_error"] is True
    assert "$mcp_error_type" in properties
    assert "$mcp_error_message" not in properties
    assert "$mcp_parameters" not in properties
    assert "$mcp_response" not in properties
    assert "$mcp_input_keys" in properties


def test_failed_call_emits_no_exception_autocapture_event(monkeypatch):
    # DD-007: enable_exception_autocapture=False. Unpinned, this flag can
    # flip without any other test failing, and the adapter's $exception
    # event carries the raw upstream error body in $exception_list, which
    # _before_send never strips (it only pops $mcp_error_message).
    fake = _FakePosthog()
    mcp = _instrumented_server(monkeypatch, posthog_fake=fake)

    result = asyncio.run(_call(mcp, "boom_tool", {}))

    assert result.is_error is True
    assert not any(e["event"] == "$exception" for e in fake.captured)


def test_before_send_keeps_input_key_names_not_values(monkeypatch):
    fake = _FakePosthog()
    mcp = _instrumented_server(monkeypatch, posthog_fake=fake)

    asyncio.run(_call(mcp, "echo_tool", {"x": "super-secret-value"}))

    tool_call_events = [e for e in fake.captured if e["event"] == "$mcp_tool_call"]
    assert len(tool_call_events) == 1
    properties = tool_call_events[0]["properties"]
    assert properties["$mcp_input_keys"] == ["x"]
    assert "super-secret-value" not in repr(properties)


def _extra(authorization: str | None) -> dict:
    headers = {"authorization": authorization} if authorization else {}
    return {"ctx": SimpleNamespace(request=SimpleNamespace(headers=headers))}


class _ProfileApi:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.calls = 0
        self._status = status
        self._body = body or {"id": 42, "email": "a@b.com"}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(self._status, json=self._body)


def _resolver(
    status: int = 200, body: dict | None = None, *, clock=None
) -> tuple["analytics._IdentityResolver", _ProfileApi]:
    api = _ProfileApi(status=status, body=body)
    client = httpx.AsyncClient(base_url="http://api.test", transport=httpx.MockTransport(api))
    kwargs = {} if clock is None else {"clock": clock}
    return analytics._IdentityResolver(client, **kwargs), api


def test_identify_resolves_the_account_never_the_raw_header():
    resolver, _ = _resolver()

    identity = asyncio.run(resolver(request={}, extra=_extra("Bearer secret-token")))

    assert identity.distinct_id == "42"
    assert identity.properties == {"email": "a@b.com"}
    assert "secret-token" not in repr(identity)
    assert "secret-token" not in str(identity.properties)


def test_identify_caches_the_resolved_identity_per_header():
    resolver, api = _resolver()

    asyncio.run(resolver(request={}, extra=_extra("Bearer secret-token")))
    asyncio.run(resolver(request={}, extra=_extra("Bearer secret-token")))

    assert api.calls == 1


def test_identify_returns_none_on_403_without_raising():
    resolver, _ = _resolver(status=403, body={})

    result = asyncio.run(resolver(request={}, extra=_extra("Bearer other-token")))

    assert result is None


def test_identify_returns_none_without_authorization_header_and_makes_no_call():
    resolver, api = _resolver()

    result = asyncio.run(resolver(request={}, extra=_extra(None)))

    assert result is None
    assert api.calls == 0


def test_sweep_expires_a_negative_entry_inserted_after_a_positive_one():
    # Positive (long TTL) and negative (short TTL) entries can expire out of
    # insertion order; _sweep must not stop scanning at the first live entry
    # it meets in insertion order, or a later-inserted, sooner-expiring entry
    # is never evicted and lingers in the cache indefinitely.
    now = [0.0]
    resolver, _ = _resolver(clock=lambda: now[0])

    asyncio.run(resolver(request={}, extra=_extra("Bearer long-lived")))  # t=0, TTL 300
    resolver._cache["short-lived"] = analytics._CachedIdentity(None, expires_at=5.0)

    now[0] = 40.0  # the synthetic short-lived entry has expired; long-lived has not
    asyncio.run(resolver(request={}, extra=_extra("Bearer third-call")))  # triggers _sweep

    assert "short-lived" not in resolver._cache
    assert any(c.expires_at == 300.0 for c in resolver._cache.values())


def test_identify_single_flights_concurrent_calls_for_the_same_header():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"id": 42, "email": "a@b.com"})

    client = httpx.AsyncClient(base_url="http://api.test", transport=httpx.MockTransport(handler))
    resolver = analytics._IdentityResolver(client)

    async def run():
        return await asyncio.gather(
            *(resolver(request={}, extra=_extra("Bearer secret-token")) for _ in range(5))
        )

    results = asyncio.run(run())

    assert calls == 1
    assert all(r.distinct_id == "42" for r in results)


def test_identify_caches_a_failed_resolution_too():
    resolver, api = _resolver(status=403, body={})

    asyncio.run(resolver(request={}, extra=_extra("Bearer scoped-token")))
    asyncio.run(resolver(request={}, extra=_extra("Bearer scoped-token")))

    assert api.calls == 1


def test_identify_returns_none_on_malformed_profile_body_without_raising():
    resolver, api = _resolver(body=["not", "a", "json", "object"])

    result = asyncio.run(resolver(request={}, extra=_extra("Bearer odd-token")))

    assert result is None
    assert api.calls == 1


def test_identify_omits_email_property_when_profile_has_none():
    resolver, _ = _resolver(body={"id": 42, "email": None})

    identity = asyncio.run(resolver(request={}, extra=_extra("Bearer secret-token")))

    assert identity.distinct_id == "42"
    assert "email" not in identity.properties


def test_identify_wiring_links_the_captured_event_to_the_account(monkeypatch):
    """EC-006 end to end: a real inbound Authorization header, through the
    actual HTTP transport (the in-memory Client used elsewhere never
    populates request headers, so it can't exercise this path), must resolve
    to the captured event's `distinct_id`/`$set` — and the raw header value
    must never appear in it."""
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
    fake = _FakePosthog()
    monkeypatch.setattr(analytics, "Posthog", lambda *a, **k: fake)

    profile_api = _ProfileApi(body={"id": 777, "email": "caller@example.com"})
    real_async_client = httpx.AsyncClient

    def async_client(*args, **kwargs):
        if kwargs.get("base_url") == "http://profile.test":
            return real_async_client(
                base_url="http://profile.test",
                transport=httpx.MockTransport(profile_api),
                timeout=kwargs.get("timeout"),
            )
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)

    api_client = httpx.AsyncClient(
        base_url="http://api.test", transport=httpx.MockTransport(_Api())
    )
    mcp = FastMCP.from_openapi(openapi_spec=_SPEC, client=api_client, name="t")
    handle = analytics.instrument(mcp, base_url="http://profile.test")
    app = mcp.http_app(path="/mcp")

    def http_client_factory(headers=None, timeout=None, auth=None, **_kwargs):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers
        )

    transport = StreamableHttpTransport(
        "http://testserver/mcp",
        headers={"Authorization": "Bearer super-secret-token"},
        httpx_client_factory=http_client_factory,
    )

    async def run():
        async with app.router.lifespan_context(app):
            async with Client(transport) as client:
                result = await client.call_tool("ping_tool", {})
            assert handle is not None
            await handle.flush()
            return result

    result = asyncio.run(run())

    assert result.is_error is False
    assert profile_api.calls == 1
    tool_call_events = [e for e in fake.captured if e["event"] == "$mcp_tool_call"]
    assert len(tool_call_events) == 1
    event = tool_call_events[0]
    assert event["distinct_id"] == "777"
    assert event["properties"]["$set"] == {"email": "caller@example.com"}
    assert "super-secret-token" not in repr(fake.captured)


def test_instrument_degrades_to_no_analytics_on_init_failure(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
    monkeypatch.setattr(
        analytics,
        "instrument_fastmcp",
        lambda *a, **k: (_ for _ in ()).throw(TypeError("unexpected keyword argument")),
    )

    handle = analytics.instrument(FastMCP(name="t"), base_url="http://api.test")

    assert handle is None


def test_instrument_shuts_down_the_posthog_client_on_init_failure(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
    fake = _FakePosthog()
    monkeypatch.setattr(analytics, "Posthog", lambda *a, **k: fake)
    monkeypatch.setattr(
        analytics,
        "instrument_fastmcp",
        lambda *a, **k: (_ for _ in ()).throw(TypeError("unexpected keyword argument")),
    )

    handle = analytics.instrument(FastMCP(name="t"), base_url="http://api.test")

    assert handle is None
    assert fake.shutdown_called is True


def test_aclose_shuts_down_the_posthog_client(monkeypatch):
    fake = _FakePosthog()
    mcp = _instrumented_server(monkeypatch, posthog_fake=fake)

    asyncio.run(mcp._wavix_analytics_handle.aclose())

    assert fake.shutdown_called is True


def test_aclose_does_not_hang_if_posthog_shutdown_never_returns(monkeypatch):
    monkeypatch.setattr(analytics, "SHUTDOWN_TIMEOUT_S", 0.05)
    fake = _FakePosthog()
    fake.shutdown = lambda: time.sleep(10)
    mcp = _instrumented_server(monkeypatch, posthog_fake=fake)

    start = time.monotonic()
    asyncio.run(mcp._wavix_analytics_handle.aclose())
    elapsed = time.monotonic() - start

    assert elapsed < 2.0
