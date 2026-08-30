"""Composes the transient assistant sent with every call.

Assembles: the system prompt from agent/prompts.py, the greeting, voice and transcriber ids
from config, the five custom tools with their JSON schemas pointed at /vapi/tools, the
built-in transferCall with transferPlan.mode="warm-transfer-say-summary" so a human hears
the context before being bridged in, and artifactPlan.recordingEnabled.

Model, voice and transcriber ids come from config.py and are never hardcoded here.

Three things worth knowing before changing this file:

  * **Vendor ids carry their provider.** ``Settings`` holds one string per slot, and Vapi
    needs a provider next to every id, so each is written ``provider/id`` in ``.env``
    (``VAPI_MODEL=openai/gpt-4o``, ``VAPI_VOICE_ID=11labs/burt``,
    ``VAPI_TRANSCRIBER=deepgram/nova-3``). Splitting here rather than adding three more
    settings keeps every vendor id in one place a person edits, which is the actual point of
    "never hardcoded".
  * **An unset id raises.** A transient assistant missing its model is a call that connects
    and cannot speak. Failing at composition costs nothing; failing on the phone costs a
    carrier's patience and the credits already spent.
  * **The tool schemas are derived from the Pydantic models in tools/model.py**, not
    retyped. Track A owns those models; a field added there appears in the schema Vapi
    validates against without anyone remembering to mirror it.

STATUS: built. OWNER: Track B.
"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from app.agent.prompts import build_greeting, build_runtime_system_prompt, escalation_line
from app.config import Settings
from app.domain import CallContext, CompanyProfile
from app.tools.model import (
    ConfirmPreagreementArgs,
    LookupOrderArgs,
    ProposeQuoteArgs,
    ReportIncidentArgs,
    VerifyCallerArgs,
)

__all__ = [
    "TOOL_ARGUMENT_MODELS",
    "WARM_TRANSFER_PLAN",
    "build_assistant",
    "build_tool_definitions",
    "profile_from_settings",
    "spoken_today",
    "transfer_message",
]


#: What the model is told each tool does. Deliberately flat and unpersuasive: a description
#: that reads like an instruction is a place a prompt-injected transcript can get leverage.
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "propose_quote": (
        "Record a rate the carrier stated on this call. Submits it for evaluation; it never "
        "books, awards or approves anything. Call this every time they name a number, "
        "including when they change one they already gave."
    ),
    "confirm_preagreement": (
        "Record that the carrier answered yes to the complete recap of the exact terms, read "
        "back word for word. Creates a pre-agreement subject to written confirmation, never "
        "a booking."
    ),
    "verify_caller": (
        "Check one operational fact the caller stated against what we hold. Returns only "
        "whether it matches. Never say the expected value first."
    ),
    "lookup_order": (
        "Read operational details of the shipment. Returns nothing until the caller's "
        "identity has been verified on this call."
    ),
    "report_incident": (
        "Record what the caller says has happened, and any new ETA they gave as an explicit "
        "date and clock time. Records the claim; it approves nothing."
    ),
}

#: The five tools, in the order they are offered to the model. Track A owns the argument
#: models; this file only renders them.
TOOL_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    "propose_quote": ProposeQuoteArgs,
    "confirm_preagreement": ConfirmPreagreementArgs,
    "verify_caller": VerifyCallerArgs,
    "lookup_order": LookupOrderArgs,
    "report_incident": ReportIncidentArgs,
}

#: Applied to the destination we return from ``transfer-destination-request``, not to the
#: tool definition. The transferCall tool here carries no destinations on purpose -- that is
#: what makes Vapi ask us where to send the call, which is the only way the destination is
#: decided server-side instead of by the model. A transferPlan can only ride on a
#: destination, so it rides on ours.
WARM_TRANSFER_PLAN: dict[str, Any] = {"mode": "warm-transfer-say-summary"}


#: What the human hears first when a call is bridged to them. Composed here, next to the
#: transfer plan it belongs to, and used by webhook.py when it answers a
#: transfer-destination-request.
def transfer_message(profile: CompanyProfile) -> str:
    return escalation_line(profile)


def _split_vendor_id(value: str, *, key: str) -> tuple[str, str]:
    """Split a ``provider/id`` setting. Raises rather than guessing a provider.

    A wrong provider is not a degraded call, it is a 400 from Vapi after the assistant was
    already composed -- so this fails where a person can read the message.
    """
    provider, separator, identifier = value.partition("/")
    if not separator or not provider.strip() or not identifier.strip():
        raise ValueError(
            f"{key} must be written 'provider/id' (for example 'openai/gpt-4o'); got {value!r}. "
            f"Verify the current id against Vapi's docs and put it in .env -- never in source."
        )
    return provider.strip(), identifier.strip()


def _require(value: str, *, key: str) -> str:
    if not value.strip():
        raise ValueError(
            f"{key} is not set. A transient assistant without it is a call that connects and "
            f"cannot speak; refusing to compose one is cheaper than discovering it on the phone."
        )
    return value.strip()


def _inline_refs(node: object, defs: dict[str, Any]) -> Any:
    """Resolve ``$ref``/``$defs`` into a self-contained schema.

    Nested Pydantic models compile to a ``$ref`` and a ``$defs`` block. Providers vary in
    whether they accept that inside a function schema, and a schema the provider silently
    drops is a tool the model calls with the wrong shape. Inlining removes the question.
    """
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            target = defs[reference.removeprefix("#/$defs/")]
            resolved = _inline_refs(target, defs)
            overrides = {k: _inline_refs(v, defs) for k, v in node.items() if k != "$ref"}
            return {**resolved, **overrides}
        return {key: _inline_refs(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    return node


def _collapse_any_of(node: object) -> Any:
    """Rewrite Pydantic's ``anyOf`` union into a single ``type``.

    ``str | None`` compiles to ``{"anyOf": [{"type": "string"}, {"type": "null"}]}`` with no
    type of its own, and Vapi rejects a property whose ``type`` is absent -- the whole
    assistant is refused with a 400 before the phone rings. A two-branch union with ``null``
    is the only union these argument models produce, so it collapses to the JSON Schema
    array form and keeps the optionality the model needs to omit the field.
    """
    if isinstance(node, dict):
        collapsed = {k: _collapse_any_of(v) for k, v in node.items() if k != "anyOf"}
        branches = node.get("anyOf")
        if isinstance(branches, list):
            types = [b.get("type") for b in branches if isinstance(b, dict)]
            concrete = [t for t in types if t and t != "null"]
            if len(concrete) == 1:
                collapsed["type"] = [concrete[0], "null"] if "null" in types else concrete[0]
                # Constraints live on the non-null branch; lift them so nothing is lost.
                for branch in branches:
                    if isinstance(branch, dict) and branch.get("type") == concrete[0]:
                        for key, value in branch.items():
                            if key != "type":
                                collapsed.setdefault(key, _collapse_any_of(value))
        return collapsed
    if isinstance(node, list):
        return [_collapse_any_of(item) for item in node]
    return node


def _parameters_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    resolved = _inline_refs(schema, defs)
    resolved.pop("title", None)
    resolved = _collapse_any_of(resolved)
    # Vapi refuses a description on the parameters object itself, though it accepts one on
    # every property inside it. The tool's own description carries the same text.
    resolved.pop("description", None)
    return dict(resolved)


def build_tool_definitions(settings: Settings) -> list[dict[str, Any]]:
    """The five custom tools plus transferCall, as Vapi's ``model.tools`` entries.

    Each custom tool carries its own ``server`` block pointing at ``/vapi/tools``: a
    tool-level URL takes precedence over the assistant-level one, which is what keeps the
    mutation surface on a different endpoint from the event stream.
    """
    base_url = _require(settings.public_base_url, key="PUBLIC_BASE_URL").rstrip("/")
    secret = _require(settings.vapi_server_secret, key="VAPI_SERVER_SECRET")
    server = {
        "url": f"{base_url}/vapi/tools",
        "secret": secret,
        "timeoutSeconds": settings.vapi_tool_timeout_seconds,
    }

    enabled_models = {
        name: model
        for name, model in TOOL_ARGUMENT_MODELS.items()
        if settings.recording_enabled or name != "confirm_preagreement"
    }
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": _TOOL_DESCRIPTIONS[name],
                "parameters": _parameters_schema(model),
            },
            "server": dict(server),
        }
        for name, model in enabled_models.items()
    ]

    # Without this the agent has no way to hang up. On the 30 Aug calls it said "I will end
    # the call now", then sat in dead air until the carrier asked "so are you gonna hang
    # up?" and hung up themselves -- thirty seconds of silence on every call, and
    # endedReason "customer-ended-call" on a call we placed. It carries no server block:
    # ending a call decides nothing, so there is nothing for policy to authorize.
    tools.append(
        {
            "type": "endCall",
            "function": {
                "name": "endCall",
                "description": (
                    "Hang up. Use it once you have said goodbye and the other side has "
                    "said goodbye back, or when they say they are hanging up. Never use "
                    "it while a question is open or a transfer is in progress."
                ),
            },
        }
    )

    # No destinations: an empty transferCall is what makes Vapi send
    # transfer-destination-request to our server URL and let us decide -- or refuse.
    tools.append(
        {
            "type": "transferCall",
            "function": {
                "name": "transferCall",
                "description": (
                    "Hand the live call to a person on our team. Use it when the caller asks "
                    "for a human, when something is outside what you may agree to, or when "
                    "you cannot verify who you are speaking with."
                ),
            },
        }
    )
    return tools


def spoken_today(now: datetime, timezone: str) -> str:
    """Today's date the way the prompt needs it: 'Friday, 29 August 2026'.

    An unknown IANA name falls back to the instant as given rather than raising. Getting the
    date one timezone wrong makes the agent read back a date a person can correct; refusing
    to compose the assistant at all makes the phone not ring.
    """
    try:
        local = now.astimezone(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        local = now
    return f"{local:%A}, {local.day} {local:%B %Y}"


def profile_from_settings(settings: Settings) -> CompanyProfile:
    """The company the agent works for, out of the environment.

    Here rather than in ``config.py`` because ``CompanyProfile`` is a domain type and
    ``config.py`` imports nothing from app. Every caller that composes an assistant needs
    one, so it lives next to the composer.
    """
    return CompanyProfile(
        display_name=settings.company_name,
        business_type=settings.company_business_type,  # type: ignore[arg-type]
        city=settings.company_city,
        country=settings.company_country,
        currency=settings.company_currency,
        timezone=settings.company_timezone,
        primary_language=settings.company_primary_language,
        fallback_language=settings.company_fallback_language,
        agent_name=settings.agent_name,
        agent_role=settings.agent_role,
    )


def build_assistant(
    profile: CompanyProfile, context: CallContext, settings: Settings
) -> dict[str, object]:
    """One transient assistant for one call.

    Transient rather than an ``assistantId``: ``agent/prompts.py`` is the single source of
    prompt truth, and nothing about a call should depend on dashboard config that can drift
    between the rehearsal and the demo.
    """
    model_provider, model_id = _split_vendor_id(settings.vapi_model, key="VAPI_MODEL")
    voice_provider, voice_id = _split_vendor_id(settings.vapi_voice_id, key="VAPI_VOICE_ID")
    transcriber_provider, transcriber_id = _split_vendor_id(
        settings.vapi_transcriber, key="VAPI_TRANSCRIBER"
    )
    base_url = _require(settings.public_base_url, key="PUBLIC_BASE_URL").rstrip("/")
    secret = _require(settings.vapi_server_secret, key="VAPI_SERVER_SECRET")

    language = profile.primary_language.lower().split("-")[0]

    greeting = build_greeting(profile, context)
    if settings.recording_enabled:
        notice = _require(settings.recording_consent_notice, key="RECORDING_CONSENT_NOTICE")
        greeting = f"{notice.strip()} {greeting}"

    return {
        "name": f"{profile.agent_name} · {context.phase.value}",
        "firstMessage": greeting,
        "model": {
            "provider": model_provider,
            "model": model_id,
            "messages": [
                {"role": "system", "content": build_runtime_system_prompt(profile, context)}
            ],
            "tools": build_tool_definitions(settings),
        },
        "voice": {"provider": voice_provider, "voiceId": voice_id},
        "transcriber": {
            "provider": transcriber_provider,
            "model": transcriber_id,
            "language": language,
        },
        # The assistant-level URL carries the lifecycle messages. Tool calls go to the
        # tool-level URL above, so the endpoint a stranger's speech can reach is not the
        # endpoint that composes assistants.
        "server": {"url": f"{base_url}/vapi/events", "secret": secret},
        "artifactPlan": {
            "recordingEnabled": settings.recording_enabled,
            "transcriptPlan": {"enabled": True},
        },
    }
