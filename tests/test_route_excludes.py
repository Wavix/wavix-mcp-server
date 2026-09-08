"""Tests for the tool-surface exclude route maps and the spec-drift guard."""

import httpx
import pytest
from fastmcp.server.providers.openapi import MCPType

from wavix_mcp.server import (
    BINARY_STREAM_ENDPOINTS,
    EXCLUDED_ENDPOINTS,
    _build_exclude_route_maps,
    _missing_targets,
    load_spec,
)

_SPEC = {
    "paths": {
        "/v1/cdrs/all": {"get": {}},
        "/v1/recordings/{call_id}": {"get": {}},
    }
}


def test_missing_targets_flags_absent_path_and_method():
    targets = (
        ("/v1/cdrs/all", "get"),
        ("/v1/recordings/{call_id}", "post"),
        ("/v1/nope", "get"),
    )

    assert _missing_targets(_SPEC, targets) == [
        ("/v1/recordings/{call_id}", "post"),
        ("/v1/nope", "get"),
    ]


def test_missing_targets_empty_when_all_present():
    assert _missing_targets(_SPEC, (("/v1/cdrs/all", "get"),)) == []


def test_exclude_route_maps_are_anchored_and_excluding():
    maps = _build_exclude_route_maps((("/v1/recordings/{call_id}", "get"),))

    assert len(maps) == 1
    assert maps[0].mcp_type is MCPType.EXCLUDE
    assert maps[0].pattern == r"^/v1/recordings/{call_id}$"
    assert maps[0].methods == ["GET"]


def test_configured_excludes_exist_in_live_spec():
    try:
        spec = load_spec()
    except httpx.HTTPError as exc:
        pytest.skip(f"live OpenAPI spec unavailable: {exc}")

    missing = _missing_targets(spec, BINARY_STREAM_ENDPOINTS + EXCLUDED_ENDPOINTS)

    assert missing == [], f"stale exclude targets: {missing}"
