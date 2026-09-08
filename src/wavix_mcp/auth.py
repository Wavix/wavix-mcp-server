import json
import logging
import os
from urllib.parse import urlparse

from fastmcp.server.auth.auth import AccessToken, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Marks an access token that was not verified as an OAuth JWT but let through
# for the upstream API to judge. `scope_filter` keys off this to keep such a
# connection out of OAuth scope gating.
PASSTHROUGH_CLIENT_ID = "wavix-api-key-passthrough"

_FALSEY = frozenset({"0", "false", "no", "off"})

SCOPE_GROUPS = (
    "account",
    "billing",
    "calls",
    "campaigns",
    "embeddable",
    "messages",
    "numbers",
    "recordings",
    "subaccounts",
    "trunks",
    "two_fa",
    "validator",
    "webhooks",
)

SCOPES_SUPPORTED = [
    *(f"{group}:{access}" for group in SCOPE_GROUPS for access in ("read", "write")),
    "offline_access",
]


def mcp_path() -> str:
    path = os.getenv("MCP_PATH", "/mcp").strip()
    return path if path.startswith("/") else f"/{path}"


def _issuer_without_synthetic_slash(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path == "/" and not parsed.query and not parsed.fragment:
        return url.rstrip("/")
    return url


def _rewrite_authorization_servers(raw: bytes) -> bytes:
    if not raw:
        return raw
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    servers = data.get("authorization_servers")
    if not isinstance(servers, list):
        return raw
    fixed = [_issuer_without_synthetic_slash(s) if isinstance(s, str) else s for s in servers]
    if fixed == servers:
        return raw
    data["authorization_servers"] = fixed
    return json.dumps(data).encode()


def _strip_issuer_slash_middleware(app: ASGIApp) -> ASGIApp:
    """Drop pydantic's synthetic root-path slash from the ``authorization_servers``
    entries in the served protected-resource metadata.

    ``AnyHttpUrl`` normalizes a host-only issuer to ``https://host/``, but RFC 8414
    §3.3 requires this value to byte-match the ``issuer`` the authorization server
    returns (host-only, no slash) — otherwise the client rejects discovery. Rewrites
    the response body because the value is already frozen into a pydantic model by
    the time FastMCP builds the route.
    """

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        start: Message = {}
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            raw = b"".join(chunks)
            new = _rewrite_authorization_servers(raw)
            if new == raw:
                await send(start)
                await send({"type": "http.response.body", "body": raw})
                return
            headers = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(new)).encode()))
            await send(
                {"type": "http.response.start", "status": start["status"], "headers": headers}
            )
            await send({"type": "http.response.body", "body": new})

        await app(scope, receive, capture)

    return wrapped


def api_key_passthrough_enabled() -> bool:
    return os.getenv("OAUTH_API_KEY_PASSTHROUGH", "true").strip().lower() not in _FALSEY


class _ApiKeyPassthroughVerifier(TokenVerifier):
    """Verify OAuth JWTs; hand any other bearer to the upstream API unchanged.

    Turning OAuth on for a deployment whose clients authenticate with a Wavix
    API key would otherwise reject every one of them: ``JWTVerifier`` cannot
    validate an opaque key, and FastMCP turns that ``None`` verdict into a 401.
    This wrapper keeps the OAuth path strict — a JWT is still checked against
    the issuer's JWKS, ``iss`` and ``aud`` — while a token that is not an OAuth
    JWT proceeds unverified here so ``MCPHeaderAuth`` forwards it upstream,
    leaving the Wavix API the sole authority on it, exactly as before OAuth
    existed.

    A request carrying **no** token still gets the RFC 9728 challenge, which is
    what lets a new client discover the authorization server. Set
    ``OAUTH_API_KEY_PASSTHROUGH=false`` to drop the fallback once API-key access
    to MCP is retired.
    """

    def __init__(self, jwt_verifier: TokenVerifier) -> None:
        super().__init__(required_scopes=jwt_verifier.required_scopes)
        self._jwt_verifier = jwt_verifier

    async def verify_token(self, token: str) -> AccessToken | None:
        verified = await self._jwt_verifier.verify_token(token)
        if verified is not None:
            return verified

        logger.debug("Bearer token is not a valid OAuth JWT; forwarding it upstream")
        return AccessToken(token=token, client_id=PASSTHROUGH_CLIENT_ID, scopes=[])


class _RFC8414RemoteAuthProvider(RemoteAuthProvider):
    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        for route in routes:
            if isinstance(route, Route) and "oauth-protected-resource" in route.path:
                route.app = _strip_issuer_slash_middleware(route.app)
        return routes


def build_auth_provider() -> RemoteAuthProvider | None:
    issuer = os.getenv("OAUTH_ISSUER", "").strip().rstrip("/")
    resource = os.getenv("OAUTH_RESOURCE", "").strip().rstrip("/")

    if not issuer or not resource:
        logger.warning(
            "OAuth disabled: OAUTH_ISSUER and OAUTH_RESOURCE must both be set. "
            "The server will forward whatever bearer token the client supplies."
        )
        return None

    # RFC 8707: the audience the authorization server stamps into `aud` is the
    # full resource URI including the MCP path, which is also what the protected
    # resource metadata advertises as `resource`. These three must agree exactly
    # or the authorization request is rejected as invalid_target.
    audience = f"{resource}{mcp_path()}"

    passthrough = api_key_passthrough_enabled()
    logger.info(
        "OAuth enabled: issuer=%s audience=%s api_key_passthrough=%s",
        issuer,
        audience,
        passthrough,
    )

    verifier: TokenVerifier = JWTVerifier(
        jwks_uri=f"{issuer}/.well-known/jwks.json",
        issuer=issuer,
        audience=audience,
    )
    if passthrough:
        verifier = _ApiKeyPassthroughVerifier(verifier)

    return _RFC8414RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(issuer)],
        base_url=resource,
        scopes_supported=SCOPES_SUPPORTED,
        resource_name="Wavix API",
        resource_documentation=AnyHttpUrl("https://docs.wavix.com/api-reference"),
    )
