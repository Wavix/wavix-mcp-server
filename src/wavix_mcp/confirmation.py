import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import anyio
import httpx
from fastmcp.server.dependencies import get_access_token, get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

from .auth import PASSTHROUGH_CLIENT_ID

logger = logging.getLogger(__name__)

TOKEN_PARAMETER = "confirmation_token"
TOKEN_TTL_S = 300.0
MAX_LISTED_NUMBERS = 20
MAX_PENDING_PER_CALLER = 100
MESSAGE_PREVIEW_CHARS = 80

_PHONE_NUMBER = re.compile(r"\+?[0-9]{7,15}")
_ANONYMOUS_CALLER = "anonymous"
_TOKEN_MISMATCH = (
    "The confirmation token has expired or does not match these arguments. Nothing was done."
)
_PREVIEW_FAILED = "The confirmation preview could not be prepared, so nothing was done."
_NO_CALLER = "This action needs an authenticated MCP session, so nothing was done."
_SECRET_FIELDS = frozenset({"password"})
_HEADER_FIELDS = ("text", "placeholder", "parameter_name", "media_url", "filename")


class ConfirmationInputError(ValueError):
    pass


PreviewBuilder = Callable[[Mapping[str, Any], httpx.AsyncClient], Awaitable[str | None]]


def _invalid(name: str) -> ConfirmationInputError:
    return ConfirmationInputError(f"Missing or invalid required argument: {name}")


def _phone_number(arguments: Mapping[str, Any], key: str, name: str | None = None) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not _PHONE_NUMBER.fullmatch(value):
        raise _invalid(name or key)
    return value


def _text(arguments: Mapping[str, Any], key: str, name: str | None = None) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(name or key)
    return value


def _integer(arguments: Mapping[str, Any], key: str, name: str | None = None) -> int:
    value = arguments.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(name or key)
    return value


