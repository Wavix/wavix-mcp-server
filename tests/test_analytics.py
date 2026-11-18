"""Baseline `posthog.mcp.instrument()` wiring: one event per tool call, no
schema change, and no behavior change when PostHog is unset or unreachable."""

import asyncio
import importlib.metadata
import time
from types import SimpleNamespace

import httpx
import mcp.types as mcp_types
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth import AccessToken

import wavix_mcp.analytics as analytics
import wavix_mcp.scope_filter as scope_filter
from wavix_mcp.auth import PASSTHROUGH_CLIENT_ID
from wavix_mcp.confirmation import NO_CALLER_MESSAGE, PREVIEW_FAILED_MESSAGE
from wavix_mcp.server import DownloadUrlError

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
        "/v1/slow": {
            "get": {
                "operationId": "slow_tool",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
        "/v1/down": {
            "get": {
                "operationId": "down_tool",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
        "/v1/shape": {
            "get": {
                "operationId": "shape_tool",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["id"],
                                    "properties": {"id": {"type": "string"}},
                                }
                            }
                        },
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
        if request.url.path == "/v1/slow":
            raise httpx.ReadTimeout("slow", request=request)
        if request.url.path == "/v1/down":
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json={"ok": True})


def _typed_tool(n: int) -> int:
    return n


def _unreachable_tool() -> dict:
    raise httpx.ConnectError("down")


def _timed_out_tool() -> dict:
    raise httpx.ReadTimeout("slow")


def _missing_download_tool() -> dict:
    raise DownloadUrlError({"error": "HTTP 404", "status_code": 404, "body": "secret detail"})


class _FakePosthog:
    def __init__(self) -> None:
        self.captured: list[dict] = []
        self.shutdown_called = False

    def capture(self, event, **kwargs) -> None:
        self.captured.append({"event": event, **kwargs})

    def shutdown(self) -> None:
        self.shutdown_called = True


def _tool_call_events(fake: _FakePosthog) -> list[dict]:
    return [e for e in fake.captured if e["event"] == "$mcp_tool_call"]


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
    mcp.tool(_typed_tool, name="typed_tool")
    mcp.tool(_unreachable_tool, name="unreachable_tool")
    mcp.tool(_timed_out_tool, name="timed_out_tool")
    mcp.tool(_missing_download_tool, name="missing_download_tool")
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

    tool_call_events = _tool_call_events(fake)
    assert len(tool_call_events) == 1
    properties = tool_call_events[0]["properties"]
    assert properties["$mcp_tool_name"] == "ping_tool"
    assert "$mcp_duration_ms" in properties
    assert properties["$mcp_is_error"] is False
    assert properties["$mcp_client_name"] == "pytest-client"
    assert properties["$mcp_client_version"] == "9.9"
    assert "$session_id" in properties
    assert properties["$mcp_server_build"] == analytics._server_build()
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

    tool_call_events = _tool_call_events(fake)
    assert len(tool_call_events) >= 1
    for entry in tool_call_events:
        properties = entry["properties"]
        assert "$mcp_parameters" not in properties
        assert "$mcp_response" not in properties
        assert "$mcp_input_keys" in properties


def test_before_send_replaces_error_message_with_an_error_type(monkeypatch):
    fake = _FakePosthog()
    mcp = _instrumented_server(monkeypatch, posthog_fake=fake)

    result = asyncio.run(_call(mcp, "boom_tool", {}))

    assert result.is_error is True
    tool_call_events = _tool_call_events(fake)
    assert len(tool_call_events) == 1
    properties = tool_call_events[0]["properties"]
    assert properties["$mcp_is_error"] is True
    assert properties["$mcp_error_type"] == "wavix_api_error"
    assert properties["$mcp_error_class"] == "ValueError"
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

    tool_call_events = _tool_call_events(fake)
    assert len(tool_call_events) == 1
    properties = tool_call_events[0]["properties"]
    assert properties["$mcp_input_keys"] == ["x"]
    assert "super-secret-value" not in repr(properties)


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("slow_tool", {}, "wavix_api_unreachable"),
        ("down_tool", {}, "wavix_api_unreachable"),
        ("shape_tool", {}, "invalid_response"),
        ("typed_tool", {"n": "not a number"}, "invalid_input"),
        ("unreachable_tool", {}, "wavix_api_unreachable"),
        ("timed_out_tool", {}, "wavix_api_unreachable"),
        ("missing_download_tool", {}, "not_found"),
        ("no_such_tool", {}, "not_found"),
    ],
)
def test_failed_call_is_typed_from_the_real_fastmcp_error(monkeypatch, tool, arguments, expected):
    fake = _FakePosthog()
    mcp = _instrumented_server(monkeypatch, posthog_fake=fake)

    result = asyncio.run(_call(mcp, tool, arguments))

    assert result.is_error is True
    properties = _tool_call_events(fake)[0]["properties"]
    assert properties["$mcp_error_type"] == expected
    assert "$mcp_error_message" not in properties


