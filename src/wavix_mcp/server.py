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
from fastmcp.server.providers.openapi import MCPType, OpenAPITool, RouteMap
from fastmcp.utilities.openapi.models import HTTPRoute
from mcp.types import ToolAnnotations

from .auth import build_auth_provider, mcp_path
from .docs import register_api_spec, register_docs

logger = logging.getLogger(__name__)

OPENAPI_URL = "https://wavix.github.io/wavix-openapi/wavix-api.yaml"

_CC_TRANSLATE = dict.fromkeys(
    cp for cp in [*range(0x00, 0x20), *range(0x7F, 0xA0)] if cp not in (0x09, 0x0A, 0x0D)
)

DEFAULT_API_BASE_URL = "https://api.wavix.com"

BINARY_STREAM_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("/v1/recordings/{call_id}", "get"),
    ("/v1/speech-analytics/{request_id}/file", "get"),
    ("/v3/10dlc/brands/{brand_id}/evidence/{id}", "get"),
)

# Operations dropped from the tool surface entirely, with no replacement tool.
# NDJSON exports (`*_all`) would blow past Anthropic's max tool-response size —
# their JSON-paginated siblings return the same data. Multipart file uploads
# can't be driven through an MCP client. The invoice PDF download is excluded by
# product decision (Anthropic listing prep) rather than surfaced as a URL.
EXCLUDED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("/v1/cdrs/all", "get"),
    ("/v3/messages/all", "get"),
    ("/v1/speech-analytics", "post"),
    ("/v1/numbers/papers", "post"),
    ("/v3/10dlc/brands/{brand_id}/evidence", "post"),
    ("/v1/billing/invoices/{id}", "get"),
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


_NULLABLE_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "array"})


def _iter_subschemas(schema: dict[str, Any]) -> Any:
    props = schema.get("properties")
    if isinstance(props, dict):
        yield from (v for v in props.values() if isinstance(v, dict))
    items = schema.get("items")
    if isinstance(items, dict):
        yield items
    elif isinstance(items, list):
        yield from (v for v in items if isinstance(v, dict))
    for keyword in ("allOf", "anyOf", "oneOf"):
        members = schema.get(keyword)
        if isinstance(members, list):
            yield from (v for v in members if isinstance(v, dict))
    extra = schema.get("additionalProperties")
    if isinstance(extra, dict):
        yield extra


def _widen_scalar_to_nullable(schema: dict[str, Any]) -> None:
    kind = schema.get("type")
    if isinstance(kind, str):
        if kind not in _NULLABLE_SCALAR_TYPES:
            return
        schema["type"] = [kind, "null"]
    elif isinstance(kind, list):
        if "null" in kind or not any(k in _NULLABLE_SCALAR_TYPES for k in kind):
            return
        schema["type"] = [*kind, "null"]
    else:
        return
    enum = schema.get("enum")
    if isinstance(enum, list) and None not in enum:
        schema["enum"] = [*enum, None]


def _widen_response_tree(schema: dict[str, Any], seen: set[int]) -> None:
    marker = id(schema)
    if marker in seen:
        return
    seen.add(marker)
    _widen_scalar_to_nullable(schema)
    for sub in _iter_subschemas(schema):
        _widen_response_tree(sub, seen)


