"""Select which toolsets a connection offers from the ``toolsets`` query parameter."""

from collections.abc import Sequence

from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.resources import Resource
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData

from .docs import EVERY_TOOLSET_URIS, doc_toolset
from .instructions import build_instructions

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

    def _resource_selected(self, uri: str, selection: frozenset[str]) -> bool:
        if uri in EVERY_TOOLSET_URIS:
            return selection >= self._valid
        toolset = doc_toolset(uri)
        return toolset is None or toolset in selection

    async def on_list_resources(self, context: MiddlewareContext, call_next) -> Sequence[Resource]:
        resources = await call_next(context)
        selection = self._selection()
        if selection is None:
            return resources
        return [r for r in resources if self._resource_selected(str(r.uri), selection)]

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        selection = self._selection()
        uri = str(context.message.uri)
        if selection is not None and not self._resource_selected(uri, selection):
            raise ResourceError(f"Resource '{uri}' is not available on this connection's toolsets.")
        return await call_next(context)

    async def on_initialize(self, context: MiddlewareContext, call_next):
        selection = self._selection()
        if context.fastmcp_context is not None:
            session = context.fastmcp_context.session
            session._init_options = session._init_options.model_copy(
                update={"instructions": build_instructions(selection)}
            )
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
