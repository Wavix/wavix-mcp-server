"""User confirmation before a call is placed."""

import asyncio
import copy
import importlib.resources
import json
from types import SimpleNamespace

import anyio
import httpx
import pytest
import yaml
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult
from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import AcceptedElicitation
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import (
    ClientCapabilities,
    ElicitationCapability,
    FormElicitationCapability,
    UrlElicitationCapability,
)

from wavix_mcp import confirmation, scope_filter, server
from wavix_mcp.confirmation import (
    CALL_CONFIRMATIONS,
    ConfirmationMiddleware,
    confirmation_form,
    supports_form_elicitation,
)
from wavix_mcp.scope_filter import ScopeFilterMiddleware

CALL = {"from": "+15550100", "to": "+15550199"}
CAMPAIGN = {
    "voice_campaign": {"callflow_id": 3212, "caller_id": "13123310912", "contact": "16729923812"}
}

_CALL_RESPONSE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"uuid": {"type": "string"}},
}

_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/v1/calls": {
            "post": {
                "operationId": "call_control_create",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "from": {"type": "string"},
                                    "to": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": _CALL_RESPONSE}},
                    }
                },
            }
        },
        "/v1/voice-campaigns": {
            "post": {
                "operationId": "voice_campaigns_create",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "voice_campaign": {
                                        "type": "object",
                                        "properties": {
                                            "callflow_id": {"type": "integer"},
                                            "caller_id": {"type": "string"},
                                            "contact": {"type": "string"},
                                        },
                                    }
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/v1/mydids": {
            "get": {
                "operationId": "my_numbers_list",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": _CALL_RESPONSE}},
                    }
                },
            }
        },
    },
}


class _Api:
    def __init__(self, delay: float = 0.0, status: int = 200):
        self.requests: list[httpx.Request] = []
        self._delay = delay
        self._status = status

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._delay:
            await asyncio.sleep(self._delay)
        return httpx.Response(self._status, json={"uuid": f"call-{len(self.requests)}"})

    @property
    def dials(self) -> int:
        return sum(1 for r in self.requests if r.method == "POST")


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Answer:
    def __init__(self, *actions):
        self.actions = list(actions)
        self.messages: list[str] = []
        self.schemas: list[dict] = []

    async def __call__(self, message, response_type, params, context):
        self.messages.append(message)
        self.schemas.append(params.requestedSchema)
        action = self.actions.pop(0) if self.actions else "accept-true"
        if action == "accept-true":
            return ElicitResult(action="accept", content={"confirm": True})
        if action == "accept-false":
            return ElicitResult(action="accept", content={"confirm": False})
        if action == "accept-empty":
            return ElicitResult(action="accept", content={})
        if action == "sleep":
            await asyncio.sleep(1)
            return ElicitResult(action="accept", content={"confirm": True})
        if action == "raise":
            raise RuntimeError("client failed")
        return ElicitResult(action=action)


@pytest.fixture
def isolated_routes():
    routes = dict(server._TOOL_ROUTES)
    unannotated = list(server._UNANNOTATED)
    yield
    server._TOOL_ROUTES.clear()
    server._TOOL_ROUTES.update(routes)
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
    mcp.add_middleware(ConfirmationMiddleware(CALL_CONFIRMATIONS, client, clock=clock or _Clock()))
    return mcp


async def _call(mcp, handler, tool="call_control_create", arguments=CALL, **kwargs):
    async with Client(mcp, elicitation_handler=handler) as client:
        return await client.call_tool(tool, arguments, raise_on_error=False, **kwargs)


def _text(result) -> str:
    return " ".join(c.text for c in result.content if hasattr(c, "text"))


def test_accept_places_call_after_one_card(isolated_routes, anonymous):
    api, answer = _Api(), _Answer("accept-true")
    result = asyncio.run(_call(_server(api), answer))
    assert not result.is_error
    assert api.dials == 1
    assert len(answer.messages) == 1
    assert "+15550100" in answer.messages[0] and "+15550199" in answer.messages[0]
    assert "billable" in answer.messages[0]
    field = answer.schemas[0]["properties"]["confirm"]
    assert answer.schemas[0]["properties"].keys() == {"confirm"}
    assert field["type"] == "boolean" and field["default"] is True
    assert field["title"] == "Place the call"


def test_accept_with_empty_content_proceeds(isolated_routes, anonymous):
    api = _Api()
    result = asyncio.run(
        _call(_server(api), _Answer("accept-empty"), arguments={**CALL, "to": "+15550177"})
    )
    assert not result.is_error and api.dials == 1


