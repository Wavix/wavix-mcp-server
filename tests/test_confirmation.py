"""Two-step confirmation: a preview with a single-use token, then the action."""

import asyncio
import copy
import importlib.resources
import json
import re
from types import SimpleNamespace

import anyio
import httpx
import pytest
import yaml
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolRequestParams

from wavix_mcp import confirmation, scope_filter, server
from wavix_mcp.confirmation import (
    ALWAYS_CONFIRMED,
    CUSTOM_PREVIEWS,
    DETAILS,
    MAX_LISTED_NUMBERS,
    TOKEN_PARAMETER,
    TOKEN_TTL_S,
    ConfirmationInputError,
    ConfirmationMiddleware,
    ConfirmedTool,
    preview_builders,
)
from wavix_mcp.scope_filter import ScopeFilterMiddleware

from .test_instructions import imperative_violations

APPROVAL_AFTER_PREVIEW = (
    "a call meant to follow the user's approval of the preview in a later message"
)
CALL = {"from": "+15550100", "to": "+15550199", "callback_url": "https://cb.test/x"}
CAMPAIGN = {
    "voice_campaign": {"callflow_id": 3212, "caller_id": "13123310912", "contact": "16729923812"}
}

_UUID_RESPONSE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"uuid": {"type": "string"}},
}


def _post(operation_id: str, properties: dict, summary: str, required: tuple = ()) -> dict:
    schema = {"type": "object", "properties": properties, "required": list(required)}
    return {
        "post": {
            "operationId": operation_id,
            "summary": summary,
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": schema}},
            },
            "responses": {
                "200": {
                    "description": "ok",
                    "content": {"application/json": {"schema": _UUID_RESPONSE}},
                }
            },
        }
    }


_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/v1/calls": _post(
            "call_control_create",
            {
                "from": {"type": "string"},
                "to": {"type": "string"},
                "callback_url": {"type": "string"},
            },
            "Start a call",
            ("from", "to"),
        ),
        "/v1/voice-campaigns": _post(
            "voice_campaigns_create",
            {
                "voice_campaign": {
                    "type": "object",
                    "properties": {
                        "callflow_id": {"type": "integer"},
                        "caller_id": {"type": "string"},
                        "contact": {"type": "string"},
                    },
                }
            },
            "Trigger a scenario",
            ("voice_campaign",),
        ),
        "/v1/calls/{id}/answer": {
            "post": {
                **_post("call_control_answer", {"stream_url": {"type": "string"}}, "Answer a call")[
                    "post"
                ],
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
            }
        },
        "/v1/trunks/{id}": {
            "put": {
                **_post("sip_trunks_update", {"label": {"type": "string"}}, "Update a SIP trunk")[
                    "post"
                ],
                "x-mcp": {"destructive": False},
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
                ],
            }
        },
        "/v1/mydids": {
            "get": {
                "operationId": "my_numbers_list",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": _UUID_RESPONSE}},
                    }
                },
            }
        },
    },
}


_BUILDERS = preview_builders({"call_control_create": ConfirmedTool("Start a call", ("from", "to"))})


class _Api:
    def __init__(self, status: int = 200, lookups: dict[str, object] | None = None):
        self.requests: list[httpx.Request] = []
        self._status = status
        self._lookups = lookups or {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            key = request.url.path
            if request.url.query:
                key += "?" + request.url.query.decode()
            if key not in self._lookups:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=self._lookups[key])
        return httpx.Response(self._status, json={"uuid": f"call-{self.dials + 1}"})

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    @property
    def dials(self) -> int:
        return len(self.posts)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def isolated_routes():
    routes = dict(server._TOOL_ROUTES)
    confirmed = dict(server._CONFIRMED_TOOLS)
    unannotated = list(server._UNANNOTATED)
    yield
    server._TOOL_ROUTES.clear()
    server._TOOL_ROUTES.update(routes)
    server._CONFIRMED_TOOLS.clear()
    server._CONFIRMED_TOOLS.update(confirmed)
    server._UNANNOTATED[:] = unannotated


@pytest.fixture
def anonymous(monkeypatch):
    monkeypatch.setattr(confirmation, "get_access_token", lambda: None)