def _error_event(error_class: str, status: int | None, message: str) -> dict:
    return {
        "event": "$mcp_tool_call",
        "properties": {
            "$mcp_is_error": True,
            "$mcp_error_type": error_class,
            "$mcp_error_status": status,
            "$mcp_error_message": message,
        },
    }


@pytest.mark.parametrize(
    ("error_class", "status", "message", "expected"),
    [
        ("ValueError", 400, "HTTP error 400", "invalid_input"),
        ("ValueError", 401, "HTTP error 401", "unauthenticated"),
        ("ValueError", 403, "HTTP error 403", "missing_permission"),
        ("ValueError", 404, "HTTP error 404", "not_found"),
        ("ValueError", 409, "HTTP error 409", "wavix_api_rejected"),
        ("ValueError", 422, "HTTP error 422", "invalid_input"),
        ("ValueError", 429, "HTTP error 429", "rate_limited"),
        ("ValueError", 502, "HTTP error 502", "wavix_api_error"),
        ("ScopeDeniedError", None, "Tool 'x' requires a permission", "missing_permission"),
        ("Error", None, NO_CALLER_MESSAGE, "unauthenticated"),
        ("Error", None, PREVIEW_FAILED_MESSAGE, "internal_error"),
        ("Error", None, "Missing or invalid required argument: id", "invalid_input"),
        ("ConnectError", None, "refused", "wavix_api_unreachable"),
        ("ReadTimeout", None, "timed out", "wavix_api_unreachable"),
        ("NotFoundError", None, "Unknown tool: x", "not_found"),
        ("RuntimeError", None, "unexpected", "internal_error"),
    ],
)
def test_error_type_names_the_failure_category(error_class, status, message, expected):
    properties = analytics._before_send(_error_event(error_class, status, message))["properties"]

    assert properties["$mcp_error_type"] == expected
    assert properties["$mcp_error_class"] == error_class
    assert "$mcp_error_message" not in properties


def test_error_on_a_non_tool_event_keeps_the_library_error_type():
    event = {
        **_error_event("Error", None, "tools/list returned no tools"),
        "event": "$mcp_tools_list",
    }

    properties = analytics._before_send(event)["properties"]

    assert properties["$mcp_error_type"] == "Error"
    assert "$mcp_error_message" not in properties


def test_successful_call_gets_no_error_type():
    properties = analytics._before_send(
        {"event": "$mcp_tool_call", "properties": {"$mcp_is_error": False}}
    )["properties"]

    assert "$mcp_error_type" not in properties
    assert "$mcp_error_class" not in properties


def test_server_build_joins_package_version_and_short_sha(monkeypatch):
    monkeypatch.setenv(analytics.BUILD_SHA_ENV, "28d5e62aa1b2c3d4")
    version = importlib.metadata.version(analytics.PACKAGE_NAME)

    assert analytics._server_build() == f"{version}+28d5e62"


def test_server_build_is_the_package_version_without_a_sha(monkeypatch):
    monkeypatch.delenv(analytics.BUILD_SHA_ENV, raising=False)

    assert analytics._server_build() == importlib.metadata.version(analytics.PACKAGE_NAME)


def _extra(authorization: str | None) -> dict:
    headers = {"authorization": authorization} if authorization else {}
    return {"ctx": SimpleNamespace(request=SimpleNamespace(headers=headers))}


class _ProfileApi:
    def __init__(self, status: int = 200, body: object = None, delay_s: float = 0.0):
        self.calls = 0
        self._status = status
        self._body = {"id": 42, "email": "a@b.com"} if body is None else body
        self._delay_s = delay_s

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return httpx.Response(self._status, json=self._body)


def _resolver(
    status: int = 200, body: object = None, *, clock=None, delay_s: float = 0.0
) -> tuple["analytics._IdentityResolver", _ProfileApi]:
    api = _ProfileApi(status=status, body=body, delay_s=delay_s)
    client = httpx.AsyncClient(base_url="http://api.test", transport=httpx.MockTransport(api))
    kwargs = {} if clock is None else {"clock": clock}
    return analytics._IdentityResolver(client, **kwargs), api


async def _drain(resolver: "analytics._IdentityResolver") -> None:
    await asyncio.gather(*list(resolver._pending.values()))


def _identify_after_lookup(resolver, authorization: str | None):
    async def run():
        first = await resolver(request={}, extra=_extra(authorization))
        await _drain(resolver)
        return first, await resolver(request={}, extra=_extra(authorization))

    return asyncio.run(run())


def _oauth(monkeypatch, *, sub: str | None, client_id: str = "claude") -> None:
    claims = {} if sub is None else {"sub": sub}
    token = AccessToken(token="jwt", client_id=client_id, scopes=[], claims=claims)
    monkeypatch.setattr(scope_filter, "get_access_token", lambda: token)


def test_identify_uses_the_oauth_subject_without_a_lookup(monkeypatch):
    _oauth(monkeypatch, sub="1234")
    resolver, api = _resolver()

    identity = asyncio.run(resolver(request={}, extra=_extra("Bearer jwt")))

    assert identity.distinct_id == "1234"
    assert api.calls == 0


