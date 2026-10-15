"""Tests for the TOOLS.md / README generator."""

import importlib.util
import re
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "generate_tools_md.py"
_spec = importlib.util.spec_from_file_location("generate_tools_md", _SCRIPT)
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

_SPEC = {
    "paths": {
        "/v1/cdrs": {"get": {"operationId": "cdrs_list", "summary": "List CDRs", "tags": ["CDRs"]}},
        "/v1/cdrs/{call_id}/transcription": {
            "get": {
                "operationId": "cdrs_transcription_get",
                "summary": "Get a transcription",
                "tags": ["CDRs"],
            }
        },
        "/v1/profile": {
            "get": {"operationId": "profile_get", "summary": "Get profile", "tags": ["Profile"]}
        },
    }
}

_COUNT = re.compile(r"\d+\s+tools", re.IGNORECASE)


def test_collect_groups_keeps_only_tools_the_server_registers():
    groups = gen._collect_groups(_SPEC, {"cdrs_list", "profile_get"})

    assert dict(groups) == {
        "CDRs": [("cdrs_list", "List CDRs")],
        "Profile": [("profile_get", "Get profile")],
    }


def test_collect_groups_fails_on_a_server_tool_missing_from_the_spec():
    with pytest.raises(SystemExit, match="unknown_tool"):
        gen._collect_groups(_SPEC, {"cdrs_list", "unknown_tool"})


def test_outputs_state_no_tool_count_and_explain_what_determines_the_set():
    groups = gen._collect_groups(_SPEC, {"cdrs_list", "cdrs_transcription_get", "profile_get"})
    ordered = gen._ordered_tags(groups)

    tools_md = gen._build_tools_md(groups, ordered)
    readme_block = gen._build_readme_block(groups, ordered)

    for text in (tools_md, readme_block):
        assert not _COUNT.search(text)
        assert gen.SCOPE_SENTENCE in text
    assert "| # |" not in readme_block
    assert "| [CDRs](TOOLS.md#cdrs) | List, retrieve" in readme_block