def _server(api: _Api, clock: _Clock | None = None, *, with_scope_filter: bool = False) -> FastMCP:
    client = httpx.AsyncClient(base_url="http://api.test", transport=httpx.MockTransport(api))
    mcp = FastMCP.from_openapi(
        openapi_spec=_SPEC,
        client=client,
        name="t",
        mcp_component_fn=server._xmcp_component_fn,
    )
    if with_scope_filter:
        mcp.add_middleware(ScopeFilterMiddleware(dict(server._TOOL_ROUTES)))
    builders = preview_builders(server._CONFIRMED_TOOLS)
    mcp.add_middleware(ConfirmationMiddleware(builders, client, clock=clock or _Clock()))
    return mcp


async def _calls(mcp, *steps):
    results = []
    async with Client(mcp) as client:
        for tool, arguments in steps:
            results.append(await client.call_tool(tool, arguments, raise_on_error=False))
    return results


def _text(result) -> str:
    return " ".join(c.text for c in result.content if hasattr(c, "text"))


def _token(result) -> str:
    match = re.search(rf'{TOKEN_PARAMETER}="([^"]+)"', _text(result))
    assert match, _text(result)
    return match.group(1)


def _confirm(mcp, tool="call_control_create", arguments=CALL):
    async def run():
        async with Client(mcp) as client:
            preview = await client.call_tool(tool, arguments, raise_on_error=False)
            confirmed = {**arguments, TOKEN_PARAMETER: _token(preview)}
            done = await client.call_tool(tool, confirmed, raise_on_error=False)
            return preview, done

    return asyncio.run(run())


def test_first_call_returns_a_preview_and_dials_nothing(isolated_routes, anonymous):
    api = _Api()
    [preview] = asyncio.run(_calls(_server(api), ("call_control_create", CALL)))
    assert api.dials == 0
    assert not preview.is_error
    lines = _text(preview).splitlines()
    assert lines[:4] == [
        'This will run "Start a call":',
        "• from: +15550100",
        "• to: +15550199",
        "• callback_url: https://cb.test/x",
    ]
    assert re.fullmatch(
        "This preview is meant for the user. The action runs when this tool "
        rf'is called again with {TOKEN_PARAMETER}="[\w-]+"; that call is meant to follow '
        "the user's approval in a later message, and approval given before this preview "
        "is not approval of it\\.",
        lines[4],
    )
    assert imperative_violations(lines[4]) == []


def test_token_runs_the_action_once_without_forwarding_the_token(isolated_routes, anonymous):
    api = _Api()
    _preview, done = _confirm(_server(api))
    assert not done.is_error and api.dials == 1
    assert json.loads(api.posts[0].content) == CALL


def test_retried_confirm_returns_the_first_result_without_acting_again(isolated_routes, anonymous):
    api = _Api()
    mcp = _server(api)

    async def run():
        async with Client(mcp) as client:
            preview = await client.call_tool("call_control_create", CALL)
            confirmed = {**CALL, TOKEN_PARAMETER: _token(preview)}
            first = await client.call_tool("call_control_create", confirmed)
            again = await client.call_tool("call_control_create", confirmed)
            return first, again

    first, again = asyncio.run(run())
    assert api.dials == 1
    assert not first.is_error and again.structured_content == first.structured_content


def test_failed_confirm_spends_the_token(isolated_routes, anonymous):
    api = _Api(status=422)
    mcp = _server(api)

    async def run():
        async with Client(mcp) as client:
            preview = await client.call_tool("call_control_create", CALL)
            confirmed = {**CALL, TOKEN_PARAMETER: _token(preview)}
            failed = await client.call_tool("call_control_create", confirmed, raise_on_error=False)
            again = await client.call_tool("call_control_create", confirmed, raise_on_error=False)
            return failed, again

    failed, again = asyncio.run(run())
    assert failed.is_error and api.dials == 1
    assert _text(again).startswith("The confirmation token has expired or does not match")


def _direct(middleware: ConfirmationMiddleware, arguments: dict, session_id: str = "s1"):
    return MiddlewareContext(
        message=CallToolRequestParams(name="call_control_create", arguments=arguments),
        fastmcp_context=SimpleNamespace(session_id=session_id),
    )


def test_retry_during_the_confirmed_call_waits_for_its_result(anonymous):
    dials = []

    async def call_next(_context):
        dials.append(1)
        await anyio.sleep(0.1)
        return ToolResult(structured_content={"uuid": "call-1"})

    middleware = ConfirmationMiddleware(_BUILDERS, httpx.AsyncClient())

    async def run():
        preview = await middleware.on_call_tool(_direct(middleware, CALL), call_next)
        confirmed = {**CALL, TOKEN_PARAMETER: _token(preview)}
        results = []

        async def confirm():
            results.append(await middleware.on_call_tool(_direct(middleware, confirmed), call_next))

        async with anyio.create_task_group() as group:
            group.start_soon(confirm)
            group.start_soon(confirm)
        return results

    results = asyncio.run(run())
    assert dials == [1]
    assert [r.structured_content for r in results] == [{"uuid": "call-1"}] * 2


