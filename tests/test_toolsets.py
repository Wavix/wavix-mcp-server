import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

from wavix_mcp import scope_filter, server

FIXTURES = Path(__file__).parent / "fixtures"
SPEC = FIXTURES / "wavix-api.yaml"
VALID = [
    "account",
    "calls",
    "messaging",
    "numbers",
    "verification",
    "voice_config",
    "voice_history",
]
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _spec_operations():
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    for path, item in spec["paths"].items():
        for method in HTTP_METHODS:
            op = item.get(method)
            if op is not None:
                yield path, method, op


def _expected_tools(*toolsets):
    hand = {route: name for name, route in server._HAND_TOOL_ROUTES.items()}
    excluded = set(server.BINARY_STREAM_ENDPOINTS + server.EXCLUDED_ENDPOINTS)
    names = set()
    for path, method, op in _spec_operations():
        if op["x-mcp"]["toolset"] not in toolsets:
            continue
        if (method.upper(), path) in hand:
            names.add(hand[(method.upper(), path)])
        elif (path, method) not in excluded and op["x-mcp"].get("expose") is not False:
            names.add(op["operationId"])
    return names


CALLS = _expected_tools("calls")


class _Upstream:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={})


@pytest.fixture
def upstream(monkeypatch):
    counter = _Upstream()
    original = FastMCP.from_openapi.__func__

    def from_openapi(cls, *args, **kwargs):
        kwargs["client"]._transport = httpx.MockTransport(counter)
        return original(cls, *args, **kwargs)

    monkeypatch.setattr(server, "OPENAPI_URL", str(SPEC))
    monkeypatch.setattr(FastMCP, "from_openapi", classmethod(from_openapi))
    return counter


async def _with_client(mcp, query, action):
    app = mcp.http_app(path="/mcp")

    def factory(headers=None, timeout=None, auth=None, **_):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), headers=headers, timeout=timeout, auth=auth
        )

    url = "http://test/mcp" + (f"?{query}" if query is not None else "")
    failure = None
    result = None
    async with app.router.lifespan_context(app):
        try:
            async with Client(StreamableHttpTransport(url, httpx_client_factory=factory)) as client:
                result = await action(client)
        except Exception as error:
            failure = error
    if failure is not None:
        raise failure
    return result


def _listed(query, upstream, prepare=None):
    async def run():
        mcp = server.build_server()
        if prepare:
            prepare(mcp)

        async def names(client):
            return {t.name for t in await client.list_tools()}

        return await _with_client(mcp, query, names)

    return asyncio.run(run())


def test_lists_only_the_selected_toolsets(upstream):
    names = _listed("toolsets=numbers,messaging", upstream)

    assert names == _expected_tools("numbers", "messaging")
    assert not names & CALLS
    assert len(CALLS) == 13


def test_every_toolset_except_calls(upstream):
    names = _listed("toolsets=" + ",".join(v for v in VALID if v != "calls"), upstream)
    everything = _listed(None, upstream)

    assert names == everything - CALLS
    assert len(names) == len(everything) - 13


def test_without_toolsets_every_tool_is_listed(upstream):
    baseline = (FIXTURES / "tool_names_baseline.txt").read_text().split()

    assert sorted(_listed(None, upstream)) == baseline


def test_a_tool_outside_the_selection_is_refused_without_an_upstream_request(upstream):
    async def run():
        mcp = server.build_server()

        async def call(client):
            await client.call_tool("call_control_create", {"to": "+15550100", "from": "+15550101"})

        await _with_client(mcp, "toolsets=numbers", call)

    with pytest.raises(ToolError, match="not available on this connection's toolsets"):
        asyncio.run(run())
    assert upstream.requests == []


def test_a_hand_registered_tool_follows_its_operation(upstream):
    names = _listed("toolsets=voice_history", upstream)

    assert "call_recording_get_by_call" in names
    assert "speech_analytics_file_get" not in names


@pytest.mark.parametrize(
    "query", ["toolsets=nope", "toolsets=", "toolsets=,", "toolsets=numbers,nope"]
)
def test_an_invalid_selection_is_rejected(upstream, query):
    with pytest.raises(McpError) as caught:
        _listed(query, upstream)

    assert caught.value.error.code == -32602
    assert ", ".join(VALID) in caught.value.error.message


def test_duplicates_and_spaces_are_ignored(upstream):
    assert _listed("toolsets=numbers, numbers", upstream) == _listed("toolsets=numbers", upstream)


def test_the_toolset_filter_composes_with_the_scope_filter(upstream, monkeypatch):
    token = SimpleNamespace(scopes=["numbers:read"], claims={}, client_id="oauth")
    monkeypatch.setattr(scope_filter, "get_access_token", lambda: token)

    names = _listed("toolsets=numbers,messaging", upstream)

    assert names
    assert names < _expected_tools("numbers", "messaging")
    assert not any("messag" in n or "sms" in n for n in names)
    assert {"my_numbers_list"} <= names
    assert "buy_number" not in names


def test_a_tool_without_a_toolset(upstream):
    def add_stub(mcp):
        @mcp.tool
        def stub_without_toolset() -> str:
            return "ok"

    assert "stub_without_toolset" not in _listed("toolsets=numbers", upstream, add_stub)
    assert "stub_without_toolset" in _listed(None, upstream, add_stub)
