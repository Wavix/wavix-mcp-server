import asyncio
import logging
import re
import time

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

LLMS_URL = "https://docs.wavix.com/llms.txt"
OPENAPI_SPEC_URL = "https://wavix.github.io/wavix-openapi/wavix-api.yaml"
LINE_RE = re.compile(r"^- \[(?P<title>.+?)\]\((?P<url>.+?)\): (?P<desc>.+)$")
DESC_MAX = 200
TTL = 3600

_CACHE: dict[str, tuple[float, str, str | None]] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


async def _cached_get(client: httpx.AsyncClient, url: str) -> str:
    hit = _CACHE.get(url)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    # setdefault is race-free in single-threaded asyncio (no await between get and insert).
    lock = _LOCKS.setdefault(url, asyncio.Lock())
    async with lock:
        hit = _CACHE.get(url)
        if hit and time.monotonic() - hit[0] < TTL:
            return hit[1]
        headers = {"If-None-Match": hit[2]} if hit and hit[2] else None
        r = await client.get(url, headers=headers)
        if r.status_code == 304 and hit is not None:
            _CACHE[url] = (time.monotonic(), hit[1], hit[2])
            return hit[1]
        r.raise_for_status()
        etag = r.headers.get("etag")
        _CACHE[url] = (time.monotonic(), r.text, etag)
        return r.text


def _make_uri(url: str) -> str:
    marker = "docs.wavix.com/"
    if marker in url:
        path = url.split(marker, 1)[1]
        if path.endswith(".md"):
            path = path[:-3]
        return f"wavix://docs/{path}"
    # Fallback for unexpected non-docs URLs
    return f"wavix://docs/external/{url.split('://', 1)[-1]}"


def _register_one(mcp: FastMCP, client: httpx.AsyncClient, title: str, url: str, desc: str) -> None:
    uri = _make_uri(url)

    @mcp.resource(uri=uri, name=title, description=desc, mime_type="text/markdown")
    async def fetch() -> str:
        return await _cached_get(client, url)


def register_docs(mcp: FastMCP, client: httpx.AsyncClient) -> int:
    response = httpx.get(LLMS_URL, timeout=30)
    response.raise_for_status()

    count = 0
    for line in response.text.splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        url = m["url"]
        if "/api-reference/" in url:
            continue
        desc = m["desc"][:DESC_MAX]
        _register_one(mcp, client, m["title"], url, desc)
        count += 1

    logger.info("Registered %d docs as resources", count)
    return count


def register_api_spec(mcp: FastMCP, client: httpx.AsyncClient) -> None:
    """Expose the Wavix OpenAPI YAML spec as an MCP Resource."""

    @mcp.resource(
        uri="wavix://api/openapi.yaml",
        name="Wavix API OpenAPI Specification",
        description="Complete OpenAPI 3.0 spec — endpoints, request/response schemas, auth.",
        mime_type="application/x-yaml",
    )
    async def fetch_spec() -> str:
        return await _cached_get(client, OPENAPI_SPEC_URL)