@pytest.mark.parametrize("action", ["decline", "cancel", "accept-false"])
def test_declined_outcomes_place_no_call(isolated_routes, anonymous, action):
    api = _Api()
    result = asyncio.run(_call(_server(api), _Answer(action)))
    assert api.dials == 0
    assert not result.is_error
    assert _text(result).startswith("The user declined to place the call to +15550199")


def test_gated_tools_drop_output_schema(isolated_routes, anonymous):
    async def run():
        async with Client(_server(_Api()), elicitation_handler=_Answer("decline")) as client:
            tools = {t.name: t for t in await client.list_tools()}
            declined = await client.call_tool("call_control_create", CALL)
            return tools, declined

    tools, declined = asyncio.run(run())
    assert tools["call_control_create"].outputSchema is None
    assert tools["my_numbers_list"].outputSchema is not None
    assert not declined.is_error


def test_replay_window(isolated_routes, anonymous):
    api, clock = _Api(), _Clock()
    mcp = _server(api, clock)
    answer = _Answer()

    async def run():
        async with Client(mcp, elicitation_handler=answer) as client:
            first = await client.call_tool("call_control_create", CALL)
            replay = await client.call_tool("call_control_create", CALL)
            third = await client.call_tool("call_control_create", CALL)
            changed = await client.call_tool("call_control_create", {**CALL, "to": "+15550111"})
            clock.now += 61
            late = await client.call_tool("call_control_create", {**CALL, "to": "+15550111"})
            return first, replay, third, changed, late

    first, replay, third, changed, late = asyncio.run(run())
    assert replay.structured_content == first.structured_content
    assert len(answer.messages) == 4
    assert api.dials == 4


def test_replay_after_failed_call_shows_new_card(isolated_routes, anonymous):
    api, answer = _Api(status=500), _Answer()

    async def run():
        async with Client(_server(api), elicitation_handler=answer) as client:
            await client.call_tool("call_control_create", CALL, raise_on_error=False)
            await client.call_tool("call_control_create", CALL, raise_on_error=False)

    asyncio.run(run())
    assert len(answer.messages) == 2


def test_replay_after_decline_shows_new_card(isolated_routes, anonymous):
    api, answer = _Api(), _Answer("decline", "accept-true")

    async def run():
        async with Client(_server(api), elicitation_handler=answer) as client:
            await client.call_tool("call_control_create", CALL)
            await client.call_tool("call_control_create", CALL)

    asyncio.run(run())
    assert len(answer.messages) == 2 and api.dials == 1


def test_replay_joins_in_flight_call(isolated_routes, anonymous):
    api, answer = _Api(delay=0.5), _Answer()
    mcp = _server(api)

    async def run():
        async with Client(mcp, elicitation_handler=answer) as client:
            first = asyncio.create_task(client.call_tool("call_control_create", CALL))
            await asyncio.sleep(0.1)
            second = await client.call_tool("call_control_create", CALL)
            return await first, second

    first, second = asyncio.run(run())
    assert api.dials == 1 and len(answer.messages) == 1
    assert second.structured_content == first.structured_content


def test_two_accepted_cards_place_one_call(isolated_routes, anonymous):
    api = _Api(delay=0.5)

    async def slow_accept(message, response_type, params, context):
        await asyncio.sleep(0.1)
        return ElicitResult(action="accept", content={"confirm": True})

    async def run():
        async with Client(_server(api), elicitation_handler=slow_accept) as client:
            return await asyncio.gather(
                client.call_tool("call_control_create", CALL),
                client.call_tool("call_control_create", CALL),
            )

    first, second = asyncio.run(run())
    assert api.dials == 1
    assert first.structured_content == second.structured_content


def test_identified_caller_survives_a_new_session(isolated_routes, monkeypatch):
    first_user = SimpleNamespace(claims={"sub": "user-1"}, scopes=[])
    second_user = SimpleNamespace(claims={"sub": "user-2"}, scopes=[])
    api, answer = _Api(), _Answer()
    mcp = _server(api)

    monkeypatch.setattr(confirmation, "get_access_token", lambda: first_user)
    asyncio.run(_call(mcp, answer))
    assert api.dials == 1 and len(answer.messages) == 1

    monkeypatch.setattr(confirmation, "get_access_token", lambda: second_user)
    asyncio.run(_call(mcp, answer))
    assert api.dials == 2 and len(answer.messages) == 2

    monkeypatch.setattr(confirmation, "get_access_token", lambda: first_user)
    asyncio.run(_call(mcp, answer))
    assert api.dials == 2 and len(answer.messages) == 2


