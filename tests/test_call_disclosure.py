"""The automated-call disclosure obligation stays in the shipped call text."""

import re

import pytest

from wavix_mcp.server import INSTRUCTIONS, TOOL_DESCRIPTIONS

DISCLOSURE_TOOLS = ("call_control_create", "call_control_collect", "voice_campaigns_create")
OBLIGATION = "this is an obligation under the Wavix Terms of Service"
ENFORCEMENT_PHRASES = ("wavix verifies", "wavix checks", "wavix enforces", "enforce")


def disclosure_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=\.)\s+", text) if "Terms of Service" in s]


@pytest.mark.parametrize("tool", DISCLOSURE_TOOLS)
def test_call_tool_states_the_obligation(tool: str) -> None:
    assert OBLIGATION in TOOL_DESCRIPTIONS[tool]


def test_collect_scopes_the_obligation_to_outbound_calls() -> None:
    assert "On outbound calls" in TOOL_DESCRIPTIONS["call_control_collect"]


def test_create_states_the_conditional_form() -> None:
    assert (
        "Whenever the call is automated or handled by AI"
        in TOOL_DESCRIPTIONS["call_control_create"]
    )


def test_instructions_state_the_obligation_once() -> None:
    assert INSTRUCTIONS.count("Terms of Service") == 1
    assert "automated or handled by AI" in INSTRUCTIONS


@pytest.mark.parametrize("name", [*DISCLOSURE_TOOLS, "instructions"])
def test_text_avoids_ai_initiated_and_enforcement_claims(name: str) -> None:
    text = INSTRUCTIONS if name == "instructions" else TOOL_DESCRIPTIONS[name]
    assert "ai-initiated" not in text.lower()
    sentences = disclosure_sentences(text)
    assert sentences
    for sentence in sentences:
        assert not any(phrase in sentence.lower() for phrase in ENFORCEMENT_PHRASES)
