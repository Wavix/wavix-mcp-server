"""Per-request scope decision, mirrored from the Wavix API gateway.

The tools this server exposes are the Wavix public API, and the gateway decides
per request whether a granted OAuth scope set may reach a given path+method
(mono: services/auth-service .../auth-public/auth-public-scopes.service.ts,
AuthPublicScopesService, plus the OAuth path in auth-public.service.ts that
builds a synthetic scoped key from the grant). This module reproduces that
decision so the server can hide a tool the gateway would reject for the
connecting token instead of listing it and letting the call fail with 403.

The path rules and levels below are copied from that source and must be kept in
step with it — a divergence makes a tool visible-but-denied or hidden-but-usable.
"""

from dataclasses import dataclass

RequiredLevel = str  # "read" | "write"

_LEVEL_ORDER = {"none": 0, "read": 1, "write": 2, "admin": 3}

_DEFAULT_LEVEL_BY_METHOD = {
    "GET": "read",
    "POST": "write",
    "PUT": "write",
    "PATCH": "write",
    "DELETE": "write",
}


@dataclass(frozen=True)
class _MethodOverride:
    path: str
    method: str
    level: RequiredLevel


@dataclass(frozen=True)
class _ScopeRule:
    scope: str
    starts_with: tuple[str, ...]
    except_starts_with: tuple[str, ...] = ()
    method_overrides: tuple[_MethodOverride, ...] = ()


_SCOPE_BY_PATH_RULES: tuple[_ScopeRule, ...] = (
    _ScopeRule("numbers", ("/v1/numbers", "/v1/mydids", "/v1/buy")),
    _ScopeRule("trunks", ("/v1/trunks",)),
    _ScopeRule(
        "calls",
        ("/v1/cdr", "/v1/speech-analytics", "/v1/calls"),
        except_starts_with=("/v1/calls/webhooks",),
        method_overrides=(
            _MethodOverride("/v1/cdr", "POST", "read"),
            _MethodOverride("/v1/cdrs", "POST", "read"),
        ),
    ),
    _ScopeRule("recordings", ("/v1/recordings",)),
    _ScopeRule(
        "campaigns",
        ("/v1/voice-campaigns", "/v3/10dlc/brands", "/v1/short-links", "/v3/10dlc/subscriptions"),
    ),
    _ScopeRule("billing", ("/v1/billing",)),
    _ScopeRule("account", ("/v1/profile",)),
    _ScopeRule("subaccounts", ("/v1/sub-organizations",)),
    _ScopeRule("messages", ("/v2/messages", "/v3/messages")),
    _ScopeRule("two_fa", ("/v1/two-fa",)),
    _ScopeRule("validator", ("/v1/validation",)),
    _ScopeRule("webhooks", ("/v1/calls/webhooks",)),
    _ScopeRule("embeddable", ("/v2/webrtc/tokens",)),
)

_SCOPE_EXEMPT_PATH_PREFIXES = ("/public/v1/support-portal", "/site/v1.1/support-portal")


@dataclass(frozen=True)
class Requirement:
    """Outcome of classifying one tool's path+method against the scope rules.

    ``exempt`` — no scope gates it, always available. ``needs`` — available only
    when the token grants ``(group, level)``. ``never`` — no rule matches, which
    the gateway treats as denied, so the tool is unusable over OAuth.
    """

    kind: str  # "exempt" | "needs" | "never"
    group: str | None = None
    level: RequiredLevel | None = None


EXEMPT = Requirement("exempt")
NEVER = Requirement("never")


def _normalize_path(path: str) -> str:
    trimmed = (path or "").split("?", 1)[0].strip()
    if not trimmed:
        return ""
    with_slash = trimmed if trimmed.startswith("/") else f"/{trimmed}"
    while "//" in with_slash:
        with_slash = with_slash.replace("//", "/")
    return with_slash


def _is_exempt(path: str) -> bool:
    return any(
        path == prefix or path.startswith(f"{prefix}/") for prefix in _SCOPE_EXEMPT_PATH_PREFIXES
    )


def _find_rule(path: str) -> _ScopeRule | None:
    for rule in _SCOPE_BY_PATH_RULES:
        if not any(path.startswith(prefix) for prefix in rule.starts_with):
            continue
        if any(path.startswith(prefix) for prefix in rule.except_starts_with):
            continue
        return rule
    return None


def _required_level(rule: _ScopeRule, path: str, method: str) -> RequiredLevel | None:
    for override in rule.method_overrides:
        if override.path == path and override.method == method:
            return override.level
    return _DEFAULT_LEVEL_BY_METHOD.get(method)


def requirement_for(method: str, path: str) -> Requirement:
    normalized = _normalize_path(path)
    if not normalized or _is_exempt(normalized):
        return EXEMPT

    rule = _find_rule(normalized)
    if rule is None:
        return NEVER

    level = _required_level(rule, normalized, (method or "").upper())
    if level is None:
        return NEVER

    return Requirement("needs", rule.scope, level)


def _granted_levels(scopes: set[str]) -> dict[str, int]:
    levels: dict[str, int] = {}
    for scope in scopes:
        group, _, level = scope.partition(":")
        if level not in ("read", "write"):
            continue
        rank = _LEVEL_ORDER[level]
        if rank > levels.get(group, 0):
            levels[group] = rank
    return levels


def is_allowed(scopes: set[str], requirement: Requirement) -> bool:
    if requirement.kind == "exempt":
        return True
    if requirement.kind == "never":
        return False
    have = _granted_levels(scopes).get(requirement.group or "", 0)
    return have >= _LEVEL_ORDER[requirement.level or "none"]
