import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_check.py"
SPEC = importlib.util.spec_from_file_location("release_check", SCRIPT)
rc = importlib.util.module_from_spec(SPEC)
sys.modules["release_check"] = rc  # @dataclass resolves annotations through sys.modules
SPEC.loader.exec_module(rc)


def changelog(body: str = "### Added\n\n- Something new.", pending: str = "") -> str:
    notes = f"{pending}\n\n" if pending else ""
    return (
        f"# Changelog\n\n## [Unreleased]\n\n{notes}## [1.3.3] - 2026-10-22\n\n{body}\n\n"
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


def test_bullets_close_on_blank_and_heading():
    wrapped = "### Added\n\n- `GET /a` returns\n  `b`.\n- Second.\n\n### Changed\n"
    assert rc.bullets(wrapped) == ["- `GET /a` returns `b`.", "- Second."]
    assert rc.bullets("### Added\n") == []
    assert rc.bullets("- A.\n\n  indented after a blank\n### H\n  under a heading\n") == ["- A."]
    assert rc.bullets("- A.  \n- B.\t\n") == ["- A.", "- B."]
    assert rc.bullets("- A.\n- A.\n") == ["- A."]


def test_unreleased_section_ends_at_next_heading_or_link_references():
    assert rc.unreleased("## [Unreleased]\n\n- A.\n\n## [1.0.0]\n\n- B.\n") == "- A."
    refs = "## [Unreleased]\n\n- A.\n\n[Unreleased]: https://x.test/a\n[1.0.0]: https://x.test/b\n"
    assert rc.unreleased(refs) == "- A."
    assert rc.unreleased("## [Unreleased]\n") == ""
    assert rc.unreleased("## [1.0.0]\n\n- B.\n") is None


def test_unreleased_ticket(tmp_path):
    log = changelog(pending="- Fixed DEV-9.")
    assert rc.check(fixture(tmp_path, log=log)).problems == [
        "CHANGELOG.md ## [Unreleased] names an internal ticket: DEV-9"
    ]


def test_missing_unreleased_heading(tmp_path):
    log = changelog().replace("## [Unreleased]\n\n", "")
    assert rc.check(fixture(tmp_path, log=log)).problems == [
        "CHANGELOG.md has no ## [Unreleased] section"
    ]


@pytest.fixture(autouse=True)
def clean_git_environment(monkeypatch):
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def committed_repo(tmp_path: Path, pending: str = "- Existing note.") -> Path:
    repo = fixture(tmp_path, log=changelog(pending=pending))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def commit_change(repo: Path, path: str, pending: str | None = None) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n")
    if pending is not None:
        (repo / "CHANGELOG.md").write_text(changelog(pending=pending))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")


def test_notes_since_new_bullet_passes(tmp_path):
    repo = committed_repo(tmp_path)
    commit_change(repo, "src/x.py", pending="- Existing note.\n- New note.")
    assert rc.main(["--notes-since", "HEAD^1"], repo) == 0


def test_notes_since_removed_bullet_passes(tmp_path):
    repo = committed_repo(tmp_path)
    commit_change(repo, "src/x.py", pending="")
    assert rc.main(["--notes-since", "HEAD^1"], repo) == 0


def test_notes_since_heading_only_fails(tmp_path, capsys):
    repo = committed_repo(tmp_path)
    commit_change(repo, "src/x.py", pending="- Existing note.\n\n### Fixed")
    assert rc.main(["--notes-since", "HEAD^1"], repo) == 1
    err = capsys.readouterr().err
    assert "CHANGELOG.md" in err
    assert "## [Unreleased]" in err
    assert "src/x.py" in err


def test_notes_since_visible_change_without_note_fails(tmp_path):
    for path in ("src/wavix_mcp/server.py", "TOOLS.md", "README.md"):
        root = tmp_path / path.replace("/", "_")
        root.mkdir()
        repo = committed_repo(root)
        commit_change(repo, path)
        assert rc.main(["--notes-since", "HEAD^1"], repo) == 1, path


def test_notes_since_no_visible_change_passes(tmp_path, capsys):
    repo = committed_repo(tmp_path)
    commit_change(repo, "tests/test_x.py")
    assert rc.main(["--notes-since", "HEAD^1"], repo) == 0
    assert "no change visible to users" in capsys.readouterr().out


def test_notes_since_moved_out_of_src_is_visible(tmp_path):
    repo = committed_repo(tmp_path)
    commit_change(repo, "src/x.py")
    (repo / "scripts").mkdir(exist_ok=True)
    git(repo, "mv", "src/x.py", "scripts/x.py")
    git(repo, "commit", "-q", "-m", "move")
    assert rc.main(["--notes-since", "HEAD^1"], repo) == 1


def test_notes_since_needs_a_revision(tmp_path):
    repo = committed_repo(tmp_path)
    assert rc.main(["--notes-since"], repo) == 2
    assert rc.main(["--published"], repo) == 2


def test_notes_since_unknown_revision_is_an_error(tmp_path):
    repo = committed_repo(tmp_path)
    assert rc.main(["--notes-since", "no-such-rev"], repo) == 2


def test_published_leaked_note_fails_listing_it(tmp_path, capsys):
    repo = fixture(tmp_path, log=changelog("- A, wrapped\n  here.\n- B."))
    body = tmp_path / "published.md"
    body.write_text("- A, wrapped here.\n")
    assert rc.main(["--published", str(body)], repo) == 1
    err = capsys.readouterr().err
    assert "CHANGELOG.md ## [1.3.3] has notes that were never published" in err
    assert "- B." in err
    assert "- A, wrapped" not in err


def test_published_equal_passes(tmp_path):
    repo = fixture(tmp_path, log=changelog("- A, wrapped\n  here.\n- B."))
    body = tmp_path / "published.md"
    body.write_text("- A, wrapped here.\n- B.\n")
    assert rc.main(["--published", str(body)], repo) == 0