def test_api_key_callers_are_told_apart_by_authorization(isolated_routes, monkeypatch):
    monkeypatch.setattr(confirmation, "get_access_token", lambda: None)
    headers = {"authorization": "Bearer A"}
    monkeypatch.setattr(confirmation, "get_http_headers", lambda **_: headers)
    api, answer = _Api(), _Answer()
    mcp = _server(api)

    asyncio.run(_call(mcp, answer))
    assert api.dials == 1 and len(answer.messages) == 1

    headers["authorization"] = "Bearer B"
    asyncio.run(_call(mcp, answer))
    assert api.dials == 2 and len(answer.messages) == 2

    headers["authorization"] = "Bearer A"
    asyncio.run(_call(mcp, answer))
    assert api.dials == 2 and len(answer.messages) == 2


def test_anonymous_caller_without_session_always_asks(monkeypatch):
    monkeypatch.setattr(confirmation, "get_access_token", lambda: None)
    middleware = ConfirmationMiddleware(CALL_CONFIRMATIONS, httpx.AsyncClient())
    ctx = SimpleNamespace()
    assert middleware._replay_key(ctx, "call_control_create", CALL) is None


class _Direct:
    def __init__(
        self,
        call_next,
        clock: _Clock | None = None,
        capabilities: ClientCapabilities | None = None,
    ):
        self.cards: list[str] = []
        self.middleware = ConfirmationMiddleware(
            CALL_CONFIRMATIONS, httpx.AsyncClient(), clock=clock or _Clock()
        )
        self._call_next = call_next

        async def elicit(message, response_type):
            self.cards.append(message)
            return AcceptedElicitation(data=response_type(confirm=True))

        self._ctx = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    capabilities=capabilities
                    or ClientCapabilities(elicitation=ElicitationCapability())
                )
            ),
            session_id="s1",
            elicit=elicit,
        )

    async def call(self):
        context = MiddlewareContext(
            message=SimpleNamespace(name="call_control_create", arguments=dict(CALL)),
            fastmcp_context=self._ctx,
        )
        return await self.middleware.on_call_tool(context, self._call_next)


def test_cancelled_client_still_records_outcome(anonymous):
    dials = []

    async def call_next(_context):
        await anyio.sleep(0.2)
        dials.append(1)
        return ToolResult(structured_content={"uuid": "call-1"})

    direct = _Direct(call_next)

    async def run():
        with anyio.move_on_after(0.05):
            await direct.call()
        return await direct.call()

    replay = asyncio.run(run())
    assert dials == [1] and len(direct.cards) == 1
    assert replay.structured_content == {"uuid": "call-1"}


def test_cancelled_replay_leaves_the_entry_intact(anonymous):
    dials = []

    async def call_next(_context):
        await anyio.sleep(0.5)
        dials.append(1)
        return ToolResult(structured_content={"uuid": "call-1"})

    direct = _Direct(call_next)

    async def run():
        first = asyncio.create_task(direct.call())
        await asyncio.sleep(0.1)
        with anyio.move_on_after(0.1):
            await direct.call()
        await first
        return await direct.call()

    replay = asyncio.run(run())
    assert dials == [1] and len(direct.cards) == 1
    assert replay.structured_content == {"uuid": "call-1"}


def test_in_flight_call_outliving_the_window_is_replayed(anonymous):
    clock = _Clock()
    dials = []

    async def call_next(_context):
        clock.now += 120
        dials.append(1)
        return ToolResult(structured_content={"uuid": "call-1"})

    direct = _Direct(call_next, clock)

    async def run():
        await direct.call()
        return await direct.call()

    replay = asyncio.run(run())
    assert dials == [1] and len(direct.cards) == 1
    assert replay.structured_content == {"uuid": "call-1"}


def test_error_result_is_not_replayed(anonymous):
    async def call_next(_context):
        return ToolResult(content="rejected", is_error=True)

    direct = _Direct(call_next)

    async def run():
        await direct.call()
        await direct.call()

    asyncio.run(run())
    assert len(direct.cards) == 2


def test_concurrent_replays_share_one_result(anonymous):
    dials = []

    async def call_next(_context):
        await anyio.sleep(0.3)
        dials.append(1)
        return ToolResult(structured_content={"uuid": f"call-{len(dials)}"})

    direct = _Direct(call_next)

    async def run():
        first = asyncio.create_task(direct.call())
        await asyncio.sleep(0.1)
        with anyio.fail_after(5):
            replays = await asyncio.gather(direct.call(), direct.call())
        await first
        return replays

    replays = asyncio.run(run())
    assert len(direct.cards) == 2 and len(dials) == 2
    assert sorted(r.structured_content["uuid"] for r in replays) == ["call-1", "call-2"]


