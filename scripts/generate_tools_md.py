#!/usr/bin/env python3
"""Regenerate TOOLS.md and patch README.md from the Wavix OpenAPI spec.

Run after the upstream OpenAPI spec changes (new tools added, renamed, or
removed). The ``tools-drift`` CI job verifies that the committed files match
what this script produces against the live spec.

    python scripts/generate_tools_md.py

Outputs (in place, relative to the repository root):

* ``TOOLS.md`` — full catalogue
* ``README.md`` — the generated block between ``<!-- tools:start -->`` and
  ``<!-- tools:end -->``.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

# Reuse the same loader the server uses so we hit the same OpenAPI URL,
# resolve $ref / allOf identically, and pick up any preprocessing tweaks.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from wavix_mcp.server import load_spec  # noqa: E402

# Operations whose response is a pre-signed download URL rather than the JSON
# body. Mirrors BINARY_STREAM_ENDPOINTS in src/wavix_mcp/server.py.
PRE_SIGNED = {
    "call_recording_get",
    "billing_invoices_download",
    "speech_analytics_file_get",
    "ten_dlc_brand_evidence_get",
}

# Polished display names for tags that need it in the public catalogue.
TAG_DISPLAY = {
    "Wavix Embeddable": "Wavix Embeddable (WebRTC)",
}

# Curated short descriptions for the README overview table. One sentence per
# tag describing the domain — when a new tag appears upstream, add an entry
# here. Tags without an entry fall back to a placeholder that fails CI.
COVERAGE: dict[str, str] = {
    "SMS and MMS": "Send, list, retrieve messages; sender IDs; opt-outs",
    "Call control": "Start / answer / end calls; play audio; collect DTMF",
    "Call recording": "List, download (pre-signed URL), delete",
    "Call streaming": "Start / stop media stream",
    "Call webhooks": "List, create, delete",
    "CDRs": "List, export, retrieve; transcription search and retranscribe",
    "Speech Analytics": "Upload, transcribe, retrieve original file",
    "2FA": "Create / check / cancel / resend verification; events",
    "My numbers": "List, update, release; SMS / voice routing; document upload",
    "Buy": "Countries, regions, cities; available number search",
    "Cart": "Add, remove, retrieve, checkout",
    "Number validator": "Single and bulk validation",
    "SIP trunks": "Full CRUD",
    "10DLC": "Brands, campaigns, vetting, evidence, event subscriptions",
    "Profile": "Get / update profile; account config",
    "API Keys": "List, create, activate / deactivate, delete",
    "Sub-accounts": "List, create, get, update; transactions",
    "Billing": "Transactions, invoices, statement download",
    "Voice campaigns": "Trigger and retrieve",
    "Wavix Embeddable": "Widget tokens CRUD",
    "Link shortener": "Create short links; metrics",
}

# Curated order for the public catalogue (more readable than the OpenAPI
# declaration order). New tags that appear upstream are appended
# alphabetically — update this tuple when adding a new domain.
TAG_ORDER: tuple[str, ...] = (
    "SMS and MMS",
    "Call control",
    "Call recording",
    "Call streaming",
    "Call webhooks",
    "CDRs",
    "Speech Analytics",
    "2FA",
    "My numbers",
    "Buy",
    "Cart",
    "Number validator",
    "SIP trunks",
    "10DLC",
    "Profile",
    "API Keys",
    "Sub-accounts",
    "Billing",
    "Voice campaigns",
    "Wavix Embeddable",
    "Link shortener",
)

# Optional intra-tag sub-grouping. Each entry maps a tag to an ordered list of
# (subheading, predicate) pairs. The first matching predicate wins.
SUB_GROUPS: dict[str, list[tuple[str, Callable[[str], bool]]]] = {
    "10DLC": [
        ("Brands", lambda opid: opid.startswith("ten_dlc_brand") and "campaign" not in opid),
        ("Campaigns", lambda opid: "campaign" in opid),
        ("Event subscriptions", lambda opid: opid.startswith("ten_dlc_subscriptions")),
    ],
}

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

TOOLS_HEADER = """\
# Wavix MCP Tools — full catalogue

