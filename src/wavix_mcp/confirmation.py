import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import anyio
import httpx
from fastmcp.server.dependencies import get_access_token, get_http_headers
from fastmcp.server.elicitation import AcceptedElicitation
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import ClientCapabilities
from pydantic import BaseModel, Field, create_model

from .auth import PASSTHROUGH_CLIENT_ID

logger = logging.getLogger(__name__)

CONFIRMATION_TIMEOUT_S = 50.0
REPLAY_WINDOW_S = 60.0

_PHONE_NUMBER = re.compile(r"\+?[0-9]{7,15}")
_ANONYMOUS_CALLER = "anonymous"
_CALL_REFUSAL = (
    "This client cannot show a confirmation prompt, so the call was not placed. "
    "Place calls from the Wavix portal at https://app.wavix.com."
)


class ConfirmationInputError(ValueError):
    pass


@dataclass(frozen=True)
class ConfirmationPrompt:
    message: str
    confirm_label: str
    declined: str
    unanswered: str


PromptBuilder = Callable[[Mapping[str, Any], httpx.AsyncClient], Awaitable[ConfirmationPrompt]]


@dataclass(frozen=True)
class ConfirmationSpec:
    build: PromptBuilder
    refusal: str


def _invalid(name: str) -> ConfirmationInputError:
    return ConfirmationInputError(f"Missing or invalid required argument: {name}")


