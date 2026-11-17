import asyncio
import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from fastmcp import FastMCP
from posthog import Posthog
from posthog.mcp import McpAnalytics, MCPAnalyticsOptions, UserIdentity, get_request_headers
from posthog.mcp import instrument as instrument_fastmcp
from posthog.mcp.types import PostHogCaptureEvent

logger = logging.getLogger(__name__)

DEFAULT_POSTHOG_URL = "https://us.i.posthog.com"
IDENTITY_CACHE_TTL_S = 300.0
NEGATIVE_IDENTITY_CACHE_TTL_S = 30.0
MAX_CACHED_IDENTITIES = 1000
PROFILE_TIMEOUT_S = 3.0
SHUTDOWN_TIMEOUT_S = 5.0


def _before_send(event: PostHogCaptureEvent) -> PostHogCaptureEvent:
    properties = event.get("properties")
    if properties is None:
        return event
    properties.pop("$mcp_parameters", None)
    properties.pop("$mcp_response", None)
    properties.pop("$mcp_error_message", None)
    return event


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
        self._locks: dict[str, asyncio.Lock] = {}

    async def __call__(self, request: dict[str, Any], extra: Any) -> UserIdentity | None:
        headers = get_request_headers(extra) or {}
        authorization = headers.get("authorization")
        if not authorization:
            return None
        key = hashlib.sha256(authorization.encode()).hexdigest()
        now = self._clock()
        self._sweep(now)
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > now:
            return cached.identity

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at > now:
                return cached.identity
            identity, ttl = await self._resolve(authorization)
            if len(self._cache) >= MAX_CACHED_IDENTITIES:
                oldest = next(iter(self._cache))
                del self._cache[oldest]
                self._locks.pop(oldest, None)
            self._cache[key] = _CachedIdentity(identity, now + ttl)
        return identity

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
            profile = response.json()
            email = profile.get("email")
            identity = UserIdentity(
                distinct_id=str(profile["id"]), properties={"email": email} if email else {}
            )
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
            self._locks.pop(key, None)
        orphaned = [
            key for key, lock in self._locks.items() if key not in self._cache and not lock.locked()
        ]
        for key in orphaned:
            del self._locks[key]


@dataclass
class _AnalyticsHandle:
    analytics: McpAnalytics
    posthog_client: Posthog | None = None
    profile_client: httpx.AsyncClient | None = None

    async def flush(self) -> None:
        await self.analytics.flush()

    async def aclose(self) -> None:
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
                before_send=_before_send,
                identify=resolver,
            ),
        )
    except Exception:
        logger.exception("PostHog analytics disabled: failed to initialize instrumentation.")
        if posthog_client is not None:
            posthog_client.shutdown()
        return None
    return _AnalyticsHandle(analytics, posthog_client, profile_client)
