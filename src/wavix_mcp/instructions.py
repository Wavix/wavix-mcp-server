"""Server instructions assembled from a fixed core and one fragment per toolset."""

from collections.abc import Iterable
from dataclasses import dataclass

from .confirmation import TOKEN_TTL_S

CORE = f"""\
The Wavix MCP server exposes the Wavix telecom platform: {{products}}.

Documentation resources:
- Wavix product documentation is available as MCP resources under wavix://docs/*, one resource per topic{{examples}}. It covers Wavix-specific feature behavior, parameter meanings, billing, and limits, which can differ from other telecom APIs.
- The full Wavix OpenAPI specification is available as the resource wavix://api/openapi.yaml.

Tool naming conventions:
- *_list tools return a collection with basic filters. Paginated collections accept per_page (1 to 100, default 25) and page (starting at 1); each tool's input schema shows whether it is paginated.
- *_get tools return a single entity, identified by the key named in the tool's input schema (usually an ID).
- *_create, *_update, and *_delete tools change account state. Each tool carries MCP annotations (readOnlyHint, destructiveHint, openWorldHint), and its description states any cost and whether the change can be reversed.
- Every tool annotated with destructiveHint runs in two steps{{gated}}. The first call does nothing and returns a preview of the action with a confirmation_token; the action runs when the same tool is called again with the same arguments and that token. The preview is meant for the user to read and approve; the confirmed call is meant to follow an approval the user gives in a message sent after the preview, and an approval given in advance is not an approval of that preview. The server checks only that the token matches the arguments and has not expired; it does not see the conversation. A token expires after {TOKEN_TTL_S / 60:g} minutes; repeating the confirmed call with the same arguments and token within that time returns the stored result and does not act a second time.{{naming}}

{{sections}}Authentication:
- Tools authenticate with the credential the MCP client supplies: an OAuth access token from Wavix account sign-in, or a Wavix API key sent as a Bearer token. With account sign-in, the available tools are limited to the scopes granted at consent.
"""


@dataclass(frozen=True)
class Fragment:
    phrase: str
    billing: tuple[str, ...] = ()
    rules: tuple[tuple[str, str], ...] = ()
    naming: tuple[str, ...] = ()
    docs_examples: tuple[str, ...] = ()
    gated: tuple[str, ...] = ()
    gated_changes: tuple[str, ...] = ()


FRAGMENTS: dict[str, Fragment] = {
    "numbers": Fragment(
        phrase="phone numbers",
        billing=("number purchases",),
    ),
    "calls": Fragment(
        phrase="voice calls",
        billing=("calls",),
        rules=(
            (
                "Calls",
                "Whenever a call is automated or handled by AI, the Wavix Terms of Service "
                "require the customer to disclose this at the start of the call.",
            ),
        ),
    ),
    "messaging": Fragment(
        phrase="SMS and MMS, WhatsApp, 10DLC registration",
        billing=("messages",),
        docs_examples=("wavix://docs/messaging/send-sms",),
        gated=(
            "register or change WhatsApp webhooks",
            "submit WhatsApp templates",
            "appeal 10DLC Brand identity verification or external vetting",
        ),
    ),
    "verification": Fragment(
        phrase="2FA",
        billing=("one-time passwords",),
        naming=(
            "Some bulk operations run asynchronously. For example, number_validator_create_bulk "
            "with async set to true returns a request ID, and number_validator_results_get "
            "returns the results for that ID.",
        ),
        docs_examples=("wavix://docs/numbers/number-validator",),
        gated=("validate phone numbers",),
    ),
    "voice_config": Fragment(
        phrase="SIP trunks",
        gated=("register call webhooks",),
        gated_changes=("SIP trunks",),
    ),
    "voice_history": Fragment(
        phrase="call detail records, recordings and speech analytics",
    ),
    "account": Fragment(
        phrase="account, billing and API key management",
        gated_changes=("sub-accounts",),
    ),
}

ORDER = (
    "numbers",
    "calls",
    "voice_config",
    "messaging",
    "verification",
    "voice_history",
    "account",
)


def _join(items: Iterable[str], word: str) -> str:
    parts = list(items)
    if len(parts) <= 2:
        return f" {word} ".join(parts)
    return ", ".join(parts[:-1]) + f", {word} {parts[-1]}"


def require_fragments(toolsets: Iterable[str]) -> None:
    missing = sorted(set(toolsets) - FRAGMENTS.keys())
    if missing:
        raise RuntimeError(f"No instruction fragment for toolset(s): {', '.join(missing)}")


def build_instructions(selection: frozenset[str] | None) -> str:
    names = [n for n in ORDER if selection is None or n in selection]
    names += sorted(
        n for n in FRAGMENTS if n not in ORDER and (selection is None or n in selection)
    )
    chosen = [FRAGMENTS[n] for n in names]

    examples = [e for f in chosen for e in f.docs_examples]
    gated = [g for f in chosen for g in f.gated]
    changes = [c for f in chosen for c in f.gated_changes]
    if changes:
        gated.append("change " + _join(changes, "or"))
    billing = [b for f in chosen for b in f.billing]
    sections = []
    if billing:
        items = _join(billing, "and")
        sections.append(
            f"Billing:\n- {items[0].upper()}{items[1:]} are charged to the account's prepaid "
            "Wavix balance.\n\n"
        )
    sections += [f"{heading}:\n- {text}\n\n" for f in chosen for heading, text in f.rules]

    return CORE.format(
        products=_join((f.phrase for f in chosen), "and"),
        examples=f" (for example, {_join(examples, 'and')})" if examples else "",
        gated=", as do the tools that " + _join(gated, "or") if gated else "",
        naming="".join(f"\n- {n}" for f in chosen for n in f.naming),
        sections="".join(sections),
    )