def test_token_does_not_cover_changed_arguments(isolated_routes, anonymous):
    api = _Api()
    mcp = _server(api)
    other = {**CALL, "to": "+15550177"}

    async def run():
        async with Client(mcp) as client:
            preview = await client.call_tool("call_control_create", CALL)
            changed = {**other, TOKEN_PARAMETER: _token(preview)}
            return await client.call_tool("call_control_create", changed)

    result = asyncio.run(run())
    assert api.dials == 0
    assert "does not match" in _text(result) and "+15550177" in _text(result)


def test_token_does_not_cover_another_tool(isolated_routes, anonymous):
    api = _Api()
    mcp = _server(api)

    async def run():
        async with Client(mcp) as client:
            preview = await client.call_tool("call_control_create", CALL)
            arguments = {**CAMPAIGN, TOKEN_PARAMETER: _token(preview)}
            return await client.call_tool("voice_campaigns_create", arguments)

    result = asyncio.run(run())
    assert api.dials == 0 and "does not match" in _text(result)


def test_unknown_token_gets_a_new_preview(isolated_routes, anonymous):
    api = _Api()
    [result] = asyncio.run(
        _calls(_server(api), ("call_control_create", {**CALL, TOKEN_PARAMETER: "forged"}))
    )
    assert api.dials == 0 and "does not match" in _text(result)
    assert _token(result) != "forged"


def test_token_expires(isolated_routes, anonymous):
    api, clock = _Api(), _Clock()
    mcp = _server(api, clock)

    async def run():
        async with Client(mcp) as client:
            preview = await client.call_tool("call_control_create", CALL)
            clock.now += TOKEN_TTL_S
            confirmed = {**CALL, TOKEN_PARAMETER: _token(preview)}
            return await client.call_tool("call_control_create", confirmed)

    result = asyncio.run(run())
    assert api.dials == 0 and "expired" in _text(result)


def test_token_is_bound_to_the_caller(isolated_routes, monkeypatch):
    user = {"sub": "user-1"}
    monkeypatch.setattr(
        confirmation, "get_access_token", lambda: SimpleNamespace(claims=dict(user), scopes=[])
    )
    api = _Api()
    mcp = _server(api)

    async def run():
        async with Client(mcp) as client:
            preview = await client.call_tool("call_control_create", CALL)
            user["sub"] = "user-2"
            confirmed = {**CALL, TOKEN_PARAMETER: _token(preview)}
            return await client.call_tool("call_control_create", confirmed)

    result = asyncio.run(run())
    assert api.dials == 0 and "does not match" in _text(result)


def test_api_key_callers_are_told_apart_by_authorization(monkeypatch):
    monkeypatch.setattr(confirmation, "get_access_token", lambda: None)
    headers = {"authorization": "Bearer A"}
    monkeypatch.setattr(confirmation, "get_http_headers", lambda **_: headers)
    first = ConfirmationMiddleware._caller(None)
    headers["authorization"] = "Bearer B"
    second = ConfirmationMiddleware._caller(None)
    assert first is not None and second is not None and first != second


def test_passthrough_token_is_keyed_by_authorization_not_sub(monkeypatch):
    token = SimpleNamespace(client_id=confirmation.PASSTHROUGH_CLIENT_ID, claims={"sub": "shared"})
    monkeypatch.setattr(confirmation, "get_access_token", lambda: token)
    monkeypatch.setattr(confirmation, "get_http_headers", lambda **_: {"authorization": "Bearer A"})
    assert confirmation._caller_identity().startswith("auth:")


def test_anonymous_callers_in_different_sessions_do_not_share_tokens(anonymous):
    first = ConfirmationMiddleware._caller(SimpleNamespace(session_id="s1"))
    second = ConfirmationMiddleware._caller(SimpleNamespace(session_id="s2"))
    assert first != second


