"""Hide tools the connecting token's OAuth scopes don't cover, and reject a call
to one that slips through (e.g. from a client's stale tool list).

Only OAuth-authenticated connections are filtered. When no access token is
present — the API-key passthrough deployment, where OAuth is disabled — every
tool is listed and the upstream API stays the sole authority, unchanged.
"""

import logging
from collections.abc import Sequence

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool

from . import scopes

logger = logging.getLogger(__name__)

ToolRoutes = dict[str, tuple[str, str]]


def _granted_scopes(token) -> set[str]:
    granted = set(token.scopes or [])
    claim = (token.claims or {}).get("scope")
    if isinstance(claim, str):
        granted.update(claim.split())
    return granted


class ScopeFilterMiddleware(Middleware):
    def __init__(self, tool_routes: ToolRoutes):
        self._tool_routes = tool_routes

    def _requirement(self, tool_name: str) -> scopes.Requirement:
        route = self._tool_routes.get(tool_name)
        # A tool with no known API route (docs helpers, future additions) is not
        # scope-gated here; it stays visible and the API remains the authority.
        if route is None:
            return scopes.EXEMPT
        return scopes.requirement_for(route[0], route[1])

    async def on_list_tools(self, context: MiddlewareContext, call_next) -> Sequence[Tool]:
        tools = await call_next(context)
        token = get_access_token()
        if token is None:
            return tools
        granted = _granted_scopes(token)
        return [t for t in tools if scopes.is_allowed(granted, self._requirement(t.name))]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        token = get_access_token()
        if token is not None:
            requirement = self._requirement(context.message.name)
            if not scopes.is_allowed(_granted_scopes(token), requirement):
                raise ToolError(_denied_message(context.message.name, requirement))
        return await call_next(context)


def _denied_message(tool_name: str, requirement: scopes.Requirement) -> str:
    if requirement.kind == "needs":
        return (
            f"Tool '{tool_name}' requires the '{requirement.group}:{requirement.level}' "
            "permission, which this connection was not granted. Re-authorize with that "
            "permission to use it."
        )
    return f"Tool '{tool_name}' is not available for this connection's granted permissions."
