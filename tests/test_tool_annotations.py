"""Every tool must carry a title and behaviour hints.

Anthropic's review criteria: "Every tool must include a `title` and the
applicable hint — `readOnlyHint: true` for read-only tools, `destructiveHint:
true` for tools that modify or delete data."
https://claude.com/docs/connectors/building/review-criteria
"""

import asyncio
import os

import pytest

from wavix_mcp.server import _hint, build_server


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
