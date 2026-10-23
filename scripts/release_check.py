"""Release checks for the MCP server: version files agree, CHANGELOG section is publishable."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

TICKET = re.compile(r"(?:DEV|WAV)-[0-9]+", re.IGNORECASE)
ROOT = Path(__file__).resolve().parents[1]
PROJECT_VERSION = re.compile(r'^version = "(.+?)"', re.MULTILINE)


@dataclass
class Result:
    version: str
    problems: list[str] = field(default_factory=list)
    body: str | None = None


def check(root: Path) -> Result:
    version = PROJECT_VERSION.search((root / "pyproject.toml").read_text())[1]
    result = Result(version)
    server = json.loads((root / "server.json").read_text()).get("version")
    if server != version:
        result.problems.append(f"version mismatch: pyproject.toml {version}, server.json {server}")
    section = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)",
        (root / "CHANGELOG.md").read_text(),
        re.MULTILINE | re.DOTALL,
    )
    result.body = section[1].strip() if section else None
    if result.body is None:
        result.problems.append(f"CHANGELOG.md has no ## [{version}] section")
    elif not result.body:
        result.problems.append(f"CHANGELOG.md ## [{version}] section is empty")
    elif ticket := TICKET.search(result.body):
        result.problems.append(f"CHANGELOG.md ## [{version}] names an internal ticket: {ticket[0]}")
    return result


def main(argv: list[str], root: Path = ROOT) -> int:
    result = check(root)
    if result.problems:
        for problem in result.problems:
            print(f"release-check: {problem}", file=sys.stderr)
        return 1
    if argv == ["--version"]:
        print(result.version)
    elif argv == ["--notes"]:
        print(result.body)
    else:
        print(f"release-check: {result.version} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
