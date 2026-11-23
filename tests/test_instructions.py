"""Server instructions, tool descriptions and the confirmation preview describe
what exists; they never address the assistant.

Anthropic Directory Review: tool descriptions and server metadata "describe what
exists and when it applies" and must not contain imperatives aimed at the assistant.
"""

import asyncio
import re

import pytest

from wavix_mcp import instructions, server
from wavix_mcp.instructions import build_instructions
from wavix_mcp.server import INSTRUCTIONS

from . import test_toolsets

upstream = test_toolsets.upstream

CLAUSE_START = (
    r"(?:(?:^[ \t]*(?:[-*]|\d+[.)])?[ \t]*|[.;:!?][ \t]+)(?:(?:first|then|please|only|optionally)\s+)?"
    r"|,\s+(?:(?:and|so|then)\s+)?"
    r"|\b(?:so|then|optionally|please)\s+)"
)
CALL_NOUNS = r"recordings?|detail|records?|logs?|metadata|flows?|history|status|ids?"
IMPERATIVE_VERB = (
    r"(?:use(?!\s+cases?\b)|read|do\s+not|don't|never|always|confirm|prefer|default\s+to|quote|ask|show|stop"
    rf"|wait|tell|present|(?-i:[Gg]et)|fetch|call(?!\s+(?:{CALL_NOUNS})\b)|pass|send|set|poll|reuse|keep"
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


def test_instructions_name_the_gated_webhook_registrations():
    assert "register call webhooks, register or change WhatsApp webhooks" in INSTRUCTIONS
    assert "submit WhatsApp templates" in INSTRUCTIONS


def test_instructions_name_the_paid_tools_among_the_two_step_tools():
    gated = INSTRUCTIONS.split("runs in two steps", 1)[1].split(". ", 1)[0]
    assert "validate phone numbers" in gated
    assert "appeal 10DLC Brand identity verification or external vetting" in gated


def test_instructions_state_that_the_server_does_not_see_the_conversation():
    assert "it does not see the conversation" in INSTRUCTIONS


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
        "Call `number_validator_results_get` for the results.",
        "Lists templates, then call `whatsapp_templates_get` for one.",
        "If the user approves, call the tool again with the token.",
        "Once approved, send the request again.",
        "First, show the preview to the user.",
        "Optionally, set the `language`.",
        "Get a download URL for a call recording. Fetch `download_url` to obtain the MP3.",
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
        "Requires the call ID; call recording is optional.",
        "Messages carry delivery and read receipts.",
        "A trial account can only send to verified numbers.",
        "The first call the server receives returns a preview.",
        "Lists each Sender ID with its type, use case, and allowlisted countries.",
        "Permission level: `read`: GET only.",
    ],
)
def test_guard_passes_descriptive_text(descriptive):
    assert imperative_violations(descriptive) == []


NON_CALLS = frozenset(test_toolsets.VALID) - {"calls"}
PRODUCT_TEXT = (
    "WhatsApp",
    "SMS",
    "2FA",
    "number-validator",
    "send-sms",
    "SIP trunks",
    "sub-accounts",
    "number_validator_create_bulk",
)


def test_no_selection_keeps_the_existing_text():
    text = build_instructions(None)

    assert text == INSTRUCTIONS
    assert "register call webhooks, register or change WhatsApp webhooks" in text
    assert "submit WhatsApp templates" in text
    assert "or change SIP trunks or sub-accounts" in text
    assert "it does not see the conversation" in text
    assert "voice calls" in text
    assert "Calls:\n- Whenever a call is automated or handled by AI" in text
    assert "Number purchases, calls, messages, and one-time passwords are charged" in text
    assert "number_validator_create_bulk" in text
    assert imperative_violations(text) == []


def test_instructions_without_calls_omit_live_calling():
    text = build_instructions(NON_CALLS)

    for absent in (
        "voice calls",
        "live call",
        "Calls:",
        "Terms of Service require the customer to disclose",
    ):
        assert absent not in text
    for present in ("SMS and MMS", "SIP trunks", "call detail records"):
        assert present in text
    assert imperative_violations(text) == []


def test_selection_names_its_products_only():
    text = build_instructions(frozenset({"numbers"}))

    assert "phone numbers" in text
    for absent in PRODUCT_TEXT:
        assert absent not in text
    assert imperative_violations(text) == []


def test_the_core_holds_no_product_text():
    for absent in (*PRODUCT_TEXT, "voice calls", "Calls:", "call detail records"):
        assert absent not in instructions.CORE


def test_every_spec_toolset_has_a_fragment():
    assert set(instructions.FRAGMENTS) == set(test_toolsets.VALID)
    assert len(instructions.FRAGMENTS) == 7


def test_a_spec_toolset_without_a_fragment_fails_the_build(upstream, monkeypatch):
    monkeypatch.delitem(instructions.FRAGMENTS, "numbers")

    with pytest.raises(RuntimeError, match="numbers"):
        server.build_server()


def _initialize_instructions(query):
    async def run():
        mcp = server.build_server()

        async def read(client):
            return client.initialize_result.instructions

        return await test_toolsets._with_client(mcp, query, read)

    return asyncio.run(run())


def test_initialize_applies_the_text_per_connection(upstream):
    assert _initialize_instructions("toolsets=numbers") == build_instructions(
        frozenset({"numbers"})
    )
    assert _initialize_instructions(None) == build_instructions(None)
