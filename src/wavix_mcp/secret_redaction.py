import json
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

SECRET_FIELDS_BY_TOOL_PREFIX: dict[str, str] = {
    "sub_accounts_": "api_key",
    "api_keys_": "value",
}


def strip_field(node: Any, field: str) -> Any:
    if isinstance(node, dict):
        return {key: strip_field(item, field) for key, item in node.items() if key != field}
    if isinstance(node, list):
        return [strip_field(item, field) for item in node]
    return node


def secret_field_for_tool(tool_name: str) -> str | None:
    for prefix, field in SECRET_FIELDS_BY_TOOL_PREFIX.items():
        if tool_name.startswith(prefix):
            return field
    return None


def _strip_text_block(block: Any, field: str) -> Any:
    if not isinstance(block, TextContent):
        return block
    try:
        parsed = json.loads(block.text)
    except ValueError:
        return block
    return block.model_copy(update={"text": json.dumps(strip_field(parsed, field))})


class SecretRedactionMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        result = await call_next(context)
        field = secret_field_for_tool(context.message.name)
        if field is None or not isinstance(result, ToolResult):
            return result
        return ToolResult(
            content=[_strip_text_block(block, field) for block in result.content],
            structured_content=strip_field(result.structured_content, field),
            meta=result.meta,
        )