def test_cancelled_client_does_not_abort_the_confirmed_call(anonymous):
    dials = []

    async def call_next(_context):
        await anyio.sleep(0.2)
        dials.append(1)
        return ToolResult(structured_content={"uuid": "call-1"})

    middleware = ConfirmationMiddleware(_BUILDERS, httpx.AsyncClient())

    def context(arguments):
        return MiddlewareContext(
            message=CallToolRequestParams(name="call_control_create", arguments=arguments),
            fastmcp_context=SimpleNamespace(session_id="s1"),
        )

    async def run():
        preview = await middleware.on_call_tool(context(CALL), call_next)
        confirmed = {**CALL, TOKEN_PARAMETER: _token(preview)}
        with anyio.move_on_after(0.05):
            await middleware.on_call_tool(context(confirmed), call_next)

    asyncio.run(run())
    assert dials == [1]


def test_answer_without_stream_needs_the_token(isolated_routes, anonymous):
    api = _Api()
    [result] = asyncio.run(_calls(_server(api), ("call_control_answer", {"id": "c1"})))
    assert not result.is_error and api.dials == 0
    assert _text(result).startswith('This will run "Answer a call":\n• id: c1')


def test_answer_with_stream_needs_the_token(isolated_routes, anonymous):
    api = _Api()
    arguments = {"id": "c1", "stream_url": "wss://agent.test/s"}
    preview, done = _confirm(_server(api), "call_control_answer", arguments)
    assert _text(preview).startswith(
        'This will run "Answer a call":\n• id: c1\n• stream_url: wss://agent.test/s'
    )
    assert not done.is_error and api.dials == 1
    assert json.loads(api.posts[0].content) == {"stream_url": "wss://agent.test/s"}


def test_hint_free_tool_named_by_the_review_is_confirmed(isolated_routes, anonymous):
    api = _Api()
    [result] = asyncio.run(_calls(_server(api), ("sip_trunks_update", {"id": 7, "label": "A"})))
    assert api.requests[-1].method == "GET" and not [r for r in api.requests if r.method == "PUT"]
    assert _text(result).startswith("This will change SIP trunk 7:")


def test_gated_tools_take_a_token_and_drop_output_schema(isolated_routes, anonymous):
    async def run():
        async with Client(_server(_Api())) as client:
            return {t.name: t for t in await client.list_tools()}

    tools = asyncio.run(run())
    gated = tools["call_control_create"]
    assert gated.outputSchema is None
    assert TOKEN_PARAMETER in gated.inputSchema["properties"]
    assert TOKEN_PARAMETER not in gated.inputSchema.get("required", [])
    assert tools["my_numbers_list"].outputSchema is not None
    assert TOKEN_PARAMETER not in tools["my_numbers_list"].inputSchema.get("properties", {})


def test_scope_denial_precedes_the_preview(isolated_routes, anonymous, monkeypatch):
    monkeypatch.setattr(
        scope_filter,
        "get_access_token",
        lambda: SimpleNamespace(scopes=["numbers:read"], claims={}),
    )
    api = _Api()

    async def run():
        async with Client(_server(api, with_scope_filter=True)) as client:
            return await client.call_tool("call_control_create", CALL)

    with pytest.raises(ToolError, match="calls:write"):
        asyncio.run(run())
    assert api.requests == []


@pytest.mark.parametrize(
    ("tool", "arguments", "field"),
    [
        ("call_control_create", {"from": "+15550100"}, "to"),
        ("call_control_create", {"from": "+15550100", "to": "  "}, "to"),
        ("voice_campaigns_create", {}, "voice_campaign"),
    ],
)
def test_missing_argument_fails_closed(isolated_routes, anonymous, tool, arguments, field):
    api = _Api()
    [result] = asyncio.run(_calls(_server(api), (tool, arguments)))
    assert result.is_error and field in _text(result)
    assert api.dials == 0


def test_failure_while_building_the_preview_fails_closed(monkeypatch, anonymous):
    async def broken(_arguments, _api):
        raise RuntimeError("boom")

    dials = []

    async def call_next(_context):
        dials.append(1)
        return ToolResult(content="ok")

    middleware = ConfirmationMiddleware({"call_control_create": broken}, httpx.AsyncClient())
    context = MiddlewareContext(
        message=CallToolRequestParams(name="call_control_create", arguments=CALL),
        fastmcp_context=SimpleNamespace(session_id="s1"),
    )
    result = asyncio.run(middleware.on_call_tool(context, call_next))
    assert result.is_error and dials == []


def _preview(tool: str, arguments: dict, lookups: dict | None = None, title: str = "Act") -> str:
    api = httpx.AsyncClient(
        base_url="http://api.test", transport=httpx.MockTransport(_Api(lookups=lookups))
    )
    build = preview_builders({tool: ConfirmedTool(title)})[tool]
    return asyncio.run(build(arguments, api))