def test_raising_in_flight_call_releases_waiting_replay(anonymous):
    attempts = []

    async def call_next(_context):
        attempts.append(1)
        if len(attempts) == 1:
            await anyio.sleep(0.3)
            raise RuntimeError("network")
        return ToolResult(structured_content={"uuid": "call-2"})

    direct = _Direct(call_next)

    async def run():
        first = asyncio.create_task(direct.call())
        await asyncio.sleep(0.1)
        with anyio.fail_after(5):
            second = await direct.call()
        with pytest.raises(RuntimeError):
            await first
        return second

    second = asyncio.run(run())
    assert len(attempts) == 2 and len(direct.cards) == 2
    assert second.structured_content == {"uuid": "call-2"}


def test_joiners_of_a_failed_call_dial_once_more(isolated_routes, anonymous):
    attempts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        await asyncio.sleep(0.3)
        status = 500 if len(attempts) == 1 else 200
        return httpx.Response(status, json={"uuid": f"call-{len(attempts)}"})

    async def slow_accept(message, response_type, params, context):
        await asyncio.sleep(0.1)
        return ElicitResult(action="accept", content={"confirm": True})

    client = httpx.AsyncClient(base_url="http://api.test", transport=httpx.MockTransport(handler))
    mcp = FastMCP.from_openapi(
        openapi_spec=_SPEC, client=client, name="t", mcp_component_fn=server._xmcp_component_fn
    )
    mcp.add_middleware(ConfirmationMiddleware(CALL_CONFIRMATIONS, client))

    async def run():
        async with Client(mcp, elicitation_handler=slow_accept) as session:
            with anyio.fail_after(5):
                return await asyncio.gather(
                    *(
                        session.call_tool("call_control_create", CALL, raise_on_error=False)
                        for _ in range(3)
                    )
                )

    results = asyncio.run(run())
    assert len(attempts) == 2
    assert sum(1 for r in results if not r.is_error) == 2


def test_build_server_registers_confirmation_inside_scope_filter(isolated_routes, monkeypatch):
    monkeypatch.setattr(server, "load_spec", lambda: copy.deepcopy(_SPEC))
    names = [type(m).__name__ for m in server.build_server().middleware]
    assert names.index("ScopeFilterMiddleware") < names.index("ConfirmationMiddleware")


def test_client_without_elicitation_is_refused(isolated_routes, anonymous):
    api = _Api()
    result = asyncio.run(_call(_server(api), None))
    assert api.dials == 0
    assert result.is_error
    assert "https://app.wavix.com" in _text(result)


def test_client_without_elicitation_is_never_sent_a_card(anonymous):
    dials = []

    async def call_next(_context):
        dials.append(1)
        return ToolResult(structured_content={"uuid": "call-1"})

    direct = _Direct(call_next, capabilities=ClientCapabilities())
    result = asyncio.run(direct.call())
    assert result.is_error and direct.cards == [] and dials == []


@pytest.mark.parametrize(
    ("capabilities", "supported"),
    [
        (None, False),
        (ClientCapabilities(), False),
        (
            ClientCapabilities(elicitation=ElicitationCapability(url=UrlElicitationCapability())),
            False,
        ),
        (ClientCapabilities(elicitation=ElicitationCapability()), True),
        (
            ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability())),
            True,
        ),
        (
            ClientCapabilities(
                elicitation=ElicitationCapability(
                    form=FormElicitationCapability(), url=UrlElicitationCapability()
                )
            ),
            True,
        ),
    ],
)
def test_form_elicitation_support(capabilities, supported):
    assert supports_form_elicitation(capabilities) is supported


def test_unanswered_card_places_no_call(isolated_routes, anonymous, monkeypatch):
    monkeypatch.setattr(confirmation, "CONFIRMATION_TIMEOUT_S", 0.05)
    api, answer = _Api(), _Answer("sleep", "decline")
    mcp = _server(api)

    async def run():
        async with Client(mcp, elicitation_handler=answer) as client:
            first = await client.call_tool("call_control_create", CALL)
            await asyncio.sleep(1.1)
            await client.call_tool("call_control_create", CALL)
            return first

    first = asyncio.run(run())
    assert api.dials == 0
    assert not first.is_error
    assert "did not answer" in _text(first)
    assert len(answer.messages) == 2


