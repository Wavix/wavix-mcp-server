import hashlib
import json
import logging
import secrets
import time
import unicodedata
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

_ANONYMOUS_CALLER = "anonymous"
_TOKEN_MISMATCH = (
    "The confirmation token has expired or does not match these arguments. Nothing was done."
)
_PREVIEW_FAILED = "The confirmation preview could not be prepared, so nothing was done."
_NO_CALLER = "This action needs an authenticated MCP session, so nothing was done."
_SECRET_FIELDS = frozenset({"password"})
_EXCERPTED_FIELDS = frozenset({"message_body.text"})
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


class ConfirmationInputError(ValueError):
    pass


PreviewBuilder = Callable[[Mapping[str, Any], httpx.AsyncClient], Awaitable[str]]
Details = Callable[[Mapping[str, Any], httpx.AsyncClient], Awaitable[list[str]]]


@dataclass(frozen=True)
class ConfirmedTool:
    title: str
    required: tuple[str, ...] = ()


def _invalid(name: str) -> ConfirmationInputError:
    return ConfirmationInputError(f"Missing or invalid required argument: {name}")


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


async def _fetch(api: httpx.AsyncClient, path: str, **params: Any) -> Any:
    try:
        response = await api.get(path, params=params or None)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        logger.info("Confirmation preview lookup %s failed", path, exc_info=True)
        return None


async def _entity(api: httpx.AsyncClient, path: str) -> Mapping[str, Any]:
    found = await _fetch(api, path)
    return found if isinstance(found, Mapping) else {}


async def _items(api: httpx.AsyncClient, path: str) -> list[Mapping[str, Any]]:
    found = await _fetch(api, path)
    if isinstance(found, Mapping):
        found = found.get("items")
    return [item for item in found or [] if isinstance(item, Mapping)]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _visible(text: str) -> str:
    return "".join(
        char.encode("unicode_escape").decode("ascii")
        if unicodedata.category(char) in _INVISIBLE_CATEGORIES
        else char
        for char in text
    )


def _bullets(lines: Iterable[str]) -> str:
    return "\n".join(f"• {_visible(line)}" for line in lines)


def _joined(*parts: Any) -> str:
    return ", ".join(str(part) for part in parts if part not in (None, ""))


def _place(entity: Mapping[str, Any]) -> str:
    return _joined(entity.get("city"), entity.get("country"))


def _number_line(entity: Mapping[str, Any] | None, fallback: str) -> str:
    if not isinstance(entity, Mapping) or not entity.get("number"):
        return fallback
    place = _place(entity)
    return f"{entity['number']} ({place})" if place else str(entity["number"])


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
        raise ConfirmationInputError("Exactly one of ids or dids is required")
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
        if not value:
            return "[]"
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
    subject = _visible(subject)
    if not lines:
        return f"This will change {subject}."
    return f"This will change {subject}:\n{_bullets(lines)}"