def test_template_lists_the_arguments_hiding_secrets_and_skipping_nulls():
    arguments = {
        "voice_campaign": {"callflow_id": 3212, "caller_id": "13123310912", "contact": None},
        "password": "secret",
        "countries": ["US", "CA"],
        "mock": False,
    }
    text = _preview("some_tool", arguments, title="Trigger a scenario")
    assert text == (
        'This will run "Trigger a scenario":\n'
        "• voice_campaign.callflow_id: 3212\n"
        "• voice_campaign.caller_id: 13123310912\n"
        "• password: (hidden)\n"
        "• countries: US, CA\n"
        "• mock: false"
    )


def test_template_without_arguments_is_one_line():
    assert _preview("some_tool", {}, title="Stop audio playback") == (
        'This will run "Stop audio playback".'
    )


def test_line_breaks_and_direction_marks_cannot_forge_preview_lines():
    arguments = {"to": "\u202e9910555", "callback_url": "https://a\n• to: 15550199"}
    assert _preview("call_control_create", arguments, title="Start a call") == (
        'This will run "Start a call":\n'
        "• to: \\u202e9910555\n"
        "• callback_url: https://a\\n• to: 15550199"
    )


def test_line_break_in_a_looked_up_name_stays_in_the_heading():
    lookups = {"/v1/trunks/7": {"label": "Office\n• password: unchanged"}}
    text = _preview("sip_trunks_update", {"id": 7, "label": "A"}, lookups)
    assert text.splitlines()[0] == "This will change SIP trunk Office\\n• password: unchanged:"


def test_empty_list_is_shown_as_empty():
    arguments = {"sender_id": "Wavix", "countries": []}
    assert _preview("sms_and_mms_sender_ids_create", arguments, title="Create a Sender ID") == (
        'This will run "Create a Sender ID":\n• sender_id: Wavix\n• countries: []'
    )


def test_long_message_is_cut_in_the_preview():
    text = "x" * (confirmation.MESSAGE_PREVIEW_CHARS + 20)
    arguments = {"from": "Wavix", "to": "15550199", "message_body": {"text": text}}
    preview = _preview("sms_and_mms_messages_send", arguments)
    assert preview.endswith(
        "• message_body.text: " + "x" * confirmation.MESSAGE_PREVIEW_CHARS + "…"
    )


def test_long_url_is_not_cut():
    url = "wss://agent.test/" + "a" * confirmation.MESSAGE_PREVIEW_CHARS
    assert _preview("call_control_streams_create", {"stream_url": url}).endswith(url)


def test_release_numbers_by_id_names_each_number():
    lookups = {
        "/v1/numbers/1": {"number": "12125550101", "city": "New York", "country": "United States"},
    }
    text = _preview("my_numbers_delete", {"ids": [1, 2]}, lookups)
    assert text == (
        "This will release 2 numbers back to stock (irreversible):\n"
        "• 12125550101 (New York, United States)\n"
        "• number ID 2"
    )


def test_release_numbers_by_did_matches_the_exact_number():
    lookups = {
        "/v1/numbers?search=442079460958": {
            "items": [
                {"number": "4420794609580", "city": "Elsewhere", "country": "X"},
                {"number": "442079460958", "city": "London", "country": "United Kingdom"},
            ]
        }
    }
    text = _preview("my_numbers_delete", {"dids": "+442079460958, 15550100"}, lookups)
    assert text == (
        "This will release 2 numbers back to stock (irreversible):\n"
        "• 442079460958 (London, United Kingdom)\n"
        "• 15550100"
    )


def test_release_numbers_lists_at_most_the_cap():
    ids = list(range(1, MAX_LISTED_NUMBERS + 4))
    text = _preview("my_numbers_delete", {"ids": ids})
    lines = text.splitlines()
    assert lines[0] == f"This will release {len(ids)} numbers back to stock (irreversible):"
    assert len(lines) == MAX_LISTED_NUMBERS + 2 and lines[-1] == "• and 3 more"


@pytest.mark.parametrize(
    "arguments",
    [{}, {"ids": [1], "dids": "1555"}, {"ids": []}, {"ids": [True]}, {"dids": " , "}],
)
def test_release_numbers_rejects_an_unclear_selection(arguments):
    with pytest.raises(ConfirmationInputError):
        _preview("my_numbers_delete", arguments)


