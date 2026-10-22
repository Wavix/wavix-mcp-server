import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "release_check", Path(__file__).resolve().parents[1] / "scripts" / "release_check.py"
)
rc = importlib.util.module_from_spec(SPEC)
sys.modules["release_check"] = rc  # @dataclass resolves annotations through sys.modules
SPEC.loader.exec_module(rc)


def changelog(v: str, body: str = "### Added\n\n- Something new.") -> str:
    return (
        f"# Changelog\n\n## [Unreleased]\n\n## [{v}] - 2026-10-22\n\n{body}\n\n"
        "## [1.0.0] - 2026-01-01\n\n- Old, from DEV-1.\n"
    )


def fixture(
    tmp_path: Path, py: str = "1.3.3", srv: str | None = None, log: str | None = None
) -> Path:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{py}"\n')
    (tmp_path / "server.json").write_text(json.dumps({"version": srv or py}))
    (tmp_path / "CHANGELOG.md").write_text(log if log is not None else changelog(py))
    return tmp_path


def test_matching_versions_pass(tmp_path):
    assert rc.check(fixture(tmp_path)).problems == []


def test_server_json_mismatch_names_the_file(tmp_path):
    assert rc.check(fixture(tmp_path, srv="1.3.2")).problems == [
        "version mismatch: pyproject.toml 1.3.3, server.json 1.3.2"
    ]


def test_missing_section(tmp_path):
    assert rc.check(fixture(tmp_path, log=changelog("1.3.2"))).problems == [
        "CHANGELOG.md has no ## [1.3.3] section"
    ]


def test_heading_without_date(tmp_path):
    log = changelog("1.3.3").replace("## [1.3.3] - 2026-10-22", "## [1.3.3]")
    assert rc.check(fixture(tmp_path, log=log)).problems == []


def test_section_is_empty(tmp_path):
    assert rc.check(fixture(tmp_path, log=changelog("1.3.3", ""))).problems == [
        "CHANGELOG.md ## [1.3.3] section is empty"
    ]
    under_unreleased = (
        "# Changelog\n\n## [Unreleased]\n\n- Real entry.\n\n"
        "## [1.3.3] - 2026-10-22\n\n## [1.0.0]\n\n- Old.\n"
    )
    assert rc.check(fixture(tmp_path, log=under_unreleased)).problems == [
        "CHANGELOG.md ## [1.3.3] section is empty"
    ]


def test_whitespace_only_section_is_empty(tmp_path):
    assert rc.check(fixture(tmp_path, log=changelog("1.3.3", "  \n\t\n   "))).problems == [
        "CHANGELOG.md ## [1.3.3] section is empty"
    ]


@pytest.mark.parametrize(
    ("text", "ticket"),
    [
        ("Fixed DEV-123.", "DEV-123"),
        ("Fixed dev-123.", "dev-123"),
        ("Fixed WAV-9.", "WAV-9"),
        ("Branch tech_DEV-11973-x.", "DEV-11973"),
        ("Fixed DEV-123_fix.", "DEV-123"),
        ("Fixed xDEV-12.", "DEV-12"),
    ],
)
def test_internal_ticket(tmp_path, text, ticket):
    log = changelog("1.3.3", f"- {text}")
    assert rc.check(fixture(tmp_path, log=log)).problems == [
        f"CHANGELOG.md ## [1.3.3] names an internal ticket: {ticket}"
    ]


def test_version_is_read_from_the_project_table(tmp_path):
    fixture(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.other]\nversion = "9.9.9"\n\n[project]\nname = "x"\nversion = \'1.3.3\'\n\n'
        '[tool.after]\nversion = "8.8.8"\n'
    )
    assert rc.check(tmp_path).problems == []


def test_project_without_version_is_a_mismatch(tmp_path):
    fixture(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[tool.other]\nversion = "1.3.3"\n'
    )
    assert rc.check(tmp_path).problems[0] == "version mismatch: pyproject.toml , server.json 1.3.3"


def test_notes_keep_subheadings(tmp_path):
    log = changelog("1.3.3", "### Added\n\n- A.\n\n### Fixed\n\n- B.")
    assert rc.check(fixture(tmp_path, log=log)).body == "### Added\n\n- A.\n\n### Fixed\n\n- B."


@pytest.mark.parametrize(
    ("version", "tags", "code"),
    [
        ("1.3.3", ["v1.3.2", "v1.1.1"], 0),
        ("1.3.3", [], 0),
        ("1.3.2", ["v1.3.2"], 3),
        ("1.3.1", ["v1.3.2"], 1),
        ("1.10.0", ["v1.9.9"], 0),
        ("1.3.3", ["v1.3.x"], 1),
        ("1.3", ["v1.3.2"], 1),
        ("1.3.3", ["v1.1.1", "v1.4.0", "v1.0.0"], 1),
        ("1.3.2", ["v1.3.2", "v1.4.0"], 1),
    ],
)
def test_newer(version, tags, code):
    assert rc.newer(version, tags) == code


