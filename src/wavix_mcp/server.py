import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import jsonref
import yaml
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.providers.openapi import MCPType, RouteMap

from .docs import register_api_spec, register_docs

logger = logging.getLogger(__name__)

OPENAPI_URL = "https://wavix.github.io/wavix-openapi/wavix-api.yaml"

_CC_TRANSLATE = dict.fromkeys(
    cp for cp in [*range(0x00, 0x20), *range(0x7F, 0xA0)] if cp not in (0x09, 0x0A, 0x0D)
)

DEFAULT_API_BASE_URL = "https://api.wavix.com"

BINARY_STREAM_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("/v1/recordings/{call_uuid}", "get"),
    ("/v1/billing/invoices/{id}", "get"),
    ("/v1/speech-analytics/{uuid}/file", "get"),
    ("/v3/10dlc/brands/{brand_id}/evidence/{uuid}", "get"),
)

INSTRUCTIONS = """\
The Wavix MCP Server provides tools and documentation for the Wavix telecom platform (SMS/MMS, voice calls, 2FA, SIP trunking, phone numbers, speech analytics).

Documentation guidance:
  1. Before answering conceptual questions about Wavix features (how a feature works, what parameters mean, billing, limits, integration patterns), read the relevant documentation resource under wavix://docs/* first. Do not rely on prior assumptions about telecom APIs - Wavix-specific behavior may differ.
  2. To discover available documentation, list MCP resources and match by URI/title (e.g. wavix://docs/numbers/number-validator, wavix://docs/messaging/send-sms). The full OpenAPI specification is available at wavix://api/openapi.yaml.
  3. Quote behavior from documentation rather than inferring it.

Tool selection guidance:
  1. Use '*_list' tools for paginated retrieval with basic filters (e.g. my_numbers_list, sms_and_mms_messages_list).
  2. Use '*_get' tools for fetching a single entity by ID.
  3. Use '*_create' / '*_update' / '*_delete' tools for mutations - confirm with the user before destructive actions (delete, return-to-stock, cancel).

Context management:
  1. Default to small page sizes (per_page=10-25) unless the user asks for more.
  2. For bulk validation or large lists, prefer asynchronous operations where available (e.g. number_validator_create_bulk with async=true) and poll for results.

Authentication:
  1. The MCP client forwards the user's Wavix API key as a Bearer token. Do not ask the user for credentials - tools authenticate automatically.
"""


def _clean_yaml_text(text: str) -> str:
    # Remove non-printable control chars that occasionally appear in exported specs.
    return text.translate(_CC_TRANSLATE)