def test_trunk_update_lists_only_the_changes():
    lookups = {
        "/v1/trunks/7": {
            "label": "Office",
            "callerid": "15550100",
            "host": "a.test",
            "allowed_ips": [{"id": 1, "ip": "1.1.1.1"}],
            "max_call_cost": "0.50",
            "ip_restrict": True,
        }
    }
    arguments = {
        "id": 7,
        "label": "Office",
        "password": "secret",
        "callerid": "15550199",
        "host_request": {"host": "a.test"},
        "allowed_ips": [{"ip": "1.1.1.1"}],
        "max_call_cost": 0.5,
        "ip_restrict": False,
    }
    text = _preview("sip_trunks_update", arguments, lookups)
    assert text == (
        "This will change SIP trunk Office:\n"
        "• password: (hidden)\n"
        "• callerid: 15550100 → 15550199\n"
        "• ip_restrict: true → false"
    )
    assert "secret" not in text


def test_trunk_update_without_lookup_lists_the_new_values():
    text = _preview("sip_trunks_update", {"id": 7, "label": "Office", "password": "p"})
    assert text == "This will change SIP trunk 7:\n• label: Office\n• password: (hidden)"


def test_sub_account_update_compares_nested_destinations():
    lookups = {
        "/v1/sub-organizations/3": {
            "name": "Shop",
            "status": "enabled",
            "default_destinations": {
                "sms_endpoint": "https://a.test",
                "dlr_endpoint": "https://b.test",
            },
        }
    }
    arguments = {
        "id": 3,
        "status": "disabled",
        "default_destinations": {"sms_endpoint": "https://a.test"},
    }
    assert _preview("sub_accounts_update", arguments, lookups) == (
        "This will change sub-account Shop:\n• status: enabled → disabled"
    )


@pytest.mark.parametrize(
    ("tool", "arguments", "lookups", "detail"),
    [
        (
            "call_control_delete",
            {"id": "c1"},
            {"/v1/calls/c1": {"call": {"from": "15550100", "to": "15550199"}}},
            "call: 15550100 → 15550199",
        ),
        (
            "call_control_streams_delete",
            {"call_id": "c1", "id": "s1"},
            {"/v1/calls/c1": {"call": {"from": "15550100", "to": "15550199"}}},
            "call: 15550100 → 15550199",
        ),
        (
            "call_recording_delete",
            {"id": 5},
            {
                "/v1/recordings/5": {
                    "from": "15550100",
                    "to": "15550199",
                    "created_at": "2026-10-12T10:00:00Z",
                    "duration": 41,
                }
            },
            "recording: 15550100 → 15550199, 2026-10-12, 41 s",
        ),
        (
            "sip_trunks_delete",
            {"id": 7},
            {"/v1/trunks/7": {"label": "Office", "callerid": "15550100"}},
            "SIP trunk: Office, Caller ID 15550100",
        ),
        (
            "ten_dlc_brands_delete",
            {"brand_id": "BABC123"},
            {"/v3/10dlc/brands/BABC123": {"dba_name": "Acme"}},
            "brand: Acme",
        ),
        (
            "ten_dlc_campaign_numbers_unlink",
            {"brand_id": "B1", "campaign_id": "C9", "number": "15550100"},
            {"/v3/10dlc/brands/B1/campaigns/C9": {"name": "Alerts"}},
            "campaign: Alerts",
        ),
        (
            "sms_and_mms_sender_ids_delete",
            {"id": "s1"},
            {"/v3/messages/sender-ids/s1": {"sender_id": "Wavix", "type": "alphanumeric"}},
            "Sender ID: Wavix (alphanumeric)",
        ),
        (
            "webrtc_tokens_delete",
            {"id": "t1"},
            {"/v2/webrtc/tokens/t1": {"sip_trunk": "Office"}},
            "SIP trunk: Office",
        ),
        (
            "api_keys_delete",
            {"id": 2},
            {"/v1/api-keys": [{"id": 1, "label": "Old"}, {"id": 2, "label": "CI", "value": "k"}]},
            "API key: CI",
        ),
        (
            "call_webhooks_delete",
            {"event_type": "post-call"},
            {"/v1/calls/webhooks": [{"event_type": "post-call", "url": "https://hook.test/a"}]},
            "current URL: https://hook.test/a",
        ),
        (
            "ten_dlc_subscriptions_delete",
            {"subscription_category": "BRAND"},
            {"/v3/10dlc/subscriptions": [{"subscription_category": "BRAND", "url": "https://s"}]},
            "current URL: https://s",
        ),
        (
            "ten_dlc_brand_evidence_delete",
            {"brand_id": "B1", "id": "u1"},
            {"/v3/10dlc/brands/B1/evidence": {"items": [{"uuid": "u1", "file_name": "ein.pdf"}]}},
            "file: ein.pdf",
        ),
    ],
)
def test_deletes_by_id_name_what_they_act_on(tool, arguments, lookups, detail):
    lines = _preview(tool, arguments, lookups).splitlines()
    assert lines[-1] == f"• {detail}"
    assert len(lines) == len(arguments) + 2