async def _update_trunk(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> str:
    trunk_id = _integer(arguments, "id")
    current = await _fetch(api, f"/v1/trunks/{trunk_id}")
    label = current.get("label") if isinstance(current, Mapping) else None
    lines = _changes(arguments, current, {"id"}, {"host_request.host": "host"})
    return _change_preview(f"SIP trunk {label or trunk_id}", lines)


async def _update_sub_account(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> str:
    account_id = _integer(arguments, "id")
    current = await _fetch(api, f"/v1/sub-organizations/{account_id}")
    name = current.get("name") if isinstance(current, Mapping) else None
    lines = _changes(arguments, current, {"id"}, {})
    return _change_preview(f"sub-account {name or account_id}", lines)


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


def _excerpt(text: str) -> str:
    if len(text) <= MESSAGE_PREVIEW_CHARS:
        return text
    return text[:MESSAGE_PREVIEW_CHARS].rstrip() + "…"


def _argument_lines(arguments: Mapping[str, Any]) -> list[str]:
    lines = []
    for field, value in _flatten(arguments).items():
        if value is None or field == TOKEN_PARAMETER:
            continue
        if field in _SECRET_FIELDS:
            lines.append(f"{field}: (hidden)")
        elif field in _EXCERPTED_FIELDS and isinstance(value, str):
            lines.append(f"{field}: {_excerpt(value)}")
        else:
            lines.append(f"{field}: {_display(value)}")
    return lines


def template_preview(tool: ConfirmedTool, details: Details | None = None) -> PreviewBuilder:
    async def build(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> str:
        for name in tool.required:
            value = arguments.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise _invalid(name)
        lines = _argument_lines(arguments)
        if details is not None:
            lines += await details(arguments, api)
        heading = f'This will run "{tool.title}"'
        return f"{heading}:\n{_bullets(lines)}" if lines else f"{heading}."

    return build


def _path_id(arguments: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str | int) and not isinstance(value, bool) and str(value).strip():
            return str(value)
    return None


async def _call_details(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> list[str]:
    call_id = _path_id(arguments, "call_id", "id")
    if call_id is None:
        return []
    call = (await _entity(api, f"/v1/calls/{call_id}")).get("call")
    if not isinstance(call, Mapping) or not call.get("from") or not call.get("to"):
        return []
    return [f"call: {call['from']} → {call['to']}"]


async def _recording_details(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> list[str]:
    recording_id = _path_id(arguments, "id")
    if recording_id is None:
        return []
    recording = await _entity(api, f"/v1/recordings/{recording_id}")
    if not recording.get("from") or not recording.get("to"):
        return []
    parties = f"{recording['from']} → {recording['to']}"
    date = str(recording.get("created_at") or "")[:10]
    duration = recording.get("duration")
    length = f"{duration} s" if isinstance(duration, int) else None
    return [f"recording: {_joined(parties, date, length)}"]


async def _trunk_details(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> list[str]:
    trunk_id = _path_id(arguments, "id")
    if trunk_id is None:
        return []
    trunk = await _entity(api, f"/v1/trunks/{trunk_id}")
    caller_id = f"Caller ID {trunk['callerid']}" if trunk.get("callerid") else None
    summary = _joined(trunk.get("label"), caller_id)
    return [f"SIP trunk: {summary}"] if summary else []


async def _brand_details(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> list[str]:
    brand_id = _path_id(arguments, "brand_id")
    if brand_id is None:
        return []
    brand = await _entity(api, f"/v3/10dlc/brands/{brand_id}")
    name = brand.get("dba_name") or brand.get("company_name")
    return [f"brand: {name}"] if name else []


async def _campaign(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> Mapping[str, Any]:
    brand_id = _path_id(arguments, "brand_id")
    campaign_id = _path_id(arguments, "campaign_id")
    if brand_id is None or campaign_id is None:
        return {}
    return await _entity(api, f"/v3/10dlc/brands/{brand_id}/campaigns/{campaign_id}")


async def _campaign_details(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> list[str]:
    campaign = await _campaign(arguments, api)
    return [f"campaign: {campaign['name']}"] if campaign.get("name") else []


async def _campaign_delete_details(
    arguments: Mapping[str, Any], api: httpx.AsyncClient
) -> list[str]:
    campaign = await _campaign(arguments, api)
    lines = [f"campaign: {campaign['name']}"] if campaign.get("name") else []
    numbers = [n for n in campaign.get("phone_numbers") or [] if isinstance(n, str) and n]
    if numbers:
        stopping = ", ".join(_capped(numbers, len(numbers)))
        lines.append(f"numbers that stop sending: {stopping}")
    return lines


async def _sender_id_details(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> list[str]:
    sender_id = _path_id(arguments, "id")
    if sender_id is None:
        return []
    sender = await _entity(api, f"/v3/messages/sender-ids/{sender_id}")
    if not sender.get("sender_id"):
        return []
    kind = f" ({sender['type']})" if sender.get("type") else ""
    return [f"Sender ID: {sender['sender_id']}{kind}"]


async def _widget_token_details(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> list[str]:
    token_id = _path_id(arguments, "id")
    if token_id is None:
        return []
    token = await _entity(api, f"/v2/webrtc/tokens/{token_id}")
    return [f"SIP trunk: {token['sip_trunk']}"] if token.get("sip_trunk") else []


async def _api_key_details(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> list[str]:
    key_id = _path_id(arguments, "id")
    keys = await _items(api, "/v1/api-keys")
    label = next((k.get("label") for k in keys if str(k.get("id")) == key_id), None)
    return [f"API key: {label}"] if label else []


def _current_url(items: list[Mapping[str, Any]], key: str, value: Any) -> list[str]:
    url = next((item.get("url") for item in items if item.get(key) == value), None)
    return [f"current URL: {url}"] if url else []


async def _webhook_details(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> list[str]:
    webhooks = await _items(api, "/v1/calls/webhooks")
    return _current_url(webhooks, "event_type", arguments.get("event_type"))


async def _subscription_details(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> list[str]:
    subscriptions = await _items(api, "/v3/10dlc/subscriptions")
    category = arguments.get("subscription_category")
    return _current_url(subscriptions, "subscription_category", category)


async def _evidence_details(arguments: Mapping[str, Any], api: httpx.AsyncClient) -> list[str]:
    brand_id = _path_id(arguments, "brand_id")
    evidence_id = _path_id(arguments, "id")
    if brand_id is None:
        return []
    files = await _items(api, f"/v3/10dlc/brands/{brand_id}/evidence")
    name = next((f.get("file_name") for f in files if f.get("uuid") == evidence_id), None)
    return [f"file: {name}"] if name else []


CUSTOM_PREVIEWS: Mapping[str, PreviewBuilder] = {
    "my_numbers_delete": _release_numbers,
    "sip_trunks_update": _update_trunk,
    "sub_accounts_update": _update_sub_account,
    "cart_checkout": _checkout,
}

DETAILS: Mapping[str, Details] = {
    "api_keys_delete": _api_key_details,
    "call_control_answer": _call_details,
    "call_control_audio_play": _call_details,
    "call_control_audio_stop": _call_details,
    "call_control_delete": _call_details,
    "call_control_streams_create": _call_details,
    "call_control_streams_delete": _call_details,
    "call_recording_delete": _recording_details,
    "call_webhooks_create": _webhook_details,
    "call_webhooks_delete": _webhook_details,
    "sip_trunks_delete": _trunk_details,
    "sms_and_mms_sender_ids_delete": _sender_id_details,
    "ten_dlc_brand_campaigns_delete": _campaign_delete_details,
    "ten_dlc_brand_evidence_delete": _evidence_details,
    "ten_dlc_brands_delete": _brand_details,
    "ten_dlc_campaign_numbers_unlink": _campaign_details,
    "ten_dlc_subscriptions_delete": _subscription_details,
    "webrtc_tokens_delete": _widget_token_details,
}

ALWAYS_CONFIRMED = frozenset(
    {"call_webhooks_create", "sip_trunks_update", "sub_accounts_update", *CUSTOM_PREVIEWS}
)


def preview_builders(tools: Mapping[str, ConfirmedTool]) -> dict[str, PreviewBuilder]:
    return {
        name: CUSTOM_PREVIEWS.get(name) or template_preview(tool, DETAILS.get(name))
        for name, tool in tools.items()
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
        if token is not None:
            preview = f"{_TOKEN_MISMATCH}\n\n{preview}"

        issued = secrets.token_urlsafe(24)
        tokens = self._pending.setdefault(caller, {})
        if len(tokens) >= MAX_PENDING_PER_CALLER:
            del tokens[next(iter(tokens))]
        tokens[issued] = _Pending(binding, self._clock() + TOKEN_TTL_S)
        return ToolResult(
            content=(
                f"{preview}\nThis preview is meant for the user. The action runs when this tool "
                f'is called again with {TOKEN_PARAMETER}="{issued}"; that call is meant to follow '
                "the user's approval in a later message, and approval given before this preview "
                "is not approval of it."
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
