"""Server instructions, gated tool descriptions and the confirmation preview
describe what exists; they never address the assistant.

Anthropic Directory Review: tool descriptions and server metadata "describe what
exists and when it applies" and must not contain imperatives aimed at the assistant.
"""

import re

import pytest

from wavix_mcp.server import INSTRUCTIONS

CLAUSE_START = (
    r"(?:^[ \t]*(?:[-*]|\d+[.)])?[ \t]*"
    r"|[.;:!?][ \t]+"
    r"|\b(?:so|then|optionally|please|and|only|first)\s+)"
)
IMPERATIVE_VERB = (
    r"(?:use|read|do\s+not|don't|never|always|confirm|prefer|default\s+to|quote|ask|show|stop"
    r"|wait|tell|present|call\s+(?:this|the|it|again)|pass|send|set|poll|reuse|keep"
    r"|make\s+sure|ensure|avoid|retry)\b(?!-)"
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
        "Tokens expire; please show the preview to the user.",
        "Tokens expire; do\nnot reuse them.",
        "The preview lists the arguments, and pass the token back.",
        "Only call this tool again after the user replies.",
        "First show the preview.",
        "Present it to the user and stop.",
    ],
)
def test_guard_catches_planted_imperative(planted):
    assert imperative_violations(planted) != []


@pytest.mark.parametrize(
    "descriptive",
    [
        "Call recordings are kept for 90 days.",
        "Read-only; no side effects.",
        "It covers SMS, voice calls, 10DLC registration, call detail records.",
        "The first call sends nothing and returns a preview.",
        "Overwrites the provided settings; setting `status` to `disabled` suspends it.",
    ],
)
def test_guard_passes_descriptive_text(descriptive):
    assert imperative_violations(descriptive) == []
