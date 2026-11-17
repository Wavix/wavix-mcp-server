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
    identity: UserIdentity
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
        try:
            response = await self._client.get(
                "/v1/profile", headers={"Authorization": authorization}
            )
        except httpx.HTTPError:
            return None
        if not (200 <= response.status_code < 300):
            return None
        profile = response.json()
        identity = UserIdentity(
            distinct_id=str(profile["id"]), properties={"email": profile.get("email")}
        )
        if len(self._cache) >= MAX_CACHED_IDENTITIES:
            del self._cache[next(iter(self._cache))]
        self._cache[key] = _CachedIdentity(identity, now + IDENTITY_CACHE_TTL_S)
        return identity

    def _sweep(self, now: float) -> None:
        while self._cache:
            oldest = next(iter(self._cache))
            if self._cache[oldest].expires_at > now:
                break
            del self._cache[oldest]


@dataclass
class _AnalyticsHandle:
    analytics: McpAnalytics
    profile_client: httpx.AsyncClient | None = None

    async def flush(self) -> None:
        await self.analytics.flush()

    async def aclose(self) -> None:
        if self.profile_client is not None:
            await self.profile_client.aclose()


def instrument(mcp: FastMCP, *, base_url: str) -> _AnalyticsHandle | None:
    api_key = os.getenv("POSTHOG_API_KEY", "").strip()
    if not api_key:
        logger.warning("PostHog analytics disabled: POSTHOG_API_KEY is not set.")
        return None

    posthog_client = Posthog(
        api_key, host=os.getenv("POSTHOG_URL", "").strip() or DEFAULT_POSTHOG_URL
    )
    profile_client = httpx.AsyncClient(base_url=base_url, timeout=PROFILE_TIMEOUT_S)
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
            send_initialize_event=False,
            send_tools_list_event=False,
            logger=logger.info,
            before_send=_before_send,
            identify=resolver,
        ),
    )
    return _AnalyticsHandle(analytics, profile_client)
