"""Select which toolsets a connection offers from the ``toolsets`` query parameter."""

from collections.abc import Sequence

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData

ToolToolset = dict[str, str]

QUERY_PARAMETER = "toolsets"


class InvalidToolsetsError(McpError):
    def __init__(self, message: str):
        super().__init__(ErrorData(code=INVALID_PARAMS, message=message))


def parse_selection(raw: str | None, valid: frozenset[str]) -> frozenset[str] | None:
    if raw is None:
        return None
    names = {name for name in (part.strip() for part in raw.split(",")) if name}
    listing = ", ".join(sorted(valid))
    if not names:
        raise InvalidToolsetsError(f"Empty toolsets value. Valid toolsets: {listing}")
    for name in sorted(names):
        if name not in valid:
            raise InvalidToolsetsError(f"Unknown toolset '{name}'. Valid toolsets: {listing}")
    return frozenset(names)


class ToolsetFilterMiddleware(Middleware):
    def __init__(self, tool_toolsets: ToolToolset, valid: frozenset[str]):
        self._tool_toolsets = tool_toolsets
        self._valid = valid

    def _selection(self) -> frozenset[str] | None:
        try:
            raw = get_http_request().query_params.get(QUERY_PARAMETER)
        except (RuntimeError, LookupError):
            return None
        return parse_selection(raw, self._valid)

    def _selected(self, tool_name: str, selection: frozenset[str]) -> bool:
        return self._tool_toolsets.get(tool_name) in selection

    async def on_initialize(self, context: MiddlewareContext, call_next):
        self._selection()
        return await call_next(context)

    async def on_list_tools(self, context: MiddlewareContext, call_next) -> Sequence[Tool]:
        tools = await call_next(context)
        selection = self._selection()
        if selection is None:
            return tools
        return [t for t in tools if self._selected(t.name, selection)]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        selection = self._selection()
        name = context.message.name
        if selection is not None and not self._selected(name, selection):
            raise ToolError(f"Tool '{name}' is not available on this connection's toolsets.")
        return await call_next(context)