def load_spec() -> dict[str, Any]:
    if OPENAPI_URL.startswith("http://") or OPENAPI_URL.startswith("https://"):
        response = httpx.get(
            OPENAPI_URL,
            timeout=60,
            headers={
                # Force revalidation on every process start to avoid stale intermediary caches.
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        response.raise_for_status()
        raw = response.text
    else:
        with open(OPENAPI_URL, encoding="utf-8") as f:
            raw = f.read()
    spec = yaml.safe_load(_clean_yaml_text(raw))
    # Resolve $ref / allOf so FastMCP can flatten body schemas into named tool params.
    return jsonref.replace_refs(spec, proxies=False, lazy_load=False)


_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


def _is_json_content_type(content_type: str) -> bool:
    """True for ``application/json``, ``text/json``, or ``application/...+json`` (RFC 6838)."""
    ct = content_type.lower().split(";", 1)[0].strip()
    return ct in {"application/json", "text/json"} or ct.endswith("+json")


def _ensure_request_body_type_object(spec: dict[str, Any]) -> dict[str, Any]:
    """Add ``type: 'object'`` to JSON request body schemas with top-level ``allOf``.

    FastMCP merges ``allOf`` children into the body schema but does not set
    ``type``. Without it, ``RequestDirector`` strips single-property wrappers
    (e.g. ``{voice_campaign: {...}}``) from the HTTP body.
    """
    for path_item in (spec.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for method in ("post", "put", "patch"):
            op = path_item.get(method)
            content = ((op or {}).get("requestBody") or {}).get("content") or {}
            for ct, mt in content.items():
                if not _is_json_content_type(ct) or not isinstance(mt, dict):
                    continue
                sch = mt.get("schema")
                if isinstance(sch, dict) and "allOf" in sch and "type" not in sch:
                    sch["type"] = "object"
    return spec


def _strip_non_json_response_content(spec: dict[str, Any]) -> dict[str, Any]:
    """Drop non-JSON content types from response schemas in-place.

    FastMCP's ``extract_output_schema_from_responses`` falls back to the first
    available content type when no JSON one is found, then validates real
    responses against it. For streaming (``application/x-ndjson``) and binary
    (``audio/*``, ``application/octet-stream``) endpoints the structured-output
    contract doesn't fit, causing validation errors. Removing non-JSON entries
    leaves the tool callable but unvalidated.
    """
    for path_item in (spec.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            for response in (op.get("responses") or {}).values():
                if not isinstance(response, dict):
                    continue
                content = response.get("content")
                if not isinstance(content, dict):
                    continue
                for ct in [c for c in content if not _is_json_content_type(c)]:
                    del content[ct]
                if not content:
                    del response["content"]
    return spec


def _build_exclude_route_maps(
    targets: tuple[tuple[str, str], ...],
) -> list[RouteMap]:
    """Anchor the regex (``^…$``) and keep ``{param}`` braces literal so the
    pattern matches the exact path string FastMCP routes against.
    """
    route_maps: list[RouteMap] = []
    for path, method in targets:
        # re.escape turns "{call_uuid}" → "\\{call_uuid\\}"; we want literal braces.
        escaped = re.escape(path).replace("\\{", "{").replace("\\}", "}")
        pattern = f"^{escaped}$"
        route_maps.append(
            RouteMap(
                methods=[method.upper()],  # type: ignore[list-item]
                pattern=pattern,
                mcp_type=MCPType.EXCLUDE,
            )
        )
    return route_maps


async def _resolve_to_download_url(
    api_client: httpx.AsyncClient,
    base_url: str,
    path: str,
) -> dict[str, Any]:
    """Contract:
    - **3xx** – ``Location`` header → ``download_url`` (pre-signed, no auth).
    - **2xx** – synthetic ``{base_url}{path}`` → ``download_url`` (caller re-fetches with Bearer).
    - **other** – ``{error, status_code, body}``; binary bodies become ``"<binary body omitted>"``.
    """
    request = api_client.build_request("GET", path)
    response = await api_client.send(request, follow_redirects=False)
    code = response.status_code

    if code in (301, 302, 303, 307, 308):
        location = response.headers.get("Location") or response.headers.get("location")
        return {
            "download_url": location,
            "content_type": response.headers.get("Content-Type"),
            "status_code": code,
            "note": "Pre-signed URL — fetch directly without auth.",
        }

    if 200 <= code < 300:
        return {
            "download_url": f"{base_url}{path}",
            "content_type": response.headers.get("Content-Type"),
            "content_length": response.headers.get("Content-Length"),
            "status_code": code,
            "note": "Authenticated URL — re-fetch with the same Bearer token.",
        }

    try:
        body: str = response.content.decode("utf-8", errors="strict")[:500]
    except UnicodeDecodeError:
        body = "<binary body omitted>"
    return {
        "error": f"HTTP {code}",
        "status_code": code,
        "body": body,
    }


def _register_binary_redirect_tools(
    mcp: FastMCP,
    api_client: httpx.AsyncClient,
    base_url: str,
) -> None:
    @mcp.tool(name="call_recording_get")
    async def call_recording_get(call_uuid: str) -> dict[str, Any]:
        """Get a download URL for a call recording audio file.

        Returns ``{download_url, content_type, status_code, note}`` instead of the
        binary audio stream. Fetch ``download_url`` to obtain the MP3.
        """
        return await _resolve_to_download_url(api_client, base_url, f"/v1/recordings/{call_uuid}")

    @mcp.tool(name="billing_invoices_download")
    async def billing_invoices_download(id: int) -> dict[str, Any]:
        """Get a download URL for a billing invoice PDF.

        Returns ``{download_url, content_type, status_code, note}`` instead of the
        binary PDF stream. Fetch ``download_url`` to obtain the file.
        """
        return await _resolve_to_download_url(api_client, base_url, f"/v1/billing/invoices/{id}")

    @mcp.tool(name="speech_analytics_file_get")
    async def speech_analytics_file_get(uuid: str) -> dict[str, Any]:
        """Get a download URL for a speech-analytics audio file.

        Returns ``{download_url, content_type, status_code, note}`` instead of the
        binary audio stream (WAV/MP3/MP4). Fetch ``download_url`` to obtain the file.
        """
        return await _resolve_to_download_url(
            api_client, base_url, f"/v1/speech-analytics/{uuid}/file"
        )

    @mcp.tool(name="ten_dlc_brand_evidence_get")
    async def ten_dlc_brand_evidence_get(brand_id: str, uuid: str) -> dict[str, Any]:
        """Get a download URL for a 10DLC brand evidence file.

        Returns ``{download_url, content_type, status_code, note}`` instead of the
        binary file stream. Fetch ``download_url`` to obtain the file.
        """
        return await _resolve_to_download_url(
            api_client, base_url, f"/v3/10dlc/brands/{brand_id}/evidence/{uuid}"
        )


class MCPHeaderAuth(httpx.Auth):
    """Forwards the incoming MCP HTTP `Authorization` header to the configured Wavix host only.

    The host check prevents the bearer token from leaking to redirect targets
    (e.g. presigned S3 URLs reached via ``follow_redirects=True``).
    """

    def __init__(self, allowed_host: str):
        self._allowed_host = allowed_host

    def auth_flow(self, request: httpx.Request):
        if request.url.host != self._allowed_host:
            yield request
            return
        try:
            headers = get_http_headers(include={"authorization"})
            auth = headers.get("authorization")
            if auth:
                request.headers["Authorization"] = auth
        except (RuntimeError, LookupError):
            # Called outside an MCP request context (e.g. during initial
            # httpx connect). Forward the request without auth — the upstream
            # API will reject with 401 if needed.
            pass
        except Exception:
            logger.exception("Failed to forward Authorization header to Wavix API")
            raise
        yield request


def build_server() -> FastMCP:
    container_env = Path("/app/.env")
    if container_env.exists():
        load_dotenv(container_env)
    else:
        load_dotenv()
    spec = load_spec()
    spec = _ensure_request_body_type_object(spec)
    spec = _strip_non_json_response_content(spec)
    base_url = os.getenv("WAVIX_API_BASE_URL", "").strip() or DEFAULT_API_BASE_URL

    info = spec.get("info", {})
    name = f"Wavix API MCP ({info.get('version', 'unknown')})"

    logger.info("Starting MCP with base_url=%s", base_url)

    docs_client = httpx.AsyncClient(timeout=30, follow_redirects=True)
    api_client = httpx.AsyncClient(
        base_url=base_url,
        auth=MCPHeaderAuth(allowed_host=httpx.URL(base_url).host),
        timeout=60,
        follow_redirects=True,
    )

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await docs_client.aclose()
            await api_client.aclose()

    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=api_client,
        name=name,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        route_maps=_build_exclude_route_maps(BINARY_STREAM_ENDPOINTS),
    )
    _register_binary_redirect_tools(mcp, api_client, base_url)
    register_docs(mcp, docs_client)
    register_api_spec(mcp, docs_client)
    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    build_server().run(transport="http", host="0.0.0.0", port=8000, path="/mcp")


if __name__ == "__main__":
    main()
