import logging
import os

from fastmcp import FastMCP
from posthog import Posthog
from posthog.mcp import McpAnalytics, MCPAnalyticsOptions
from posthog.mcp import instrument as instrument_fastmcp
from posthog.mcp.types import PostHogCaptureEvent

logger = logging.getLogger(__name__)

DEFAULT_POSTHOG_URL = "https://us.i.posthog.com"


def _before_send(event: PostHogCaptureEvent) -> PostHogCaptureEvent:
    properties = event.get("properties")
    if properties is None:
        return event
    properties.pop("$mcp_parameters", None)
    properties.pop("$mcp_response", None)
    properties.pop("$mcp_error_message", None)
    return event


def instrument(mcp: FastMCP, *, base_url: str) -> McpAnalytics | None:
    api_key = os.getenv("POSTHOG_API_KEY", "").strip()
    if not api_key:
        logger.warning("PostHog analytics disabled: POSTHOG_API_KEY is not set.")
        return None

    posthog_client = Posthog(
        api_key, host=os.getenv("POSTHOG_URL", "").strip() or DEFAULT_POSTHOG_URL
    )
    return instrument_fastmcp(
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
        ),
    )