{count} tools, generated from the [Wavix OpenAPI spec](https://github.com/Wavix/wavix-openapi) via [`FastMCP.from_openapi()`](https://github.com/jlowin/fastmcp). Arguments mirror request path / query / body fields.

If a tool listed here is missing from your client, refresh the connection: the live catalogue tracks the OpenAPI spec as it evolves.

For the high-level grouping and an overview table, see the [README](README.md#tools).

"""

README_BLOCK_TEMPLATE = """\
{count} tools, generated from the [Wavix OpenAPI spec](https://github.com/Wavix/wavix-openapi). Arguments mirror request parameters and body fields.

| Group | # | Coverage |
| --- | ---: | --- |
{rows}\
"""


def _slugify(name: str) -> str:
    """GitHub-flavoured anchor slug for a heading."""
    s = name.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s


def _format_tool_line(opid: str, summary: str) -> str:
    line = f"- `{opid}` — {summary}"
    if opid in PRE_SIGNED:
        line += " (returns a pre-signed download URL)"
    return line


def _collect_groups(spec: dict) -> dict[str, list[tuple[str, str]]]:
    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path_item in (spec.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            opid = op.get("operationId")
            if not opid:
                continue
            summary = (op.get("summary") or "").strip()
            tag = (op.get("tags") or ["Other"])[0]
            groups[tag].append((opid, summary))
    return groups


def _ordered_tags(groups: dict) -> list[str]:
    return [t for t in TAG_ORDER if t in groups] + sorted(t for t in groups if t not in TAG_ORDER)


def _build_tools_md(groups: dict, ordered: list[str]) -> str:
    total = sum(len(ops) for ops in groups.values())
    out: list[str] = [TOOLS_HEADER.format(count=total)]
    for tag in ordered:
        display = TAG_DISPLAY.get(tag, tag)
        out.append(f"## {display}\n\n")
        ops = groups[tag]
        sub_spec = SUB_GROUPS.get(tag)
        if sub_spec:
            buckets: dict[str, list[tuple[str, str]]] = {name: [] for name, _ in sub_spec}
            for opid, summary in ops:
                for name, predicate in sub_spec:
                    if predicate(opid):
                        buckets[name].append((opid, summary))
                        break
            for name, _ in sub_spec:
                if not buckets[name]:
                    continue
                out.append(f"### {name}\n\n")
                out.extend(_format_tool_line(o, s) + "\n" for o, s in buckets[name])
                out.append("\n")
        else:
            out.extend(_format_tool_line(o, s) + "\n" for o, s in ops)
            out.append("\n")
    return "".join(out)


def _build_readme_block(groups: dict, ordered: list[str]) -> str:
    total = sum(len(ops) for ops in groups.values())
    rows: list[str] = []
    missing_coverage: list[str] = []
    for tag in ordered:
        display = TAG_DISPLAY.get(tag, tag)
        anchor = _slugify(display)
        count = len(groups[tag])
        coverage = COVERAGE.get(tag)
        if coverage is None:
            missing_coverage.append(tag)
            coverage = (
                "TODO: add coverage description for this group in scripts/generate_tools_md.py"
            )
        rows.append(f"| [{display}](TOOLS.md#{anchor}) | {count} | {coverage} |")
    if missing_coverage:
        print(
            f"warning: missing COVERAGE entries for: {', '.join(missing_coverage)}",
            file=sys.stderr,
        )
    return README_BLOCK_TEMPLATE.format(count=total, rows="\n".join(rows) + "\n")


def _patch_readme(path: Path, block: str) -> None:
    text = path.read_text(encoding="utf-8")

    start, end = "<!-- tools:start -->", "<!-- tools:end -->"
    pattern = re.compile(
        re.escape(start) + r"\n(.*?)" + re.escape(end),
        flags=re.DOTALL,
    )
    replacement = f"{start}\n{block}\n{end}"
    new_text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise SystemExit(
            f"README.md: could not find {start} / {end} markers around the Tools section"
        )

    path.write_text(new_text, encoding="utf-8")


def main() -> None:
    spec = load_spec()
    groups = _collect_groups(spec)
    ordered = _ordered_tags(groups)
    total = sum(len(ops) for ops in groups.values())

    tools_md = _build_tools_md(groups, ordered)
    readme_block = _build_readme_block(groups, ordered)

    (ROOT / "TOOLS.md").write_text(tools_md, encoding="utf-8")
    _patch_readme(ROOT / "README.md", readme_block)

    print(f"wrote TOOLS.md ({total} tools across {len(ordered)} groups)", file=sys.stderr)
    print("patched README.md (Tools block)", file=sys.stderr)


if __name__ == "__main__":
    main()
