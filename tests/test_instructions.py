"""Server instructions describe what exists; they never address the assistant.

Anthropic Directory Review: tool descriptions and server metadata "describe what
exists and when it applies" and must not contain imperatives aimed at the assistant.
"""

import re

import pytest

from wavix_mcp.server import INSTRUCTIONS

IMPERATIVE_LINE_START = re.compile(
    r"^(use|read|do not|don't|never|always|confirm|prefer|default to|quote|ask)\b",
    re.IGNORECASE,
)
BANNED_PHRASES = ("you must", "you should", "before answering")


def imperative_violations(text: str) -> list[str]:
    violations = []
    for line in text.splitlines():
        body = line.lstrip(" -*0123456789.)")
        if IMPERATIVE_LINE_START.match(body):
            violations.append(line)
    lowered = text.lower()
    violations.extend(phrase for phrase in BANNED_PHRASES if phrase in lowered)
    return violations


def test_instructions_carry_no_assistant_imperatives():
    assert imperative_violations(INSTRUCTIONS) == []


@pytest.mark.parametrize(
    "planted",
    [
        "- Use *_list tools for paginated retrieval.",
        "  1. Do not ask the user for credentials.",
        "Default to small page sizes.",
        "Before answering, read wavix://docs/* first.",
        "You should confirm destructive actions.",
    ],
)
def test_guard_catches_planted_imperative(planted):
    assert imperative_violations(f"{INSTRUCTIONS}\n{planted}\n") != []
