"""The automated-call disclosure obligation stays in the shipped call text."""

import re

import pytest

from wavix_mcp.server import INSTRUCTIONS, TOOL_DESCRIPTIONS

DISCLOSURE_TOOLS = ("call_control_create", "call_control_collect", "voice_campaigns_create")
TEXT_NAMES = (*DISCLOSURE_TOOLS, "instructions")
OBLIGATION = "this is an obligation under the Wavix Terms of Service"
DISCLOSURE_CORE = ("disclose", "at the start of the call")
LEGITIMATE_PHRASE = "active or verified number"
UNSUPPORTED_CLAIMS = re.compile(
    r"ai[- ]?initiated|verif|check|enforc|monitor",
    re.IGNORECASE,
)


def shipped_text(name):
    return INSTRUCTIONS if name == "instructions" else TOOL_DESCRIPTIONS[name]


def disclosure_violations(text):
    return UNSUPPORTED_CLAIMS.findall(text.replace(LEGITIMATE_PHRASE, ""))


@pytest.mark.parametrize("name", TEXT_NAMES)
def test_text_states_the_disclosure_itself(name):
    text = shipped_text(name)
    for phrase in DISCLOSURE_CORE:
        assert phrase in text


@pytest.mark.parametrize("tool", DISCLOSURE_TOOLS)
def test_call_tool_states_the_tos_obligation(tool):
    assert OBLIGATION in TOOL_DESCRIPTIONS[tool]


def test_collect_scopes_the_obligation_to_outbound_calls():
    assert "On outbound calls" in TOOL_DESCRIPTIONS["call_control_collect"]


def test_create_states_the_conditional_form():
    assert (
        "Whenever the call is automated or handled by AI"
        in TOOL_DESCRIPTIONS["call_control_create"]
    )


def test_instructions_state_the_obligation_once():
    assert INSTRUCTIONS.count("Terms of Service") == 1
    assert "automated or handled by AI" in INSTRUCTIONS


@pytest.mark.parametrize("name", TEXT_NAMES)
def test_text_makes_no_ai_initiated_or_enforcement_claim(name):
    assert disclosure_violations(shipped_text(name)) == []


@pytest.mark.parametrize("name", TEXT_NAMES)
@pytest.mark.parametrize(
    "planted",
    [
        "Wavix verifies the disclosure on every call.",
        "The disclosure is verified by Wavix.",
        "Compliance is monitored by Wavix.",
        "Wavix enforces this rule.",
        "Calls are AI-initiated.",
        "Calls are AI initiated.",
    ],
)
def test_guard_catches_planted_claim(name, planted):
    assert disclosure_violations(f"{shipped_text(name)} {planted}") != []