def test_registry_has_finds_a_non_first_entry():
    doc = {"servers": [{"server": {"version": v}} for v in ["1.3.1", "1.3.0", "1.2.0"]]}
    assert rc.registry_has(doc, "1.3.0") is True
    assert rc.registry_has(doc, "1.3.2") is False


REGISTRY_ANSWER = Path(__file__).resolve().parent / "fixtures" / "registry_versions.json"


def test_registry_has_on_a_recorded_registry_answer():
    doc = json.loads(REGISTRY_ANSWER.read_text())
    assert rc.registry_has(doc, "1.3.0") is True
    assert rc.registry_has(doc, "1.3.2") is False


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"servers": None},
        {"servers": 5},
        {"servers": {"server": {"version": "1.3.1"}}},
        {"servers": ["1.3.1"]},
        {"servers": [{"version": "1.3.1"}]},
        {"servers": [{"server": "1.3.1"}]},
        [],
        None,
    ],
)
def test_registry_has_rejects_an_unreadable_answer(doc):
    with pytest.raises(ValueError, match="Registry answer is not the versions JSON"):
        rc.registry_has(doc, "1.3.1")


def test_real_files_pass():
    assert rc.check(Path(__file__).resolve().parents[1]).problems == []


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_check.py"


def run(*args: str, stdin: str = "", root: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    env.pop("RELEASE_CHECK_ROOT", None)
    if root is not None:
        env["RELEASE_CHECK_ROOT"] = str(root)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
    )


def test_cli_newer_exit_codes_and_messages():
    assert run("--newer", "1.3.4", "v1.3.2").returncode == 0
    assert run("--newer", "1.3.2", "v1.3.2").returncode == 3
    below = run("--newer", "1.3.1", "v1.3.2")
    assert (below.returncode, below.stdout) == (1, "")
    assert below.stderr == "release-check: 1.3.1 is below the latest release v1.3.2\n"


def test_cli_newer_names_the_highest_tag_by_version():
    below = run("--newer", "1.3.3", "v1.1.1", "v1.10.0", "v1.9.0")
    assert below.returncode == 1
    assert below.stderr == "release-check: 1.3.3 is below the latest release v1.10.0\n"


@pytest.mark.parametrize(
    ("args", "culprit"),
    [
        (("1.3.3", "v1.4.0-rc1"), "v1.4.0-rc1"),
        (("1.3.3", "v1.9.0", "v1.4.0-rc1"), "v1.4.0-rc1"),
        (("1.3", "v1.3.2"), "1.3"),
    ],
)
def test_cli_newer_names_a_malformed_version(args, culprit):
    out = run("--newer", *args)
    assert out.returncode == 1
    assert out.stderr == f"release-check: not a version: {culprit}\n"


def test_cli_registry_has_exit_codes():
    versions = '{"servers": [{"server": {"version": "1.3.1"}}]}'
    assert run("--registry-has", "1.3.1", stdin=versions).returncode == 0
    assert run("--registry-has", "1.3.2", stdin=versions).returncode == 1
    assert run("--registry-has", "1.3.2", stdin=REGISTRY_ANSWER.read_text()).returncode == 1
    assert run("--registry-has", "1.3.0", stdin=REGISTRY_ANSWER.read_text()).returncode == 0


@pytest.mark.parametrize(
    "stdin", ["not json", "", "{}", '{"servers": null}', '{"servers": 5}', '{"servers": [1]}', "[]"]
)
def test_cli_registry_has_unreadable_answer_exits_2(stdin):
    out = run("--registry-has", "1.3.1", stdin=stdin)
    assert out.returncode == 2
    assert out.stderr == "release-check: Registry answer is not the versions JSON\n"


@pytest.mark.parametrize(
    "args", [("--registry-has",), ("--registry-has", "1.3.1", "1.3.0"), ("--newer",)]
)
def test_cli_wrong_argument_count_exits_2_with_usage(args):
    out = run(*args, stdin='{"servers": []}')
    assert out.returncode == 2
    assert out.stderr.startswith("release-check: usage:")


def test_cli_version_and_notes_print_exactly(tmp_path):
    fixture(tmp_path)
    assert run("--version", root=tmp_path).stdout == "1.3.3\n"
    assert run("--notes", root=tmp_path).stdout == "### Added\n\n- Something new.\n"
    assert run(root=tmp_path).stdout == "release-check: 1.3.3 ok\n"


def test_cli_problem_exits_1_with_the_message_on_stderr(tmp_path):
    fixture(tmp_path, srv="1.3.2")
    for args in ((), ("--version",), ("--notes",)):
        out = run(*args, root=tmp_path)
        assert (out.returncode, out.stdout) == (1, "")
        assert out.stderr == (
            "release-check: version mismatch: pyproject.toml 1.3.3, server.json 1.3.2\n"
        )


def test_cli_unknown_argument_exits_1(tmp_path):
    fixture(tmp_path)
    out = run("--bogus", root=tmp_path)
    assert (out.returncode, out.stdout) == (1, "")
    assert out.stderr == "release-check: unknown arguments ['--bogus']\n"
