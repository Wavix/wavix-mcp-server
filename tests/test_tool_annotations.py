"""Every tool must carry a title and behaviour hints.

Anthropic's review criteria: "Every tool must include a `title` and the
applicable hint — `readOnlyHint: true` for read-only tools, `destructiveHint:
true` for tools that modify or delete data."
https://claude.com/docs/connectors/building/review-criteria
"""

import asyncio
import os

import pytest

from wavix_mcp.server import (
    BINARY_STREAM_ENDPOINTS,
    EXCLUDED_ENDPOINTS,
    _hint,
    build_server,
    load_spec,
)


@pytest.fixture(scope="module")
def tools():
    os.environ.setdefault("CLUSTER", "prod")
    return asyncio.run(build_server().list_tools())


def test_no_tool_ships_without_a_title(tools):
    assert [t.name for t in tools if not t.title] == []


def test_no_tool_name_exceeds_64_chars(tools):
    # Anthropic's review criteria: "Tool names must be 64 characters or fewer."
    assert [t.name for t in tools if len(t.name) > 64] == []


def test_no_tool_ships_without_hints(tools):
    # The published spec carries no x-mcp today, so this is what proves the
    # method-derived fallback is doing its job rather than leaving hints null.
    missing = [
        t.name
        for t in tools
        if t.annotations is None
        or t.annotations.readOnlyHint is None
        or t.annotations.destructiveHint is None
    ]
    assert missing == []


def test_hints_are_not_a_blanket_constant(tools):
    # A constant would satisfy the shape while lying about behaviour.
    assert {bool(t.annotations.readOnlyHint) for t in tools} == {True, False}
    assert {bool(t.annotations.destructiveHint) for t in tools} == {True, False}


def test_the_fallback_treats_any_write_as_destructive():
    # A method cannot distinguish a benign create from one that spends money,
    # so when the spec is silent the fallback over-warns rather than under-warns.
    assert _hint(None, True) is True


def test_an_explicit_spec_value_wins_over_the_derived_one():
    assert _hint(True, False) is True
    assert _hint(False, True) is False


def test_the_derived_value_is_used_when_the_spec_is_silent():
    assert _hint(None, True) is True
    assert _hint(None, False) is False


def test_no_two_tools_share_a_title(tools):
    titles = [t.title for t in tools]
    assert sorted({title for title in titles if titles.count(title) > 1}) == []


def test_the_spec_declares_every_hint_of_every_exposed_operation():
    excluded = set(BINARY_STREAM_ENDPOINTS + EXCLUDED_ENDPOINTS)
    undeclared = []
    for path, item in load_spec()["paths"].items():
        for method, operation in item.items():
            if not isinstance(operation, dict) or (path, method) in excluded:
                continue
            xmcp = operation.get("x-mcp")
            if not isinstance(xmcp, dict):
                xmcp = {}
            if xmcp.get("expose") is False:
                continue
            missing = [
                key
                for key in ("readOnly", "destructive", "openWorld")
                if not isinstance(xmcp.get(key), bool)
            ]
            if missing:
                undeclared.append(f"{operation.get('operationId')}: {', '.join(missing)}")
    assert undeclared == []
