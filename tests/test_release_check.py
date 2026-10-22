import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_check.py"
SPEC = importlib.util.spec_from_file_location("release_check", SCRIPT)
rc = importlib.util.module_from_spec(SPEC)
sys.modules["release_check"] = rc  # @dataclass resolves annotations through sys.modules
SPEC.loader.exec_module(rc)


def changelog(body: str = "### Added\n\n- Something new.") -> str:
    return (
        f"# Changelog\n\n## [Unreleased]\n\n## [1.3.3] - 2026-10-22\n\n{body}\n\n"
        "## [1.0.0] - 2026-01-01\n\n- Old, from DEV-1.\n"
    )


def fixture(tmp_path: Path, server: str = "1.3.3", log: str | None = None) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.3.3"\n')
    (tmp_path / "server.json").write_text(json.dumps({"version": server}))
    (tmp_path / "CHANGELOG.md").write_text(changelog() if log is None else log)
    return tmp_path


def test_matching_versions_pass(tmp_path):
    assert rc.check(fixture(tmp_path)).problems == []


def test_server_json_mismatch_names_the_file(tmp_path):
    assert rc.check(fixture(tmp_path, server="1.3.2")).problems == [
        "version mismatch: pyproject.toml 1.3.3, server.json 1.3.2"
    ]


def test_missing_section(tmp_path):
    log = changelog().replace("[1.3.3]", "[1.3.2]")
    assert rc.check(fixture(tmp_path, log=log)).problems == [
        "CHANGELOG.md has no ## [1.3.3] section"
    ]


def test_blank_section(tmp_path):
    assert rc.check(fixture(tmp_path, log=changelog("  \n"))).problems == [
        "CHANGELOG.md ## [1.3.3] section is empty"
    ]


def test_internal_ticket(tmp_path):
    assert rc.check(fixture(tmp_path, log=changelog("- Fixed DEV-123."))).problems == [
        "CHANGELOG.md ## [1.3.3] names an internal ticket: DEV-123"
    ]


def test_lowercase_ticket(tmp_path):
    assert rc.check(fixture(tmp_path, log=changelog("- Fixed wav-7."))).problems == [
        "CHANGELOG.md ## [1.3.3] names an internal ticket: wav-7"
    ]


def test_main_returns_1_on_a_problem(tmp_path):
    assert rc.main([], fixture(tmp_path, server="1.3.2")) == 1
    other = tmp_path / "other"
    other.mkdir()
    assert rc.main([], fixture(other, log=changelog("  \n"))) == 1


def test_notes_keep_subheadings(tmp_path):
    log = changelog("### Added\n\n- A.\n\n### Fixed\n\n- B.")
    assert rc.check(fixture(tmp_path, log=log)).body == "### Added\n\n- A.\n\n### Fixed\n\n- B."


def test_real_files_pass():
    assert rc.check(ROOT).problems == []


def test_cli_on_the_real_files():
    version = subprocess.run(
        [sys.executable, str(SCRIPT), "--version"], text=True, capture_output=True
    )
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert version.stdout == f"{rc.PROJECT_VERSION.search(pyproject)[1]}\n"
    notes = subprocess.run([sys.executable, str(SCRIPT), "--notes"], text=True, capture_output=True)
    assert (notes.returncode, notes.stdout) == (0, f"{rc.check(ROOT).body}\n")
