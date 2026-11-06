import asyncio
import json
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult

from wavix_mcp.secret_masking import SecretMaskingMiddleware, mask_secret

FULL_KEY = "abc123def456ghi789jkl012mno345pqr678stu901vwx234yz0123456789WXYZ"
OTHER_KEY = "zzz123def456ghi789jkl012mno345pqr678stu901vwx234yz0123456789ABCD"


def _call(tool_name: str, payload: dict) -> ToolResult:
    async def call_next(_ctx):
        return ToolResult(structured_content=payload)

    ctx = MiddlewareContext(message=SimpleNamespace(name=tool_name))
    return asyncio.run(SecretMaskingMiddleware().on_call_tool(ctx, call_next))


def _everything(result: ToolResult) -> str:
    return json.dumps(result.structured_content) + "".join(b.text for b in result.content)


def test_mask_secret_keeps_last_four():
    assert mask_secret(FULL_KEY) == "****WXYZ"


def test_mask_secret_short_value_is_not_revealed():
    assert mask_secret("abcdefgh") == "****"


@pytest.mark.parametrize("tool", ["sub_accounts_list", "sub_accounts_get", "sub_accounts_create"])
def test_sub_account_keys_are_masked_in_every_representation(tool):
    result = _call(
        tool,
        {
            "sub_organizations": [
                {"id": 1, "name": "A", "status": "enabled", "api_key": FULL_KEY},
                {"id": 2, "name": "B", "status": "disabled", "api_key": OTHER_KEY},
            ]
        },
    )

    text = _everything(result)
    assert FULL_KEY not in text
    assert OTHER_KEY not in text
    assert result.structured_content["sub_organizations"][0]["api_key"] == "****WXYZ"
    assert result.structured_content["sub_organizations"][1]["api_key"] == "****ABCD"


def test_sub_account_masking_keeps_the_rest_of_the_reply():
    result = _call(
        "sub_accounts_list",
        {"sub_organizations": [{"id": 1, "name": "A", "status": "enabled", "api_key": FULL_KEY}]},
    )

    item = result.structured_content["sub_organizations"][0]
    assert (item["id"], item["name"], item["status"]) == (1, "A", "enabled")
    assert json.loads(result.content[0].text)["sub_organizations"][0]["status"] == "enabled"


@pytest.mark.parametrize("tool", ["api_keys_list", "api_keys_create"])
def test_api_key_values_are_masked(tool):
    result = _call(tool, {"api_keys": [{"id": 7, "label": "prod", "value": FULL_KEY}]})

    assert FULL_KEY not in _everything(result)
    assert result.structured_content["api_keys"][0]["value"] == "****WXYZ"


def test_other_tools_are_untouched():
    payload = {"sip_trunks": [{"id": 1, "api_key": FULL_KEY, "value": FULL_KEY}]}

    result = _call("sip_trunks_list", payload)

    assert result.structured_content == payload


def test_masking_applies_through_a_real_server():
    mcp = FastMCP("t")

    @mcp.tool
    def sub_accounts_list() -> dict:
        return {"sub_organizations": [{"id": 1, "api_key": FULL_KEY}]}

    mcp.add_middleware(SecretMaskingMiddleware())

    async def run():
        async with Client(mcp) as client:
            return await client.call_tool("sub_accounts_list", {})

    result = asyncio.run(run())

    assert FULL_KEY not in json.dumps(result.structured_content)
    assert FULL_KEY not in "".join(b.text for b in result.content)
    assert result.structured_content["sub_organizations"][0]["api_key"] == "****WXYZ"
