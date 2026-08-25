import logging
import os

from fastmcp.server.auth.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

logger = logging.getLogger(__name__)

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

    logger.info("OAuth enabled: issuer=%s audience=%s", issuer, audience)

    return RemoteAuthProvider(
        token_verifier=JWTVerifier(
            jwks_uri=f"{issuer}/.well-known/jwks.json",
            issuer=issuer,
            audience=audience,
        ),
        authorization_servers=[AnyHttpUrl(issuer)],
        base_url=resource,
        scopes_supported=SCOPES_SUPPORTED,
        resource_name="Wavix API",
        resource_documentation=AnyHttpUrl("https://docs.wavix.com/api-reference"),
    )
