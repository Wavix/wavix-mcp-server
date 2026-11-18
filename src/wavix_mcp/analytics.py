import asyncio
import contextlib
import hashlib
import importlib.metadata
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError
from posthog import Posthog
from posthog.mcp import McpAnalytics, MCPAnalyticsOptions, UserIdentity, get_request_headers
from posthog.mcp import instrument as instrument_fastmcp
from posthog.mcp.types import PostHogCaptureEvent

from .confirmation import NO_CALLER_MESSAGE, PREVIEW_FAILED_MESSAGE
from .scope_filter import ScopeDeniedError, oauth_token

logger = logging.getLogger(__name__)

DEFAULT_POSTHOG_URL = "https://us.i.posthog.com"
PACKAGE_NAME = "wavix-mcp-server"
BUILD_SHA_ENV = "WAVIX_MCP_BUILD_SHA"
SHORT_SHA_LENGTH = 7
IDENTITY_CACHE_TTL_S = 24 * 3600.0
NEGATIVE_IDENTITY_CACHE_TTL_S = 300.0
MAX_CACHED_IDENTITIES = 1000
PROFILE_TIMEOUT_S = 3.0
SHUTDOWN_TIMEOUT_S = 5.0

_STATUS_ERROR_TYPES = {
    400: "invalid_input",
    401: "unauthenticated",
    403: "missing_permission",
    404: "not_found",
    422: "invalid_input",
    429: "rate_limited",
}
# fastmcp's own error texts (openapi/components.py, server.py); pinned by tests.
_UNREACHABLE_PREFIXES = (
    "HTTP request timed out",
    "Request error (",
    "Upstream request timed out",
)
_RESPONSE_MISMATCH_PREFIX = "Output validation error:"
_REFUSED_RESULT_CLASS = "Error"
_VALIDATION_ERROR_CLASS = "ValidationError"
_TOOL_CALL_EVENT = "$mcp_tool_call"


def _subclass_names(base: type[BaseException]) -> frozenset[str]:
    names = {base.__name__}
    subclass: type[BaseException]
    for subclass in base.__subclasses__():
        names |= _subclass_names(subclass)
    return frozenset(names)


_TRANSPORT_ERROR_CLASSES = _subclass_names(httpx.TransportError)


def _error_type(error_class: Any, status: Any, message: str) -> str:
    if isinstance(status, int):
        if status in _STATUS_ERROR_TYPES:
            return _STATUS_ERROR_TYPES[status]
        return "wavix_api_error" if status >= 500 else "wavix_api_rejected"
    if error_class == _VALIDATION_ERROR_CLASS:
        return "invalid_input"
    if error_class == ScopeDeniedError.__name__:
        return "missing_permission"
    if error_class == NotFoundError.__name__:
        return "not_found"
    if error_class in _TRANSPORT_ERROR_CLASSES or message.startswith(_UNREACHABLE_PREFIXES):
        return "wavix_api_unreachable"
    if message.startswith(_RESPONSE_MISMATCH_PREFIX):
        return "invalid_response"
    if message == NO_CALLER_MESSAGE:
        return "unauthenticated"
    if message == PREVIEW_FAILED_MESSAGE:
        return "internal_error"
    # The only other error result is ConfirmationMiddleware refusing a ConfirmationInputError.
    if error_class == _REFUSED_RESULT_CLASS:
        return "invalid_input"
    return "internal_error"


def _before_send(event: PostHogCaptureEvent) -> PostHogCaptureEvent:
    properties = event.get("properties")
    if properties is None:
        return event
    if event.get("event") == _TOOL_CALL_EVENT and properties.get("$mcp_is_error"):
        error_class = properties.get("$mcp_error_type")
        properties["$mcp_error_class"] = error_class
        properties["$mcp_error_type"] = _error_type(
            error_class,
            properties.get("$mcp_error_status"),
            properties.get("$mcp_error_message") or "",
        )
    properties.pop("$mcp_parameters", None)
    properties.pop("$mcp_response", None)
    properties.pop("$mcp_error_message", None)
    return event


def _server_build() -> str | None:
    try:
        version = importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None
    sha = os.getenv(BUILD_SHA_ENV, "").strip()[:SHORT_SHA_LENGTH]
    return f"{version}+{sha}" if sha else version


def _oauth_subject() -> str | None:
    token = oauth_token()
    if token is None:
        return None
    subject = (token.claims or {}).get("sub")
    return subject if isinstance(subject, str) and subject else None


@dataclass
class _CachedIdentity:
    identity: UserIdentity | None
    expires_at: float


