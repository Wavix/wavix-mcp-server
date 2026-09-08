"""The reconcile warning fires on genuine scope-table drift, not on the
API-key tools that are intentionally outside the OAuth scope model (DEV-11668)."""

import logging

from wavix_mcp.server import _warn_on_never_gated_tools


def test_api_key_tools_do_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        _warn_on_never_gated_tools({"api_keys_create": ("POST", "/v1/api-keys")})
    assert "Reconcile" not in caplog.text


def test_unknown_unmapped_tool_warns(caplog):
    with caplog.at_level(logging.WARNING):
        _warn_on_never_gated_tools({"widgets_list": ("GET", "/v1/widgets")})
    assert "widgets_list" in caplog.text
    assert "Reconcile" in caplog.text