def test_failed_lookup_leaves_only_the_arguments():
    assert _preview("sip_trunks_delete", {"id": 8}, title="Delete a SIP trunk") == (
        'This will run "Delete a SIP trunk":\n• id: 8'
    )


def test_campaign_delete_lists_the_numbers_that_stop_sending():
    lookups = {
        "/v3/10dlc/brands/BABC123/campaigns/C9": {
            "name": "Alerts",
            "phone_numbers": ["15550100", "15550101"],
        }
    }
    arguments = {"brand_id": "BABC123", "campaign_id": "C9"}
    assert _preview("ten_dlc_brand_campaigns_delete", arguments, lookups) == (
        'This will run "Act":\n'
        "• brand_id: BABC123\n"
        "• campaign_id: C9\n"
        "• campaign: Alerts\n"
        "• numbers that stop sending: 15550100, 15550101"
    )


def test_checkout_lists_numbers_and_the_total():
    lookups = {
        "/v1/buy/cart": {
            "dids": [
                {
                    "id": 11,
                    "number": "12125550101",
                    "city": "New York",
                    "country": "US",
                    "activation_fee": "1.00",
                    "monthly_fee": "2.50",
                },
                {
                    "id": 12,
                    "number": "442079460958",
                    "city": "London",
                    "country": "UK",
                    "activation_fee": "0",
                    "monthly_fee": "3.00",
                },
            ]
        }
    }
    assert _preview("cart_checkout", {"ids": ["11", "12"]}, lookups) == (
        "This will buy 2 numbers for 6.50, charged to your balance:\n"
        "• 12125550101 (New York, US)\n"
        "• 442079460958 (London, UK)"
    )


def test_checkout_without_cart_omits_the_total():
    assert _preview("cart_checkout", {"ids": ["11"]}) == (
        "This will buy 1 number, charged to your balance:\n• cart item 11"
    )


@pytest.fixture(scope="module")
def real_tools():
    routes = dict(server._TOOL_ROUTES)
    confirmed = dict(server._CONFIRMED_TOOLS)
    tools = asyncio.run(server.build_server().list_tools())
    gated = dict(server._CONFIRMED_TOOLS)
    server._TOOL_ROUTES.clear()
    server._TOOL_ROUTES.update(routes)
    server._CONFIRMED_TOOLS.clear()
    server._CONFIRMED_TOOLS.update(confirmed)
    return tools, gated


def test_every_destructive_tool_and_the_named_ones_are_confirmed(real_tools):
    tools, gated = real_tools
    destructive = {t.name for t in tools if t.annotations and t.annotations.destructiveHint}
    assert set(gated) == destructive | ALWAYS_CONFIRMED
    assert set(DETAILS) | set(CUSTOM_PREVIEWS) <= set(gated)
    for tool in tools:
        takes_token = TOKEN_PARAMETER in tool.parameters.get("properties", {})
        assert takes_token == (tool.name in gated), tool.name


def test_tool_descriptions_carry_no_assistant_imperatives(real_tools):
    tools, _gated = real_tools
    violations = {
        tool.name: found
        for tool in tools
        if (found := imperative_violations(tool.description or ""))
    }
    assert violations == {}


def test_gated_tool_descriptions_state_that_approval_follows_the_preview(real_tools):
    tools, gated = real_tools
    missing = [
        tool.name
        for tool in tools
        if tool.name in gated
        and APPROVAL_AFTER_PREVIEW not in " ".join((tool.description or "").split())
    ]
    assert missing == []


def test_gated_tool_missing_from_the_spec_fails_startup():
    confirmed = {name: ConfirmedTool(name) for name in ALWAYS_CONFIRMED}
    server._require_gated_tools(confirmed)
    del confirmed["cart_checkout"]
    with pytest.raises(RuntimeError, match="cart_checkout"):
        server._require_gated_tools(confirmed)


