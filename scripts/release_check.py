"""Release checks for the MCP server: version files, CHANGELOG section, ordering, Registry."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SEMVER = re.compile(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)")
TICKET = re.compile(r"(?:DEV|WAV)-[0-9]+", re.IGNORECASE)
TABLE_HEADER = re.compile(r"^\[[^\]]*\]\s*(#.*)?$")
PROJECT_VERSION = re.compile(r"^version\s*=\s*([\"'])(.+?)\1", re.MULTILINE)
REGISTRY_UNREADABLE = "Registry answer is not the versions JSON"
USAGE = (
    "usage: release_check.py [--version | --notes]\n"
    "       release_check.py --newer <version> [<tag>...]\n"
    "       release_check.py --registry-has <version>"
)


@dataclass
class Result:
    version: str
    problems: list[str] = field(default_factory=list)
    body: str | None = None


def _parse(v: str) -> tuple[int, int, int]:
    m = SEMVER.fullmatch(v)
    if not m:
        raise ValueError(f"not a version: {v}")
    return int(m[1]), int(m[2]), int(m[3])


def newer(version: str, tags: list[str]) -> int:
    try:
        mine = _parse(version)
        parsed = [_parse(t) for t in tags]
    except ValueError:
        return 1
    if any(mine < other for other in parsed):
        return 1
    return 3 if mine in parsed else 0


def _section(changelog: str, version: str) -> str | None:
    lines = changelog.split("\n")
    heading = f"## [{version}]"
    start = next(
        (i for i, line in enumerate(lines) if line == heading or line.startswith(f"{heading} - ")),
        None,
    )
    if start is None:
        return None
    rest = lines[start + 1 :]
    end = next((i for i, line in enumerate(rest) if line.startswith("## ")), len(rest))
    return "\n".join(rest[:end]).strip()


def _project_version(pyproject: str) -> str:
    table: list[str] = []
    in_project = False
    for line in pyproject.split("\n"):
        if TABLE_HEADER.match(line):
            in_project = line.split("#")[0].strip() == "[project]"
        elif in_project:
            table.append(line)
    m = PROJECT_VERSION.search("\n".join(table))
    return m[2] if m else ""


def check(root: Path) -> Result:
    version = _project_version((root / "pyproject.toml").read_text())
    result = Result(version)
    server = json.loads((root / "server.json").read_text()).get("version")
    if server != version:
        result.problems.append(f"version mismatch: pyproject.toml {version}, server.json {server}")
    body = _section((root / "CHANGELOG.md").read_text(), version)
    if body is None:
        result.problems.append(f"CHANGELOG.md has no ## [{version}] section")
    elif body == "":
        result.problems.append(f"CHANGELOG.md ## [{version}] section is empty")
    elif t := TICKET.search(body):
        result.problems.append(f"CHANGELOG.md ## [{version}] names an internal ticket: {t[0]}")
    result.body = body
    return result


def registry_has(doc: object, version: str) -> bool:
    servers = doc.get("servers") if isinstance(doc, dict) else None
    if not isinstance(servers, list) or not all(
        isinstance(s, dict) and isinstance(s.get("server"), dict) for s in servers
    ):
        raise ValueError(REGISTRY_UNREADABLE)
    return any(s["server"].get("version") == version for s in servers)


def _usage_error() -> int:
    print(f"release-check: {USAGE}", file=sys.stderr)
    return 2


def _newer_cli(version: str, tags: list[str]) -> int:
    code = newer(version, tags)
    if code == 1:
        try:
            _parse(version)
            highest = max(tags, key=_parse)
            print(
                f"release-check: {version} is below the latest release {highest}", file=sys.stderr
            )
        except ValueError as e:
            print(f"release-check: {e}", file=sys.stderr)
    return code


def main(argv: list[str]) -> int:
    if argv[:1] == ["--newer"]:
        return _newer_cli(argv[1], argv[2:]) if len(argv) >= 2 else _usage_error()
    if argv[:1] == ["--registry-has"]:
        if len(argv) != 2:
            return _usage_error()
        try:
            return 0 if registry_has(json.load(sys.stdin), argv[1]) else 1
        except ValueError:
            print(f"release-check: {REGISTRY_UNREADABLE}", file=sys.stderr)
            return 2
    root = Path(os.environ.get("RELEASE_CHECK_ROOT") or Path(__file__).resolve().parents[1])
    result = check(root)
    if result.problems:
        for p in result.problems:
            print(f"release-check: {p}", file=sys.stderr)
        return 1
    if argv == ["--version"]:
        print(result.version)
    elif argv == ["--notes"]:
        print(result.body)
    elif not argv:
        print(f"release-check: {result.version} ok")
    else:
        print(f"release-check: unknown arguments {argv}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
