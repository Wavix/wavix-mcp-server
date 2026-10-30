"""Release checks for the MCP server: version files agree, CHANGELOG section is publishable."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

TICKET = re.compile(r"(?:DEV|WAV)-[0-9]+", re.IGNORECASE)
ROOT = Path(__file__).resolve().parents[1]
PROJECT_VERSION = re.compile(r'^version = "(.+?)"', re.MULTILINE)
UNRELEASED = re.compile(
    r"^## \[Unreleased\]\n(.*?)"
    r"(?=^## |^\[[^\]]+\]: [^\n]*(?:\n(?:\[[^\]]+\]: [^\n]*)?)*\Z|\Z)",
    re.MULTILINE | re.DOTALL,
)
VISIBLE_PREFIX = "src/"
VISIBLE_FILES = ("TOOLS.md", "README.md")
USAGE = "usage: release_check.py [--version | --notes | --notes-since <rev> | --published <file>]"


def unreleased(changelog: str) -> str | None:
    found = UNRELEASED.search(changelog)
    return found[1].strip() if found else None


def bullets(section: str) -> list[str]:
    found: list[str] = []
    open_bullet = False
    for line in section.split("\n"):
        if line.startswith("- "):
            found.append(line)
            open_bullet = True
        elif open_bullet and re.match(r"\s+\S", line):
            found[-1] += f" {line}"
        else:
            open_bullet = False
    return list(dict.fromkeys(" ".join(b.split()) for b in found))


@dataclass
class Result:
    version: str
    problems: list[str] = field(default_factory=list)
    body: str | None = None


def check(root: Path) -> Result:
    version = PROJECT_VERSION.search((root / "pyproject.toml").read_text())[1]
    changelog = (root / "CHANGELOG.md").read_text()
    result = Result(version)
    server = json.loads((root / "server.json").read_text()).get("version")
    if server != version:
        result.problems.append(f"version mismatch: pyproject.toml {version}, server.json {server}")
    section = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)",
        changelog,
        re.MULTILINE | re.DOTALL,
    )
    result.body = section[1].strip() if section else None
    if result.body is None:
        result.problems.append(f"CHANGELOG.md has no ## [{version}] section")
    elif not result.body:
        result.problems.append(f"CHANGELOG.md ## [{version}] section is empty")
    elif ticket := TICKET.search(result.body):
        result.problems.append(f"CHANGELOG.md ## [{version}] names an internal ticket: {ticket[0]}")
    pending = unreleased(changelog)
    if pending is None:
        result.problems.append("CHANGELOG.md has no ## [Unreleased] section")
    elif ticket := TICKET.search(pending):
        result.problems.append(
            f"CHANGELOG.md ## [Unreleased] names an internal ticket: {ticket[0]}"
        )
    return result


def is_visible(path: str) -> bool:
    return path.startswith(VISIBLE_PREFIX) or path in VISIBLE_FILES


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)


def notes_since(rev: str, root: Path) -> int:
    diff = git(root, "diff", "--name-only", "--no-renames", rev, "HEAD")
    base = git(root, "show", f"{rev}:CHANGELOG.md")
    for failed in (diff, base):
        if failed.returncode:
            print(f"release-check: {failed.stderr.strip()}", file=sys.stderr)
            return 2
    visible = [p for p in diff.stdout.splitlines() if is_visible(p)]
    if not visible:
        print("release-check: no change visible to users")
        return 0
    before = bullets(unreleased(base.stdout) or "")
    head = bullets(unreleased((root / "CHANGELOG.md").read_text()) or "")
    if set(before) == set(head):
        print(
            f"release-check: this change touches {visible[0]}, which users see.\n"
            "Add a bullet (- ...) under ## [Unreleased] in CHANGELOG.md,\n"
            "in the Added, Changed, Deprecated, Removed or Fixed subsection, written for\n"
            "an external reader, without internal ticket IDs.",
            file=sys.stderr,
        )
        return 1
    print("release-check: [Unreleased] notes changed")
    return 0


def published(body_file: Path, result: Result) -> int:
    section = set(bullets(body_file.read_text()))
    leaked = [b for b in bullets(result.body or "") if b not in section]
    if leaked:
        print(
            f"release-check: CHANGELOG.md ## [{result.version}] has notes that were never "
            "published: move them to ## [Unreleased]",
            file=sys.stderr,
        )
        for bullet in leaked:
            print(bullet, file=sys.stderr)
        return 1
    print(f"release-check: {result.version} published notes match")
    return 0


def main(argv: list[str], root: Path = ROOT) -> int:
    if argv[:1] in (["--notes-since"], ["--published"]) and len(argv) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    result = check(root)
    if result.problems:
        for problem in result.problems:
            print(f"release-check: {problem}", file=sys.stderr)
        return 1
    if argv[:1] == ["--notes-since"]:
        return notes_since(argv[1], root)
    if argv[:1] == ["--published"]:
        return published(Path(argv[1]), result)
    if argv == ["--version"]:
        print(result.version)
    elif argv == ["--notes"]:
        print(result.body)
    else:
        print(f"release-check: {result.version} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