def _mapping(arguments: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = arguments.get(key)
    if not isinstance(value, Mapping):
        raise _invalid(key)
    return value


async def _fetch(api: httpx.AsyncClient, path: str, **params: Any) -> Any:
    try:
        response = await api.get(path, params=params or None)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        logger.info("Confirmation preview lookup %s failed", path, exc_info=True)
        return None


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _bullets(lines: Iterable[str]) -> str:
    return "\n".join(f"• {line}" for line in lines)


def _place(entity: Mapping[str, Any]) -> str:
    return ", ".join(str(entity[k]) for k in ("city", "country") if entity.get(k))


def _number_line(entity: Mapping[str, Any] | None, fallback: str) -> str:
    if not isinstance(entity, Mapping) or not entity.get("number"):
        return fallback
    place = _place(entity)
    return f"{entity['number']} ({place})" if place else str(entity["number"])


async def _outbound_call(arguments: Mapping[str, Any], _api: httpx.AsyncClient) -> str:
    caller_id = _phone_number(arguments, "from")
    destination = _phone_number(arguments, "to")
    preview = f"This will place a billable call from {caller_id} to {destination}."
    if arguments.get("recording") is True:
        preview += " The call will be recorded."
    return preview


async def _voice_campaign(arguments: Mapping[str, Any], _api: httpx.AsyncClient) -> str:
    campaign = _mapping(arguments, "voice_campaign")
    callflow_id = _integer(campaign, "callflow_id", "voice_campaign.callflow_id")
    caller_id = _phone_number(campaign, "caller_id", "voice_campaign.caller_id")
    destination = _phone_number(campaign, "contact", "voice_campaign.contact")
    return (
        f"This will launch call flow {callflow_id}: "
        f"a billable call from {caller_id} to {destination}."
    )


def _streamed_audio(arguments: Mapping[str, Any], party: str) -> tuple[str, str]:
    stream_url = _text(arguments, "stream_url")
    two_way = arguments.get("stream_type") == "twoway"
    talk_back = " The destination can also play audio into the call." if two_way else ""
    return f"including the {party}'s voice, to {stream_url}.", talk_back


async def _call_stream(arguments: Mapping[str, Any], _api: httpx.AsyncClient) -> str:
    call_id = _text(arguments, "call_id")
    audio, talk_back = _streamed_audio(arguments, "other party")
    return f"This will stream the live audio of call {call_id}, {audio}{talk_back}"


async def _call_answer(arguments: Mapping[str, Any], _api: httpx.AsyncClient) -> str | None:
    if arguments.get("stream_url") is None:
        return None
    call_id = _text(arguments, "id")
    audio, talk_back = _streamed_audio(arguments, "caller")
    return f"This will answer call {call_id} and stream its live audio, {audio}{talk_back}"


async def _call_webhook(arguments: Mapping[str, Any], _api: httpx.AsyncClient) -> str:
    url = _text(arguments, "url")
    event_type = _text(arguments, "event_type")
    return f"This will send call metadata for every {event_type} event to {url}."


def _capped(lines: list[str], total: int) -> list[str]:
    shown = lines[:MAX_LISTED_NUMBERS]
    if total > len(shown):
        shown.append(f"and {total - len(shown)} more")
    return shown


async def _lookup_all(keys: list[Any], lookup: Callable[[Any], Awaitable[str]]) -> list[str]:
    lines = [""] * len(keys)

    async def run(index: int, key: Any) -> None:
        lines[index] = await lookup(key)

    async with anyio.create_task_group() as group:
        for index, key in enumerate(keys):
            group.start_soon(run, index, key)
    return lines


async def _number_by_id(api: httpx.AsyncClient, number_id: int) -> str:
    entity = await _fetch(api, f"/v1/numbers/{number_id}")
    return _number_line(entity, f"number ID {number_id}")


async def _number_by_did(api: httpx.AsyncClient, did: str) -> str:
    page = await _fetch(api, "/v1/numbers", search=did)
    items = page.get("items") if isinstance(page, Mapping) else None
    match = next(
        (
            item
            for item in items or []
            if isinstance(item, Mapping) and str(item.get("number")).lstrip("+") == did
        ),
        None,
    )
    return _number_line(match, did)


async def _release_numbers(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> str:
    ids, dids = arguments.get("ids"), arguments.get("dids")
    if (ids is None) == (dids is None):
        raise ConfirmationInputError("Pass exactly one of ids or dids")
    if ids is not None:
        if not isinstance(ids, list) or not ids:
            raise _invalid("ids")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in ids):
            raise _invalid("ids")
        total = len(ids)
        lines = await _lookup_all(
            ids[:MAX_LISTED_NUMBERS], lambda number_id: _number_by_id(api, number_id)
        )
    else:
        selected = [d.strip().lstrip("+") for d in _text(arguments, "dids").split(",")]
        selected = [did for did in selected if did]
        if not selected:
            raise _invalid("dids")
        total = len(selected)
        lines = await _lookup_all(
            selected[:MAX_LISTED_NUMBERS], lambda did: _number_by_did(api, did)
        )
    heading = f"This will release {_plural(total, 'number')} back to stock (irreversible):"
    return f"{heading}\n{_bullets(_capped(lines, total))}"


def _display(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(
            _display({k: v for k, v in item.items() if k != "id"})
            if isinstance(item, Mapping)
            else _display(item)
            for item in value
        )
    if isinstance(value, Mapping):
        return ", ".join(_display(v) for v in value.values())
    return json.dumps(value)


def _same(old: Any, new: Any) -> bool:
    if _display(old) == _display(new):
        return True
    if not any(isinstance(v, int | float) and not isinstance(v, bool) for v in (old, new)):
        return False
    try:
        return Decimal(_display(old)) == Decimal(_display(new))
    except InvalidOperation:
        return False


def _flatten(values: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in values.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(_flatten(value, f"{name}."))
        else:
            flat[name] = value
    return flat


def _changes(
    arguments: Mapping[str, Any],
    current: Any,
    skip: Iterable[str],
    aliases: Mapping[str, str],
) -> list[str]:
    known = _flatten(current) if isinstance(current, Mapping) else None
    lines = []
    for field, new in _flatten(arguments).items():
        if field in skip or field == TOKEN_PARAMETER:
            continue
        if field in _SECRET_FIELDS:
            lines.append(f"{field}: (hidden)")
            continue
        if known is None:
            lines.append(f"{field}: {_display(new)}")
            continue
        old = known.get(aliases.get(field, field))
        if not _same(old, new):
            lines.append(f"{field}: {_display(old)} → {_display(new)}")
    return lines


def _change_preview(subject: str, lines: list[str]) -> str:
    if not lines:
        return f"This will change {subject}."
    return f"This will change {subject}:\n{_bullets(lines)}"


async def _update_trunk(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> str:
    trunk_id = _integer(arguments, "id")
    current = await _fetch(api, f"/v1/trunks/{trunk_id}")
    label = current.get("label") if isinstance(current, Mapping) else None
    lines = _changes(arguments, current, {"id"}, {"host_request.host": "host"})
    return _change_preview(f"SIP trunk {label or trunk_id}", lines)


async def _delete_trunk(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> str:
    trunk_id = _integer(arguments, "id")
    current = await _fetch(api, f"/v1/trunks/{trunk_id}")
    known = current if isinstance(current, Mapping) else {}
    caller_id = f" with Caller ID {known['callerid']}" if known.get("callerid") else ""
    return f"This will delete SIP trunk {known.get('label') or trunk_id}{caller_id} (irreversible)."


async def _update_sub_account(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> str:
    account_id = _integer(arguments, "id")
    current = await _fetch(api, f"/v1/sub-organizations/{account_id}")
    name = current.get("name") if isinstance(current, Mapping) else None
    lines = _changes(arguments, current, {"id"}, {})
    return _change_preview(f"sub-account {name or account_id}", lines)


def _named(name: Any, identifier: str) -> str:
    return f"{name} ({identifier})" if isinstance(name, str) and name else identifier


async def _delete_brand(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> str:
    brand_id = _text(arguments, "brand_id")
    brand = await _fetch(api, f"/v3/10dlc/brands/{brand_id}")
    name = (
        (brand.get("dba_name") or brand.get("company_name")) if isinstance(brand, Mapping) else None
    )
    return f"This will delete 10DLC brand {_named(name, brand_id)} (irreversible)."


async def _delete_campaign(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> str:
    brand_id = _text(arguments, "brand_id")
    campaign_id = _text(arguments, "campaign_id")
    campaign = await _fetch(api, f"/v3/10dlc/brands/{brand_id}/campaigns/{campaign_id}")
    known = campaign if isinstance(campaign, Mapping) else {}
    preview = (
        f"This will delete 10DLC campaign {_named(known.get('name'), campaign_id)} (irreversible)."
    )
    numbers = [n for n in known.get("phone_numbers") or [] if isinstance(n, str) and n]
    if not numbers:
        return preview
    stopping = (
        f"These numbers will stop sending through it:\n{_bullets(_capped(numbers, len(numbers)))}"
    )
    return f"{preview}\n{stopping}"


def _excerpt(text: str) -> str:
    if len(text) <= MESSAGE_PREVIEW_CHARS:
        return text
    return text[:MESSAGE_PREVIEW_CHARS].rstrip() + "…"


async def _send_sms(arguments: Mapping[str, Any], _api: httpx.AsyncClient) -> str:
    sender = _text(arguments, "from")
    destination = _text(arguments, "to")
    body = _mapping(arguments, "message_body")
    text = body.get("text")
    media = [url for url in body.get("media") or [] if isinstance(url, str) and url]
    heading = f"This will send a billable message from {sender} to {destination}"
    heading += f': "{_excerpt(text)}"' if isinstance(text, str) and text else "."
    if not media:
        return heading
    return f"{heading}\n{_bullets(f'media: {url}' for url in media)}"


async def _send_whatsapp(arguments: Mapping[str, Any], _api: httpx.AsyncClient) -> str:
    sender = _text(arguments, "from")
    destination = _text(arguments, "to")
    template = _mapping(arguments, "template")
    name = _text(template, "name", "template.name")
    language = _text(template, "language", "template.language")
    placeholders = [p for p in template.get("placeholders") or [] if isinstance(p, str)]
    values = f", values: {', '.join(placeholders)}" if placeholders else ""
    preview = (
        f"This will send a billable message from {sender} to {destination}: "
        f'template "{name}" ({language}){values}.'
    )
    extras = _template_extras(template)
    return f"{preview}\n{_bullets(extras)}" if extras else preview


def _template_extras(template: Mapping[str, Any]) -> list[str]:
    extras = []
    header = template.get("header")
    if isinstance(header, Mapping):
        parts = [str(header[k]) for k in _HEADER_FIELDS if header.get(k) not in (None, "")]
        if header.get("latitude") is not None and header.get("longitude") is not None:
            parts.append(f"{header['latitude']}, {header['longitude']}")
        extras.append(f"header ({header.get('type')}): {', '.join(parts)}".rstrip(": "))
    for button in template.get("buttons") or []:
        if isinstance(button, Mapping):
            parameter = button.get("parameter")
            line = f"button ({button.get('type')})"
            extras.append(f"{line}: {parameter}" if parameter else line)
    return extras


async def _send_otp(arguments: Mapping[str, Any], _api: httpx.AsyncClient) -> str:
    channel = _text(arguments, "channel")
    destination = _phone_number(arguments, "to")
    service_id = _text(arguments, "service_id")
    return (
        f"This will send a billable verification code by {channel} to {destination} "
        f"from 2FA service {service_id}."
    )


async def _resend_otp(arguments: Mapping[str, Any], _api: httpx.AsyncClient) -> str:
    channel = _text(arguments, "channel")
    session_id = _text(arguments, "session_id")
    return (
        f"This will resend a billable verification code by {channel} for verification {session_id}."
    )


def _fee(entity: Mapping[str, Any], key: str) -> Decimal | None:
    try:
        return Decimal(str(entity.get(key)))
    except InvalidOperation:
        return None


async def _checkout(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> str:
    ids = arguments.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
        raise _invalid("ids")
    cart = await _fetch(api, "/v1/buy/cart")
    dids = cart.get("dids") if isinstance(cart, Mapping) else None
    by_id = {str(d.get("id")): d for d in dids or [] if isinstance(d, Mapping)}
    lines, fees = [], []
    for cart_id in ids:
        entity = by_id.get(cart_id)
        lines.append(_number_line(entity, f"cart item {cart_id}"))
        if entity is not None:
            fees += [_fee(entity, "activation_fee"), _fee(entity, "monthly_fee")]
    count = _plural(len(ids), "number")
    if len(fees) == 2 * len(ids) and all(fee is not None for fee in fees):
        total = sum(fee for fee in fees if fee is not None)
        heading = f"This will buy {count} for {total}, charged to your balance:"
    else:
        heading = f"This will buy {count}, charged to your balance:"
    return f"{heading}\n{_bullets(lines)}"


CONFIRMATIONS: Mapping[str, PreviewBuilder] = {
    "call_control_create": _outbound_call,
    "voice_campaigns_create": _voice_campaign,
    "call_control_streams_create": _call_stream,
    "call_control_answer": _call_answer,
    "call_webhooks_create": _call_webhook,
    "my_numbers_delete": _release_numbers,
    "sip_trunks_delete": _delete_trunk,
    "sip_trunks_update": _update_trunk,
    "sub_accounts_update": _update_sub_account,
    "ten_dlc_brands_delete": _delete_brand,
    "ten_dlc_brand_campaigns_delete": _delete_campaign,
    "sms_and_mms_messages_send": _send_sms,
    "whatsapp_messages_send": _send_whatsapp,
    "two_fa_verification_create": _send_otp,
    "two_fa_verification_resend": _resend_otp,
    "cart_checkout": _checkout,
}


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
class _Pending:
    binding: str
    expires_at: float
    done: anyio.Event | None = None
    result: ToolResult | None = None


class ConfirmationMiddleware(Middleware):
    def __init__(
        self,
        builders: Mapping[str, PreviewBuilder],
        api_client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._builders = dict(builders)
        self._api_client = api_client
        self._clock = clock
        self._pending: dict[str, dict[str, _Pending]] = {}
        self._swept_at = clock()

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool = context.message.name
        build = self._builders.get(tool)
        if build is None:
            return await call_next(context)

        arguments = dict(context.message.arguments or {})
        token = arguments.pop(TOKEN_PARAMETER, None) or None
        stripped = context.copy(message=context.message.model_copy(update={"arguments": arguments}))

        caller = self._caller(context.fastmcp_context)
        if caller is None:
            return _refused(_NO_CALLER)
        tokens = self._tokens(caller)
        binding = self._binding(caller, tool, arguments)
        pending = tokens.get(str(token)) if token is not None else None
        if pending is not None and pending.binding == binding:
            if pending.done is None:
                return await self._run_once(caller, str(token), pending, stripped, call_next)
            await pending.done.wait()
            if pending.result is not None:
                return pending.result
        elif pending is not None:
            del tokens[str(token)]

        try:
            preview = await build(arguments, self._api_client)
        except ConfirmationInputError as exc:
            return _refused(str(exc))
        except Exception:
            logger.exception("Building the confirmation preview for %s failed", tool)
            return _refused(_PREVIEW_FAILED)
        if preview is None:
            return await call_next(stripped)
        if token is not None:
            preview = f"{_TOKEN_MISMATCH}\n\n{preview}"

        issued = secrets.token_urlsafe(24)
        tokens = self._pending.setdefault(caller, {})
        if len(tokens) >= MAX_PENDING_PER_CALLER:
            del tokens[next(iter(tokens))]
        tokens[issued] = _Pending(binding, self._clock() + TOKEN_TTL_S)
        return ToolResult(
            content=(
                f"{preview}\nShow this preview to the user and wait for their reply. "
                f'Call this tool again with {TOKEN_PARAMETER}="{issued}" only after the user '
                "approves it in a later message; approval given before this preview does not count."
            )
        )

    async def _run_once(
        self,
        caller: str,
        token: str,
        pending: _Pending,
        context: MiddlewareContext,
        call_next,
    ) -> ToolResult:
        pending.done = anyio.Event()
        result: ToolResult | None = None
        try:
            # a client cancel must not abort a request the API may already have accepted
            with anyio.CancelScope(shield=True):
                result = await call_next(context)
            return result
        finally:
            tokens = self._pending.setdefault(caller, {})
            if result is not None and not result.is_error:
                pending.result = result
                pending.expires_at = self._clock() + TOKEN_TTL_S
                tokens.pop(token, None)
                tokens[token] = pending
            elif tokens.get(token) is pending:
                del tokens[token]
            pending.done.set()

    def _tokens(self, caller: str) -> dict[str, _Pending]:
        now = self._clock()
        if now - self._swept_at >= TOKEN_TTL_S:
            self._swept_at = now
            for other in list(self._pending):
                self._purge(other, now)
        else:
            self._purge(caller, now)
        return self._pending.setdefault(caller, {})

    def _purge(self, caller: str, now: float) -> None:
        tokens = self._pending.get(caller)
        if tokens is None:
            return
        while tokens:
            oldest = next(iter(tokens))
            if tokens[oldest].expires_at > now:
                break
            del tokens[oldest]
        if not tokens:
            del self._pending[caller]

    @staticmethod
    def _caller(ctx: Any) -> str | None:
        caller = _caller_identity()
        if caller != _ANONYMOUS_CALLER:
            return caller
        try:
            return f"{_ANONYMOUS_CALLER}:{ctx.session_id}"
        except (AttributeError, RuntimeError):
            return None

    @staticmethod
    def _binding(caller: str, tool: str, arguments: Mapping[str, Any]) -> str:
        material = {"caller": caller, "tool": tool, "arguments": arguments}
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()