def test_identify_falls_back_to_the_lookup_for_an_api_key_passthrough(monkeypatch):
    _oauth(monkeypatch, sub="1234", client_id=PASSTHROUGH_CLIENT_ID)
    resolver, api = _resolver()

    _, identity = _identify_after_lookup(resolver, "Bearer api-key")

    assert identity.distinct_id == "42"
    assert api.calls == 1


def test_identify_falls_back_to_the_lookup_for_an_oauth_token_without_subject(monkeypatch):
    _oauth(monkeypatch, sub=None)
    resolver, api = _resolver()

    _, identity = _identify_after_lookup(resolver, "Bearer jwt")

    assert identity.distinct_id == "42"
    assert api.calls == 1


def test_identify_answers_a_cold_lookup_without_waiting_for_it():
    resolver, api = _resolver(delay_s=1.0)

    async def run():
        start = time.monotonic()
        identity = await resolver(request={}, extra=_extra("Bearer api-key"))
        elapsed = time.monotonic() - start
        await asyncio.sleep(0.05)
        await resolver.aclose()
        return identity, elapsed

    identity, elapsed = asyncio.run(run())

    assert identity is None
    assert elapsed < 0.5
    assert api.calls == 1


def test_identify_resolves_the_account_never_the_raw_header():
    resolver, _ = _resolver()

    first, identity = _identify_after_lookup(resolver, "Bearer secret-token")

    assert first is None
    assert identity.distinct_id == "42"
    assert "secret-token" not in repr(identity)


def test_identify_sends_no_email():
    resolver, _ = _resolver(body={"id": 42, "email": "a@b.com"})

    _, identity = _identify_after_lookup(resolver, "Bearer secret-token")

    assert not identity.properties
    assert "a@b.com" not in repr(identity)


def test_identify_caches_the_resolved_identity_per_header():
    resolver, api = _resolver()

    _identify_after_lookup(resolver, "Bearer secret-token")
    asyncio.run(resolver(request={}, extra=_extra("Bearer secret-token")))

    assert api.calls == 1


def test_identify_looks_the_account_up_again_after_the_ttl():
    now = [0.0]
    resolver, api = _resolver(clock=lambda: now[0])

    _identify_after_lookup(resolver, "Bearer secret-token")
    now[0] = analytics.IDENTITY_CACHE_TTL_S + 1
    _identify_after_lookup(resolver, "Bearer secret-token")

    assert api.calls == 2


def test_identify_returns_none_on_403_without_raising():
    resolver, _ = _resolver(status=403, body={})

    _, result = _identify_after_lookup(resolver, "Bearer other-token")

    assert result is None


def test_identify_returns_none_without_authorization_header_and_makes_no_call():
    resolver, api = _resolver()

    _, result = _identify_after_lookup(resolver, None)

    assert result is None
    assert api.calls == 0


def test_identify_single_flights_concurrent_calls_for_the_same_header():
    resolver, api = _resolver(delay_s=0.05)

    async def run():
        await asyncio.gather(
            *(resolver(request={}, extra=_extra("Bearer secret-token")) for _ in range(5))
        )
        await _drain(resolver)

    asyncio.run(run())

    assert api.calls == 1


def test_identify_caches_a_failed_resolution_too():
    resolver, api = _resolver(status=403, body={})

    _identify_after_lookup(resolver, "Bearer scoped-token")

    assert api.calls == 1


def test_identify_returns_none_on_malformed_profile_body_without_raising():
    resolver, api = _resolver(body=["not", "a", "json", "object"])

    _, result = _identify_after_lookup(resolver, "Bearer odd-token")

    assert result is None
    assert api.calls == 1


def test_identify_caches_an_unexpected_lookup_failure_without_leaking_it():
    resolver, _ = _resolver()
    calls = 0

    async def broken_get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("client closed")

    resolver._client = SimpleNamespace(get=broken_get)

    _, first = _identify_after_lookup(resolver, "Bearer api-key")
    _, second = _identify_after_lookup(resolver, "Bearer api-key")

    assert first is None and second is None
    assert calls == 1


def test_resolver_aclose_cancels_a_pending_lookup():
    resolver, _ = _resolver(delay_s=10.0)

    async def run():
        await resolver(request={}, extra=_extra("Bearer api-key"))
        await resolver.aclose()
        return dict(resolver._pending)

    start = time.monotonic()
    pending = asyncio.run(run())

    assert pending == {}
    assert time.monotonic() - start < 2.0


def test_identify_wiring_links_the_captured_event_to_the_account(monkeypatch):
    """The in-memory Client never populates request headers, so only the real
    HTTP transport exercises the Authorization-header path."""
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
                first = await client.call_tool("ping_tool", {})
                assert handle is not None and handle.resolver is not None
                await _drain(handle.resolver)
                second = await client.call_tool("ping_tool", {})
            await handle.flush()
            return first, second

    first, second = asyncio.run(run())

    assert first.is_error is False
    assert second.is_error is False
    assert profile_api.calls == 1
    tool_call_events = _tool_call_events(fake)
    assert len(tool_call_events) == 2
    assert tool_call_events[-1]["distinct_id"] == "777"
    assert "caller@example.com" not in repr(fake.captured)
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