def _phone_number(arguments: Mapping[str, Any], key: str, name: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not _PHONE_NUMBER.fullmatch(value):
        raise _invalid(name)
    return value


def _call_prompt(destination: str, message: str, label: str) -> ConfirmationPrompt:
    return ConfirmationPrompt(
        message=message,
        confirm_label=label,
        declined=(
            f"The user declined to place the call to {destination}. "
            "Do not retry without new instructions from the user."
        ),
        unanswered=(
            "The user did not answer the confirmation prompt, so the call to "
            f"{destination} was not placed."
        ),
    )


async def _outbound_call_prompt(
    arguments: Mapping[str, Any], _api: httpx.AsyncClient
) -> ConfirmationPrompt:
    caller_id = _phone_number(arguments, "from", "from")
    destination = _phone_number(arguments, "to", "to")
    return _call_prompt(
        destination,
        f"Place a billable phone call from {caller_id} to {destination}?",
        "Place the call",
    )


async def _voice_campaign_prompt(
    arguments: Mapping[str, Any], _api: httpx.AsyncClient
) -> ConfirmationPrompt:
    campaign = arguments.get("voice_campaign")
    if not isinstance(campaign, Mapping):
        raise _invalid("voice_campaign")
    callflow_id = campaign.get("callflow_id")
    if isinstance(callflow_id, bool) or not isinstance(callflow_id, int):
        raise _invalid("voice_campaign.callflow_id")
    caller_id = _phone_number(campaign, "caller_id", "voice_campaign.caller_id")
    destination = _phone_number(campaign, "contact", "voice_campaign.contact")
    return _call_prompt(
        destination,
        f"Launch call flow {callflow_id}: a billable phone call from {caller_id} to {destination}?",
        "Launch the campaign",
    )


CALL_CONFIRMATIONS: Mapping[str, ConfirmationSpec] = {
    "call_control_create": ConfirmationSpec(_outbound_call_prompt, _CALL_REFUSAL),
    "voice_campaigns_create": ConfirmationSpec(_voice_campaign_prompt, _CALL_REFUSAL),
}

CONFIRMATIONS: Mapping[str, ConfirmationSpec] = {**CALL_CONFIRMATIONS}


def supports_form_elicitation(capabilities: ClientCapabilities | None) -> bool:
    elicitation = capabilities.elicitation if capabilities is not None else None
    if elicitation is None:
        return False
    # MCP 2025-11-25: an empty elicitation capability means form mode only.
    return elicitation.form is not None or elicitation.url is None


class ConfirmationForm(BaseModel):
    confirm: bool = True


@cache
def confirmation_form(label: str) -> type[ConfirmationForm]:
    return create_model(
        "Confirm", __base__=ConfirmationForm, confirm=(bool, Field(default=True, title=label))
    )


def _client_capabilities(ctx: Any) -> ClientCapabilities | None:
    try:
        params = ctx.session.client_params
    except (AttributeError, RuntimeError):
        return None
    return params.capabilities if params is not None else None


def _caller_identity() -> str:
    token = get_access_token()
    if token is not None and getattr(token, "client_id", None) == PASSTHROUGH_CLIENT_ID:
        token = None
    subject = (getattr(token, "claims", None) or {}).get("sub")
    if isinstance(subject, str) and subject:
        return f"sub:{subject}"
    try:
        authorization = get_http_headers(include={"authorization"}).get("authorization")
    except (RuntimeError, LookupError):
        authorization = None
    if authorization:
        return "auth:" + hashlib.sha256(authorization.encode()).hexdigest()
    return _ANONYMOUS_CALLER


def _refused(text: str) -> ToolResult:
    return ToolResult(content=text, is_error=True)


@dataclass
class _Accepted:
    done: anyio.Event = field(default_factory=anyio.Event)
    result: ToolResult | None = None
    finished_at: float | None = None
    replayed: bool = False


class ConfirmationMiddleware(Middleware):
    def __init__(
        self,
        specs: Mapping[str, ConfirmationSpec],
        api_client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._specs = dict(specs)
        self._api_client = api_client
        self._clock = clock
        self._accepted: dict[str, _Accepted] = {}

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        spec = self._specs.get(context.message.name)
        if spec is None:
            return await call_next(context)

        ctx: Any = context.fastmcp_context
        arguments = dict(context.message.arguments or {})
        self._purge_expired()
        key = self._replay_key(ctx, context.message.name, arguments)

        replayed = await self._replay(key)
        if replayed is not None:
            return replayed
        seen = self._accepted.get(key) if key is not None else None

        if ctx is None or not supports_form_elicitation(_client_capabilities(ctx)):
            return _refused(spec.refusal)

        try:
            prompt = await spec.build(arguments, self._api_client)
        except ConfirmationInputError as exc:
            return _refused(str(exc))
        except Exception:
            logger.exception("Building the confirmation for %s failed", context.message.name)
            return _refused(spec.refusal)

        try:
            with anyio.fail_after(CONFIRMATION_TIMEOUT_S):
                answer = await ctx.elicit(prompt.message, confirmation_form(prompt.confirm_label))
        except TimeoutError:
            return ToolResult(content=prompt.unanswered)
        except Exception:
            logger.exception("Confirmation prompt for %s failed", context.message.name)
            return _refused(spec.refusal)

        if not (isinstance(answer, AcceptedElicitation) and answer.data.confirm):
            return ToolResult(content=prompt.declined)

        if key is None:
            return await self._dial(context, call_next)
        return await self._join_entry_not_seen_or_dial(key, seen, context, call_next)

    async def _join_entry_not_seen_or_dial(
        self, key: str, seen: _Accepted | None, context: MiddlewareContext, call_next
    ) -> ToolResult:
        while (existing := self._accepted.get(key)) is not None and existing is not seen:
            await existing.done.wait()
            if existing.result is not None:
                existing.replayed = True
                return existing.result
        return await self._run_accepted(key, context, call_next)

    @staticmethod
    async def _dial(context: MiddlewareContext, call_next) -> ToolResult:
        # a client cancel must not abort a request the API may already have accepted
        with anyio.CancelScope(shield=True):
            return await call_next(context)

    async def _replay(self, key: str | None) -> ToolResult | None:
        if key is None:
            return None
        entry = self._accepted.get(key)
        if entry is None or entry.replayed:
            return None
        await entry.done.wait()
        if entry.replayed or entry.result is None:
            return None
        entry.replayed = True
        return entry.result

    async def _run_accepted(self, key: str, context: MiddlewareContext, call_next) -> ToolResult:
        entry = _Accepted()
        self._accepted[key] = entry
        try:
            result = await self._dial(context, call_next)
        except BaseException:
            del self._accepted[key]
            entry.done.set()
            raise
        if result.is_error:
            del self._accepted[key]
        else:
            entry.result = result
            entry.finished_at = self._clock()
        entry.done.set()
        return result

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [
            key
            for key, entry in self._accepted.items()
            if entry.finished_at is not None and now - entry.finished_at > REPLAY_WINDOW_S
        ]
        for key in expired:
            del self._accepted[key]

    def _replay_key(self, ctx: Any, tool: str, arguments: Mapping[str, Any]) -> str | None:
        caller = _caller_identity()
        material: dict[str, Any] = {"caller": caller, "tool": tool, "arguments": arguments}
        if caller == _ANONYMOUS_CALLER:
            try:
                material["session"] = ctx.session_id
            except (AttributeError, RuntimeError):
                return None
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()