def _relax_response_nullability(spec: dict[str, Any]) -> dict[str, Any]:
    """Allow ``null`` in every scalar/array field of a JSON response schema.

    The spec types many response fields as a bare ``string``/``integer``/… that the
    live API can still return ``null`` for (e.g. an unset SIP-trunk ``label``). An
    MCP client validates a tool's structured output against the advertised schema
    and hard-fails the whole call on the first mismatch, so one stray null takes a
    tool down entirely. ``object`` types are left strict so a top-level response
    stays ``type: object`` and FastMCP does not wrap it in ``{"result": …}``.
    """
    for path_item in (spec.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            for response in (op.get("responses") or {}).values():
                content = (response or {}).get("content")
                if not isinstance(content, dict):
                    continue
                for ct, mt in content.items():
                    if not _is_json_content_type(ct) or not isinstance(mt, dict):
                        continue
                    root = mt.get("schema")
                    if isinstance(root, dict):
                        _widen_response_tree(root, set())
    return spec


def _missing_targets(
    spec: dict[str, Any],
    targets: tuple[tuple[str, str], ...],
) -> list[tuple[str, str]]:
    """Configured (path, method) pairs absent from the spec.

    A silent mismatch here means an EXCLUDE route map matches nothing and a
    binary/NDJSON endpoint leaks back into the tool surface, so callers log it.
    """
    paths = spec.get("paths") or {}
    return [
        (path, method)
        for path, method in targets
        if not isinstance(paths.get(path), dict) or method not in paths[path]
    ]


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


def _xmcp(route: HTTPRoute) -> dict[str, Any] | None:
    """The operation's ``x-mcp`` vendor extension, or ``None`` when absent.

    FastMCP does not interpret ``x-mcp`` — it only surfaces raw ``x-*`` keys on
    ``route.extensions``. This is the single accessor the two hooks below share,
    so a spec without ``x-mcp`` is a silent no-op.
    """
    ext = route.extensions.get("x-mcp")
    return ext if isinstance(ext, dict) else None


def _xmcp_route_map_fn(route: HTTPRoute, mcp_type: MCPType) -> MCPType | None:
    """Exclude operations the spec marks ``x-mcp.expose: false``. Only an
    explicit ``false`` excludes; a missing ``expose`` stays exposed. Returning
    ``None`` leaves FastMCP's default typing untouched.
    """
    xmcp = _xmcp(route)
    if xmcp is not None and xmcp.get("expose") is False:
        return MCPType.EXCLUDE
    return None


_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DESTRUCTIVE_METHODS = frozenset({"PUT", "PATCH", "DELETE"})


def _hint(declared: object, derived: bool) -> bool:
    """The spec's own answer when it gives one, else the method-derived default."""
    return declared if isinstance(declared, bool) else derived


def _download_url_annotations(title: str) -> ToolAnnotations:
    """For the hand-registered tools: they resolve a download URL and change
    nothing. They have no OpenAPI route, so _xmcp_component_fn never sees them.
    """
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=True,
    )


def _xmcp_component_fn(route: HTTPRoute, component: object) -> None:
    """Map the operation onto the generated tool: human title from the OpenAPI
    ``summary`` (FastMCP does not derive one), risk annotations from ``x-mcp``.

    The tool description is left as FastMCP set it — the operation's own
    ``description`` — rather than duplicated into ``x-mcp``. ``idempotentHint``
    is left unset: the spec does not carry it, and inferring it from the HTTP
    method would assert a contract the spec never made.
    """
    if not isinstance(component, OpenAPITool):
        return
    if route.summary:
        component.title = route.summary

    # x-mcp is the explicit answer, the HTTP method the fallback. No operation
    # in the published spec carries x-mcp yet (checked: 0 of 122), so without a
    # fallback every hint would be None — and Anthropic's review rejects a tool
    # that declares neither: "Every tool must include a `title` and the
    # applicable hint — `readOnlyHint: true` for read-only tools,
    # `destructiveHint: true` for tools that modify or delete data."
    # https://claude.com/docs/connectors/building/review-criteria
    #
    # Derivation follows MCP's own definition of destructive — overwriting or
    # removing existing state — so POST, which creates, is additive.
    xmcp = _xmcp(route) or {}
    method = (route.method or "").upper()

    component.annotations = ToolAnnotations(
        title=route.summary or None,
        readOnlyHint=_hint(xmcp.get("readOnly"), method in _READ_ONLY_METHODS),
        destructiveHint=_hint(xmcp.get("destructive"), method in _DESTRUCTIVE_METHODS),
        # Every tool reaches the live Wavix API, never a closed local set.
        openWorldHint=_hint(xmcp.get("openWorld"), True),
    )