class _IdentityResolver:
    def __init__(
        self, client: httpx.AsyncClient, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._client = client
        self._clock = clock
        self._cache: dict[str, _CachedIdentity] = {}
        self._pending: dict[str, asyncio.Task[None]] = {}

    async def __call__(self, request: dict[str, Any], extra: Any) -> UserIdentity | None:
        subject = _oauth_subject()
        if subject is not None:
            return UserIdentity(distinct_id=subject)
        headers = get_request_headers(extra) or {}
        authorization = headers.get("authorization")
        if not authorization:
            return None
        key = hashlib.sha256(authorization.encode()).hexdigest()
        self._sweep(self._clock())
        cached = self._cache.get(key)
        if cached is not None:
            return cached.identity
        if key not in self._pending:
            task = asyncio.create_task(self._resolve_and_cache(key, authorization))
            self._pending[key] = task
            task.add_done_callback(lambda _task: self._pending.pop(key, None))
        return None

    async def aclose(self) -> None:
        pending = list(self._pending.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _resolve_and_cache(self, key: str, authorization: str) -> None:
        try:
            identity, ttl = await self._resolve(authorization)
        except Exception:
            logger.exception("PostHog account lookup failed; the account stays unlinked for now.")
            identity, ttl = None, NEGATIVE_IDENTITY_CACHE_TTL_S
        if len(self._cache) >= MAX_CACHED_IDENTITIES:
            del self._cache[next(iter(self._cache))]
        self._cache[key] = _CachedIdentity(identity, self._clock() + ttl)

    async def _resolve(self, authorization: str) -> tuple[UserIdentity | None, float]:
        try:
            # httpx's bare `timeout=` bounds connect/read/write/pool each
            # separately, not the call as a whole; wait_for() is the actual
            # total budget.
            response = await asyncio.wait_for(
                self._client.get("/v1/profile", headers={"Authorization": authorization}),
                timeout=PROFILE_TIMEOUT_S,
            )
            if not (200 <= response.status_code < 300):
                return None, NEGATIVE_IDENTITY_CACHE_TTL_S
            identity = UserIdentity(distinct_id=str(response.json()["id"]))
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            AttributeError,
            TypeError,
            asyncio.TimeoutError,
        ):
            return None, NEGATIVE_IDENTITY_CACHE_TTL_S
        return identity, IDENTITY_CACHE_TTL_S

    def _sweep(self, now: float) -> None:
        expired = [key for key, cached in self._cache.items() if cached.expires_at <= now]
        for key in expired:
            del self._cache[key]


@dataclass
class _AnalyticsHandle:
    analytics: McpAnalytics
    posthog_client: Posthog | None = None
    profile_client: httpx.AsyncClient | None = None
    resolver: _IdentityResolver | None = None

    async def flush(self) -> None:
        await self.analytics.flush()

    async def aclose(self) -> None:
        if self.resolver is not None:
            await self.resolver.aclose()
        if self.profile_client is not None:
            await self.profile_client.aclose()
        if self.posthog_client is not None:
            # posthog/mcp's documented contract: flush() before shutdown(),
            # or a trailing capture is dropped.
            await self.flush()
            # A plain `asyncio.to_thread` runs on the loop's default
            # executor, whose non-daemon workers block interpreter exit
            # regardless of any wait_for() here — a hung shutdown() would
            # still stall process exit into a SIGKILL. A daemon thread lets
            # us bound *our* wait without that risk.
            loop = asyncio.get_running_loop()
            done = loop.create_future()
            posthog_client = self.posthog_client

            def _shutdown() -> None:
                try:
                    posthog_client.shutdown()
                finally:
                    # A shutdown() slower than SHUTDOWN_TIMEOUT_S finishes
                    # after wait_for() below has already given up and the
                    # loop may be closed by then (process exit, or another
                    # asyncio.run() in tests) — nothing is waiting on `done`
                    # at that point either way.
                    with contextlib.suppress(RuntimeError):
                        loop.call_soon_threadsafe(done.set_result, None)

            threading.Thread(target=_shutdown, daemon=True).start()
            try:
                await asyncio.wait_for(done, timeout=SHUTDOWN_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning(
                    "PostHog shutdown did not drain within %ss; exiting without a full flush.",
                    SHUTDOWN_TIMEOUT_S,
                )


def instrument(mcp: FastMCP, *, base_url: str) -> _AnalyticsHandle | None:
    api_key = os.getenv("POSTHOG_API_KEY", "").strip()
    if not api_key:
        logger.warning("PostHog analytics disabled: POSTHOG_API_KEY is not set.")
        return None

    posthog_client: Posthog | None = None
    try:
        posthog_client = Posthog(
            api_key, host=os.getenv("POSTHOG_URL", "").strip() or DEFAULT_POSTHOG_URL
        )
        # No redirect following: unlike api_client, this client sends the
        # caller's own Authorization header, so a redirect target must never
        # see it (api_client gets the same guarantee from MCPHeaderAuth's
        # allowed_host check instead).
        profile_client = httpx.AsyncClient(
            base_url=base_url, timeout=PROFILE_TIMEOUT_S, follow_redirects=False
        )
        resolver = _IdentityResolver(profile_client)
        analytics = instrument_fastmcp(
            mcp,
            posthog_client,
            MCPAnalyticsOptions(
                context=False,
                capture_model=False,
                enable_conversation_id=False,
                collect_feedback=False,
                enable_exception_autocapture=False,
                logger=logger.info,
                server_build=_server_build(),
                before_send=_before_send,
                identify=resolver,
            ),
        )
    except Exception:
        logger.exception("PostHog analytics disabled: failed to initialize instrumentation.")
        if posthog_client is not None:
            posthog_client.shutdown()
        return None
    return _AnalyticsHandle(analytics, posthog_client, profile_client, resolver)
