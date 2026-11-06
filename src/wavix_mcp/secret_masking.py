import json
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

VISIBLE_TAIL = 4
MASK_PREFIX = "****"

SECRET_FIELDS_BY_TOOL_PREFIX: dict[str, str] = {
    "sub_accounts_": "api_key",
    "api_keys_": "value",
}


def mask_secret(value: str) -> str:
    if len(value) <= 2 * VISIBLE_TAIL:
        return MASK_PREFIX
    return f"{MASK_PREFIX}{value[-VISIBLE_TAIL:]}"


def mask_field(node: Any, field: str) -> Any:
    if isinstance(node, dict):
        return {
            key: mask_secret(item)
            if key == field and isinstance(item, str)
            else mask_field(item, field)
            for key, item in node.items()
        }
    if isinstance(node, list):
        return [mask_field(item, field) for item in node]
    return node


def _secret_field(tool_name: str) -> str | None:
    for prefix, field in SECRET_FIELDS_BY_TOOL_PREFIX.items():
        if tool_name.startswith(prefix):
            return field
    return None


def _mask_text_block(block: Any, field: str) -> Any:
    if not isinstance(block, TextContent):
        return block
    try:
        parsed = json.loads(block.text)
    except ValueError:
        return block
    return block.model_copy(update={"text": json.dumps(mask_field(parsed, field))})


class SecretMaskingMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        result = await call_next(context)
        field = _secret_field(context.message.name)
        if field is None or not isinstance(result, ToolResult):
            return result
        return ToolResult(
            content=[_mask_text_block(block, field) for block in result.content],
            structured_content=mask_field(result.structured_content, field),
            meta=result.meta,
        )