async def _resolve_to_download_url(
    api_client: httpx.AsyncClient,
    base_url: str,
    path: str,
) -> dict[str, Any]:
    """Contract:
    - **``Location`` on a 2xx/3xx** – its value → ``download_url`` (pre-signed, no auth).
      Checked before the 2xx branch because the recording endpoint sends it on a 200;
      a ``Location`` on a 4xx/5xx is not a download and falls through to the error case.
    - **other 2xx** – synthetic ``{base_url}{path}`` → ``download_url`` (caller re-fetches with Bearer).
    - **other** – ``{error, status_code, body}``; binary bodies become ``"<binary body omitted>"``.
    """
    request = api_client.build_request("GET", path)
    response = await api_client.send(request, follow_redirects=False)
    code = response.status_code

    location = response.headers.get("Location") or response.headers.get("location")
    if location and 200 <= code < 400:
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
    @mcp.tool(
        name="call_recording_get_by_call",
        title="Get call recording download URL",
        annotations=_download_url_annotations("Get call recording download URL"),
    )
    async def call_recording_get_by_call(call_id: str) -> dict[str, Any]:
        """Get a download URL for a call recording audio file by call ID.

        Returns ``{download_url, content_type, status_code, note}`` instead of the
        binary audio stream. Fetch ``download_url`` to obtain the MP3.
        """
        return await _resolve_to_download_url(api_client, base_url, f"/v1/recordings/{call_id}")

    @mcp.tool(
        name="speech_analytics_file_get",
        title="Get speech analytics file URL",
        annotations=_download_url_annotations("Get speech analytics file URL"),
    )
    async def speech_analytics_file_get(request_id: str) -> dict[str, Any]:
        """Get a download URL for a speech-analytics audio file.

        Returns ``{download_url, content_type, status_code, note}`` instead of the
        binary audio stream (WAV/MP3/MP4). Fetch ``download_url`` to obtain the file.
        """
        return await _resolve_to_download_url(
            api_client, base_url, f"/v1/speech-analytics/{request_id}/file"
        )

    @mcp.tool(
        name="ten_dlc_brand_evidence_get",
        title="Get 10DLC brand evidence URL",
        annotations=_download_url_annotations("Get 10DLC brand evidence URL"),
    )
    async def ten_dlc_brand_evidence_get(brand_id: str, id: str) -> dict[str, Any]:
        """Get a download URL for a 10DLC brand evidence file.

        Returns ``{download_url, content_type, status_code, note}`` instead of the
        binary file stream. Fetch ``download_url`` to obtain the file.
        """
        return await _resolve_to_download_url(
            api_client, base_url, f"/v3/10dlc/brands/{brand_id}/evidence/{id}"
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
    spec = _relax_response_nullability(spec)
    base_url = os.getenv("WAVIX_API_BASE_URL", "").strip() or DEFAULT_API_BASE_URL

    info = spec.get("info", {})
    name = f"Wavix API MCP ({info.get('version', 'unknown')})"

    logger.info("Starting MCP with base_url=%s", base_url)

    excluded = BINARY_STREAM_ENDPOINTS + EXCLUDED_ENDPOINTS
    missing = _missing_targets(spec, excluded)
    if missing:
        logger.warning(
            "Route-map exclude targets not found in spec (stale after a spec change?): %s",
            ", ".join(f"{m.upper()} {p}" for p, m in missing),
        )

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
        auth=build_auth_provider(),
        route_maps=_build_exclude_route_maps(excluded),
        route_map_fn=_xmcp_route_map_fn,
        mcp_component_fn=_xmcp_component_fn,
    )
    _register_binary_redirect_tools(mcp, api_client, base_url)
    register_docs(mcp, docs_client)
    register_api_spec(mcp, docs_client)
    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    build_server().run(transport="http", host="0.0.0.0", port=8000, path=mcp_path())


if __name__ == "__main__":
    main()
