"""The automated-call disclosure obligation stays in the shipped call text."""

import re

import pytest

from wavix_mcp.server import INSTRUCTIONS, TOOL_DESCRIPTIONS

TOS_CLAUSE = "this is an obligation under the Wavix Terms of Service."
DISCLOSURE_SENTENCES = {
    "call_control_create": (
        "What the called party hears depends on what the customer connects to the call: "
        "audio playback, synthesized prompts, or a media stream to an AI agent or to live agents.",
        "Whenever the call is automated or handled by AI, the customer must disclose this to the "
        f"called party at the start of the call; {TOS_CLAUSE}",
    ),
    "call_control_collect": (
        "This tool speaks synthesized text to the other party.",
        f"On outbound calls, the customer must disclose at the start of the call that it is automated; {TOS_CLAUSE}",
    ),
    "call_control_audio_play": (
        f"On outbound calls, the customer must disclose at the start of the call that it is automated; {TOS_CLAUSE}",
    ),
    "call_control_streams_create": (
        "On outbound calls that are automated or handled by AI, the customer must disclose this to "
        f"the called party at the start of the call; {TOS_CLAUSE}",
    ),
    "voice_campaigns_create": (
        "running a pre-approved call flow",
        f"The customer must disclose at the start of the call that it is automated; {TOS_CLAUSE}",
    ),
}
INSTRUCTIONS_RULE = (
    "Whenever a call is automated or handled by AI, the Wavix Terms of Service require the "
    "customer to disclose this at the start of the call."
)
EXEMPT_PHRASES = ("active or verified number",)
UNSUPPORTED_CLAIMS = re.compile(r"ai[- ]?initiated|verif|check|enforc|monitor", re.IGNORECASE)
SCOPES = (*DISCLOSURE_SENTENCES, "instructions")


def calls_section(instructions: str) -> str:
    return instructions.split("Calls:\n", 1)[1].split("\n\n", 1)[0]


def disclosure_scope(name: str) -> str:
    return calls_section(INSTRUCTIONS) if name == "instructions" else TOOL_DESCRIPTIONS[name]


def unsupported_claims(text: str) -> list[str]:
    for phrase in EXEMPT_PHRASES:
        text = text.replace(phrase, "")
    return UNSUPPORTED_CLAIMS.findall(text)


@pytest.mark.parametrize(("tool", "sentences"), DISCLOSURE_SENTENCES.items())
def test_call_tool_carries_the_disclosure_pair(tool, sentences):
    for sentence in sentences:
        assert sentence in TOOL_DESCRIPTIONS[tool]


def test_instructions_carry_the_rule_once_in_the_calls_section():
    assert INSTRUCTIONS.count(INSTRUCTIONS_RULE) == 1
    assert INSTRUCTIONS_RULE in calls_section(INSTRUCTIONS)


@pytest.mark.parametrize("name", SCOPES)
def test_disclosure_text_makes_no_ai_initiated_or_enforcement_claim(name):
    assert unsupported_claims(disclosure_scope(name)) == []


@pytest.mark.parametrize("name", SCOPES)
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
    assert unsupported_claims(f"{disclosure_scope(name)} {planted}") != []