def test_scope_denial_precedes_card(isolated_routes, anonymous, monkeypatch):
    monkeypatch.setattr(
        scope_filter,
        "get_access_token",
        lambda: SimpleNamespace(scopes=["numbers:read"], claims={}),
    )
    api, answer = _Api(), _Answer()
    with pytest.raises(ToolError, match="calls:write"):
        asyncio.run(_call_raising(_server(api, with_scope_filter=True), answer))
    assert answer.messages == [] and api.dials == 0


async def _call_raising(mcp, handler):
    async with Client(mcp, elicitation_handler=handler) as client:
        return await client.call_tool("call_control_create", CALL)


def test_voice_campaign_card(isolated_routes, anonymous):
    api, answer = _Api(), _Answer()
    result = asyncio.run(
        _call(_server(api), answer, tool="voice_campaigns_create", arguments=CAMPAIGN)
    )
    assert not result.is_error and api.dials == 1
    message = answer.messages[0]
    for value in ("3212", "13123310912", "16729923812", "billable"):
        assert value in message
    assert answer.schemas[0]["properties"]["confirm"]["title"] == "Launch the campaign"


@pytest.mark.parametrize(
    ("tool", "arguments", "field"),
    [
        ("call_control_create", {"from": "+15550100"}, "to"),
        ("call_control_create", {"from": "+15550100", "to": "  "}, "to"),
        ("call_control_create", {"from": 15550100, "to": "+15550199"}, "from"),
        ("call_control_create", {"from": "+15550100", "to": "+1 555 0100 or +1 555 0199"}, "to"),
        ("call_control_create", {"from": "+15550100", "to": "+15550199 +15550100"}, "to"),
        ("voice_campaigns_create", {"voice_campaign": "x"}, "voice_campaign"),
        (
            "voice_campaigns_create",
            {"voice_campaign": {**CAMPAIGN["voice_campaign"], "callflow_id": True}},
            "voice_campaign.callflow_id",
        ),
        (
            "voice_campaigns_create",
            {"voice_campaign": {**CAMPAIGN["voice_campaign"], "callflow_id": "3212"}},
            "voice_campaign.callflow_id",
        ),
        (
            "voice_campaigns_create",
            {"voice_campaign": {**CAMPAIGN["voice_campaign"], "contact": ""}},
            "voice_campaign.contact",
        ),
        (
            "voice_campaigns_create",
            {"voice_campaign": {**CAMPAIGN["voice_campaign"], "caller_id": None}},
            "voice_campaign.caller_id",
        ),
    ],
)
def test_missing_argument_fails_closed(isolated_routes, anonymous, tool, arguments, field):
    api, answer = _Api(), _Answer()
    result = asyncio.run(_call(_server(api), answer, tool=tool, arguments=arguments))
    assert result.is_error
    assert _text(result) == f"Missing or invalid required argument: {field}"
    assert answer.messages == [] and api.dials == 0


def test_failure_before_answer_fails_closed(isolated_routes, anonymous):
    api = _Api()
    result = asyncio.run(_call(_server(api), _Answer("raise")))
    assert result.is_error and "cannot show a confirmation prompt" in _text(result)
    assert api.dials == 0

    async def broken(arguments, api_client):
        raise RuntimeError("lookup failed")

    specs = {"call_control_create": confirmation.ConfirmationSpec(broken, "refused")}
    client = httpx.AsyncClient(base_url="http://api.test", transport=httpx.MockTransport(api))
    mcp = FastMCP.from_openapi(openapi_spec=_SPEC, client=client, name="t")
    mcp.add_middleware(ConfirmationMiddleware(specs, client))
    answer = _Answer()
    result = asyncio.run(_call(mcp, answer))
    assert result.is_error and _text(result) == "refused"
    assert answer.messages == [] and api.dials == 0


def test_call_wording_says_tool_asks():
    overlay = yaml.safe_load(
        (importlib.resources.files("wavix_mcp") / "tool_descriptions.yaml").read_text("utf-8")
    )
    for key in ("call_control_create", "voice_campaigns_create"):
        assert "has confirmed" not in overlay[key]
        assert "asks the user" in overlay[key]
    assert "voice_campaigns_create ask the user to confirm" in server.INSTRUCTIONS


def test_confirmation_form_is_one_prechecked_boolean():
    schema = confirmation_form("Place the call").model_json_schema()
    assert json.loads(json.dumps(schema))["properties"]["confirm"]["default"] is True
