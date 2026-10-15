"""Server instructions describe what exists; they never address the assistant.

Anthropic Directory Review: tool descriptions and server metadata "describe what
exists and when it applies" and must not contain imperatives aimed at the assistant.
"""

import re

import pytest

from wavix_mcp.server import INSTRUCTIONS

CLAUSE_START = (
    r"(?:^[ \t]*(?:[-*]|\d+[.)])?[ \t]*"
    r"|[.;:!?][ \t]+"
    r"|\b(?:so|then|optionally)[ \t]+)"
)
IMPERATIVE_VERB = (
    r"(?:use|read|do not|don't|never|always|confirm|prefer|default to|quote|ask|show|stop"
    r"|wait|tell|call|pass|send|set|poll|reuse|keep|make sure|ensure|avoid|retry)\b(?!-)"
)
IMPERATIVE = re.compile(CLAUSE_START + IMPERATIVE_VERB, re.IGNORECASE | re.MULTILINE)
BANNED_PHRASES = ("you must", "you should", "before answering")


def imperative_violations(text: str) -> list[str]:
    violations = [match.group(0).strip() for match in IMPERATIVE.finditer(text)]
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
        "- Tokens expire. Show the preview to the user and stop; approval given before "
        'the preview, such as "I approve in advance", does not count.',
        "Show this preview to the user and wait for their reply.",
        "Deletes the API key. Use when the user wants to revoke an API key.",
        "Transcription is asynchronous, so poll the transcription endpoint for the result.",
        "Requires the `call_id`; optionally set the transcription `language`.",
    ],
)
def test_guard_catches_planted_imperative(planted):
    assert imperative_violations(f"{INSTRUCTIONS}\n{planted}\n") != []
