import asyncio
import hashlib
import logging
import os
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
        identity, ttl = await self._resolve(authorization)
        if len(self._cache) >= MAX_CACHED_IDENTITIES:
            # Oldest-inserted, not oldest-expiring: a coarse FIFO cap, not
            # true LRU/TTL eviction. Good enough as a hard size bound.
            del self._cache[next(iter(self._cache))]
        self._cache[key] = _CachedIdentity(identity, now + ttl)
        return identity

    async def _resolve(self, authorization: str) -> tuple[UserIdentity | None, float]:
        # A 403 (OAuth connection scoped narrower than account:read) is a
        # permanent-until-reconnect state, not a transient failure — cache it
        # too, with a shorter TTL, or every tool call from that caller pays a
        # GET /v1/profile round trip forever.
        try:
            response = await self._client.get(
                "/v1/profile", headers={"Authorization": authorization}
            )
            if not (200 <= response.status_code < 300):
                return None, NEGATIVE_IDENTITY_CACHE_TTL_S
            profile = response.json()
            # Omit rather than send None: PostHog $set merges every property
            # named here into the person, and a None would overwrite the
            # email other Wavix surfaces already set for the same person.
            email = profile.get("email")
            identity = UserIdentity(
                distinct_id=str(profile["id"]), properties={"email": email} if email else {}
            )
        except (httpx.HTTPError, ValueError, KeyError):
            return None, NEGATIVE_IDENTITY_CACHE_TTL_S
        return identity, IDENTITY_CACHE_TTL_S

    def _sweep(self, now: float) -> None:
        # Positive and negative-cache entries carry different TTLs, so
        # insertion order is not expiry order: scan every entry rather than
        # stopping at the first unexpired one.
        expired = [key for key, cached in self._cache.items() if cached.expires_at <= now]
        for key in expired:
            del self._cache[key]


@dataclass
class _AnalyticsHandle:
    analytics: McpAnalytics
    posthog_client: Posthog | None = None
    profile_client: httpx.AsyncClient | None = None

    async def flush(self) -> None:
        # No production caller: exists so tests can drain the adapter's
        # fire-and-forget captures deterministically before asserting on them.
        await self.analytics.flush()

    async def aclose(self) -> None:
        if self.profile_client is not None:
            await self.profile_client.aclose()
        if self.posthog_client is not None:
            # instrument()'s own contract: "call shutdown() on exit to
            # flush" — the SDK never owns the client's lifecycle. shutdown()
            # is synchronous and drains the send queue, so run it off the
            # event loop.
            await asyncio.to_thread(self.posthog_client.shutdown)


def instrument(mcp: FastMCP, *, base_url: str) -> _AnalyticsHandle | None:
    api_key = os.getenv("POSTHOG_API_KEY", "").strip()
    if not api_key:
        logger.warning("PostHog analytics disabled: POSTHOG_API_KEY is not set.")
        return None

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
        # Analytics must never be able to take the server down with it —
        # a misconfigured POSTHOG_URL or an incompatible posthog resolve
        # degrades to "no analytics", same as POSTHOG_API_KEY being unset.
        logger.exception("PostHog analytics disabled: failed to initialize instrumentation.")
        return None
    return _AnalyticsHandle(analytics, posthog_client, profile_client)
