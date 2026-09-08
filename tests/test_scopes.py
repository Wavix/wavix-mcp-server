"""Scope-to-route decision, mirrored from the API gateway (DEV-11668)."""

import pytest

from wavix_mcp import scopes


@pytest.mark.parametrize(
    "method,path,group,level",
    [
        ("POST", "/v1/buy", "numbers", "write"),
        ("GET", "/v1/mydids/{id}", "numbers", "read"),
        ("GET", "/v1/numbers", "numbers", "read"),
        ("POST", "/v1/cdr", "calls", "read"),  # method override
        ("POST", "/v1/cdrs", "calls", "read"),  # method override
        ("GET", "/v1/cdr", "calls", "read"),
        ("POST", "/v1/voice-campaigns", "campaigns", "write"),
        ("GET", "/v3/10dlc/brands/{brand_id}", "campaigns", "read"),
        ("POST", "/v2/messages", "messages", "write"),
        ("GET", "/v1/calls/webhooks/{id}", "webhooks", "read"),  # exclusion beats calls
        ("GET", "/v2/webrtc/tokens", "embeddable", "read"),
    ],
)
def test_requirement_maps_route_to_scope(method, path, group, level):
    req = scopes.requirement_for(method, path)
    assert (req.kind, req.group, req.level) == ("needs", group, level)


def test_unmatched_path_is_never_available_over_oauth():
    assert scopes.requirement_for("GET", "/v1/countries").kind == "never"


def test_api_key_management_is_never_available_over_oauth():
    # A delegated OAuth grant must not mint or manage the account's API keys.
    assert scopes.requirement_for("POST", "/v1/api-keys").kind == "never"
    assert scopes.requirement_for("DELETE", "/v1/api-keys/{id}").kind == "never"


def test_support_portal_paths_are_exempt():
    assert scopes.requirement_for("GET", "/public/v1/support-portal/tickets").kind == "exempt"


def test_write_grant_covers_read_tools():
    read_tool = scopes.requirement_for("GET", "/v1/numbers")
    assert scopes.is_allowed({"numbers:write"}, read_tool)


def test_read_grant_hides_write_tools():
    write_tool = scopes.requirement_for("POST", "/v1/buy")
    assert not scopes.is_allowed({"numbers:read"}, write_tool)
    assert scopes.is_allowed({"numbers:read"}, scopes.requirement_for("GET", "/v1/mydids"))


def test_ungranted_group_hides_all_its_tools():
    assert not scopes.is_allowed({"messages:write"}, scopes.requirement_for("GET", "/v1/numbers"))
    assert not scopes.is_allowed(set(), scopes.requirement_for("POST", "/v1/buy"))


def test_offline_access_and_junk_scopes_grant_nothing():
    assert not scopes.is_allowed(
        {"offline_access", "numbers"}, scopes.requirement_for("GET", "/v1/numbers")
    )


def test_exempt_always_allowed_never_never_allowed():
    assert scopes.is_allowed(set(), scopes.EXEMPT)
    assert not scopes.is_allowed({"numbers:write"}, scopes.NEVER)
