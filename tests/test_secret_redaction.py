import asyncio
import json
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult

from wavix_mcp.secret_redaction import SecretRedactionMiddleware, strip_field
from wavix_mcp.server import TOOL_DESCRIPTIONS, _strip_secret_fields

FULL_KEY = "abc123def456ghi789jkl012mno345pqr678stu901vwx234yz0123456789WXYZ"


def _call(tool_name: str, payload: dict) -> ToolResult:
    async def call_next(_ctx):
        return ToolResult(structured_content=payload)

    ctx = MiddlewareContext(message=SimpleNamespace(name=tool_name))
    return asyncio.run(SecretRedactionMiddleware().on_call_tool(ctx, call_next))


def _everything(result: ToolResult) -> str:
    return json.dumps(result.structured_content) + "".join(b.text for b in result.content)


def _spec(operation_id: str, schema: dict) -> dict:
    return {
        "paths": {
            "/x": {
                "get": {
                    "operationId": operation_id,
                    "responses": {"200": {"content": {"application/json": {"schema": schema}}}},
                }
            }
        }
    }


def _schema(spec: dict) -> dict:
    return spec["paths"]["/x"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_strip_field_removes_the_key_at_any_depth():
    payload = {"sub_organizations": [{"id": 1, "api_key": FULL_KEY, "redacted_api_key": "...WXYZ"}]}

    result = strip_field(payload, "api_key")

    assert result == {"sub_organizations": [{"id": 1, "redacted_api_key": "...WXYZ"}]}
    assert "api_key" in payload["sub_organizations"][0]


@pytest.mark.parametrize(
    "tool", ["sub_accounts_list", "sub_accounts_get", "sub_accounts_create", "sub_accounts_update"]
)
def test_sub_account_replies_hold_no_full_key(tool):
    result = _call(
        tool,
        {
            "sub_organizations": [
                {"id": 1, "api_key": FULL_KEY, "redacted_api_key": "...WXYZ"},
            ]
        },
    )

    assert FULL_KEY not in _everything(result)
    assert result.structured_content["sub_organizations"][0]["redacted_api_key"] == "...WXYZ"
    assert json.loads(result.content[0].text)["sub_organizations"][0]["redacted_api_key"] == (
        "...WXYZ"
    )


@pytest.mark.parametrize("tool", ["api_keys_list", "api_keys_create", "api_keys_update"])
def test_api_key_replies_hold_no_full_value(tool):
    result = _call(
        tool,
        {"api_keys": [{"id": 7, "label": "prod", "value": FULL_KEY, "redacted_value": "...WXYZ"}]},
    )

    assert FULL_KEY not in _everything(result)
    item = result.structured_content["api_keys"][0]
    assert "value" not in item
    assert item["redacted_value"] == "...WXYZ"


def test_other_tools_are_untouched():
    payload = {"sip_trunks": [{"id": 1, "api_key": FULL_KEY, "value": FULL_KEY}]}

    result = _call("sip_trunks_list", payload)

    assert result.structured_content == payload


def test_stripping_applies_through_a_real_server():
    mcp = FastMCP("t")

    @mcp.tool
    def sub_accounts_list() -> dict:
        return {"sub_organizations": [{"id": 1, "api_key": FULL_KEY}]}

    mcp.add_middleware(SecretRedactionMiddleware())

    async def run():
        async with Client(mcp) as client:
            return await client.call_tool("sub_accounts_list", {})

    result = asyncio.run(run())

    assert FULL_KEY not in json.dumps(result.structured_content)
    assert FULL_KEY not in "".join(b.text for b in result.content)


def _fields(*names: str) -> dict:
    return {
        "type": "object",
        "required": list(names),
        "properties": {name: {"type": "string"} for name in names},
    }


def test_schema_of_sub_account_tool_loses_api_key():
    names = ("id", "api_key", "redacted_api_key")
    spec = _spec("sub_accounts_get", _fields(*names))
    other = _spec("sip_trunks_get", _fields(*names))

    _strip_secret_fields(spec)
    _strip_secret_fields(other)

    assert "api_key" not in _schema(spec)["properties"]
    assert _schema(spec)["required"] == ["id", "redacted_api_key"]
    assert _schema(other) == _fields(*names)


def test_schema_of_api_keys_list_items_loses_value():
    schema = {
        "type": "object",
        "properties": {
            "api_keys": {"type": "array", "items": _fields("id", "value", "redacted_value")}
        },
    }
    spec = _spec("api_keys_list", schema)

    _strip_secret_fields(spec)

    items = _schema(spec)["properties"]["api_keys"]["items"]
    assert "value" not in items["properties"]
    assert items["required"] == ["id", "redacted_value"]


@pytest.mark.parametrize(
    ("tool", "field"),
    [("sub_accounts_create", "redacted_api_key"), ("api_keys_create", "redacted_value")],
)
def test_create_descriptions_name_the_last_four_rule(tool, field):
    text = TOOL_DESCRIPTIONS[tool]

    assert "last 4 characters" in text
    assert field in text