def test_build_server_fails_when_a_gated_tool_is_not_in_the_spec(isolated_routes, monkeypatch):
    monkeypatch.setattr(server, "load_spec", lambda: copy.deepcopy(_SPEC))
    with pytest.raises(RuntimeError, match="cart_checkout"):
        server.build_server()


def test_gated_descriptions_describe_the_two_steps(real_tools):
    _tools, gated = real_tools
    overlay = yaml.safe_load(
        (importlib.resources.files("wavix_mcp") / "tool_descriptions.yaml").read_text("utf-8")
    )
    for key in gated:
        assert "has confirmed" not in overlay[key], key
        assert "`confirmation_token`" in overlay[key], key
    assert "runs in two steps" in server.INSTRUCTIONS


def test_build_server_registers_confirmation_inside_scope_filter(isolated_routes, monkeypatch):
    monkeypatch.setattr(server, "ALWAYS_CONFIRMED", frozenset())
    monkeypatch.setattr(server, "load_spec", lambda: copy.deepcopy(_SPEC))
    names = [type(m).__name__ for m in server.build_server().middleware]
    assert names.index("ScopeFilterMiddleware") < names.index("ConfirmationMiddleware")


def test_numeric_looking_text_change_is_shown():
    lookups = {"/v1/trunks/7": {"label": "Office", "rewrite_prefix": "0"}}
    arguments = {"id": 7, "label": "Office", "rewrite_prefix": "00"}
    assert _preview("sip_trunks_update", arguments, lookups) == (
        "This will change SIP trunk Office:\n• rewrite_prefix: 0 → 00"
    )


def test_empty_token_counts_as_a_first_call(isolated_routes, anonymous):
    api = _Api()
    [result] = asyncio.run(
        _calls(_server(api), ("call_control_create", {**CALL, TOKEN_PARAMETER: ""}))
    )
    assert api.dials == 0
    assert _text(result).startswith('This will run "Start a call":')


def test_pending_tokens_are_capped_per_caller(monkeypatch, anonymous):
    monkeypatch.setattr(confirmation, "MAX_PENDING_PER_CALLER", 2)

    async def call_next(_context):
        return ToolResult(content="ok")

    middleware = ConfirmationMiddleware(_BUILDERS, httpx.AsyncClient())

    async def run():
        other = await middleware.on_call_tool(_direct(middleware, CALL, "s2"), call_next)
        for _ in range(3):
            await middleware.on_call_tool(_direct(middleware, CALL), call_next)
        return other

    other = asyncio.run(run())
    assert len(middleware._pending["anonymous:s1"]) == 2
    assert _token(other) in middleware._pending["anonymous:s2"]


def test_expired_tokens_of_idle_callers_are_swept(anonymous):
    clock = _Clock()

    async def call_next(_context):
        return ToolResult(content="ok")

    middleware = ConfirmationMiddleware(_BUILDERS, httpx.AsyncClient(), clock=clock)

    async def run():
        await middleware.on_call_tool(_direct(middleware, CALL, "idle"), call_next)
        clock.now += TOKEN_TTL_S
        await middleware.on_call_tool(_direct(middleware, CALL, "active"), call_next)

    asyncio.run(run())
    assert "anonymous:idle" not in middleware._pending


def test_anonymous_caller_without_a_session_is_refused(anonymous):
    dials = []

    async def call_next(_context):
        dials.append(1)
        return ToolResult(content="ok")

    middleware = ConfirmationMiddleware(_BUILDERS, httpx.AsyncClient())
    context = MiddlewareContext(
        message=CallToolRequestParams(name="call_control_create", arguments=CALL),
        fastmcp_context=SimpleNamespace(),
    )
    result = asyncio.run(middleware.on_call_tool(context, call_next))
    assert result.is_error and dials == [] and middleware._pending == {}


def test_instructions_state_the_token_lifetime():
    assert f"expires after {TOKEN_TTL_S / 60:g} minutes" in server.INSTRUCTIONS


def test_failed_confirm_drops_the_token_from_memory(anonymous):
    async def call_next(_context):
        return ToolResult(content="rejected", is_error=True)

    middleware = ConfirmationMiddleware(_BUILDERS, httpx.AsyncClient())

    async def run():
        preview = await middleware.on_call_tool(_direct(middleware, CALL), call_next)
        token = _token(preview)
        await middleware.on_call_tool(
            _direct(middleware, {**CALL, TOKEN_PARAMETER: token}), call_next
        )
        return token

    token = asyncio.run(run())
    assert token not in middleware._pending.get("anonymous:s1", {})
