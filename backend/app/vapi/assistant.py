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
from app.domain import CallContext, CallPhase, CompanyProfile
from app.tools.model import (
    ConfirmPreagreementArgs,
    LookupOrderArgs,
    ProposeQuoteArgs,
    ReportIncidentArgs,
    VerifyCallerArgs,
)

__all__ = [
    "ENDPOINTING_SPEAKERS",
    "PATIENT_STOP_SPEAKING_PLAN",
    "STOP_SPEAKING_PLAN",
    "TOOL_ARGUMENT_MODELS",
    "WARM_TRANSFER_PLAN",
    "build_assistant",
    "build_start_speaking_plan",
    "build_stop_speaking_plan",
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


#: When the agent stops talking because the caller started. Sent explicitly rather than
#: inherited: Vapi's defaults are documented but not contractual, and turn-taking is the
#: first thing a carrier judges us on.
#:
#: ``numWords: 0`` is voice-activity detection -- the agent yields on the sound of a voice
#: rather than waiting for words to come back transcribed, which costs 200-500ms more.
#: ``voiceSeconds`` is tightened below the 0.2 default because a carrier who has to say
#: "no, listen" twice has already decided we are a robot. Some run-on past an interruption
#: is downstream audio already buffered in the telephony leg and no setting removes it.
#:
#: ``acknowledgementPhrases`` is the one that earns its place: a carrier saying "mm-hmm"
#: through the terms read-back is agreeing, not interrupting. Without it every backchannel
#: cuts the recap short, and the recap is what ``confirm_preagreement`` attests to.
STOP_SPEAKING_PLAN: dict[str, Any] = {
    "numWords": 0,
    "voiceSeconds": 0.1,
    "backoffSeconds": 0.8,
    "acknowledgementPhrases": [
        "okay",
        "ok",
        "right",
        "uh-huh",
        "mm-hmm",
        "yeah",
        "yep",
        "sure",
        "got it",
        "go ahead",
        "si",
        "sí",
        "ajá",
        "claro",
        "ándale",
    ],
}

#: The same plan with the trigger backed off, for every phase that is not the award recap.
#:
#: ``numWords: 0`` yields on 100ms of anything voice-shaped. That is right when we are
#: reading terms back and wrong everywhere else: the 30 Aug inbound call from a moving truck
#: shows the agent cut dead after "Thank you for", because cab noise is voice-shaped. Waiting
#: for two transcribed words costs a few hundred milliseconds and buys a turn that survives a
#: bad line. The award recap keeps the twitchy plan -- there, being interrupted is the
#: cheaper failure, because a carrier who objects mid-read-back must be heard immediately.
PATIENT_STOP_SPEAKING_PLAN: dict[str, Any] = {
    **STOP_SPEAKING_PLAN,
    "numWords": 2,
    "voiceSeconds": 0.2,
}


def build_stop_speaking_plan(context: CallContext) -> dict[str, Any]:
    """How readily the agent yields, chosen by what the phase is for.

    Copied rather than shared: a payload holding the module constant's own list lets one
    call's plan be edited into the next.
    """
    plan = STOP_SPEAKING_PLAN if context.phase is CallPhase.AWARD else PATIENT_STOP_SPEAKING_PLAN
    return {**plan, "acknowledgementPhrases": list(plan["acknowledgementPhrases"])}


#: Words that mean a number is still being spoken. A carrier reading a rate says "eight" and
#: then "five hundred"; endpointing on the pause between them is how "8500" becomes "8".
_NUMBER_WORDS = (
    "zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    "fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    "sixty|seventy|eighty|ninety|hundred|thousand|point|and"
)

#: Highest-priority endpointing rules, evaluated before any model or punctuation heuristic.
#: Both exist to serve invariant 8 -- never infer numbers or dates. A turn that ends on a
#: number is a turn that may not have finished, so it gets a wait long enough to hear the
#: rest instead of a proposal built from half an amount.
#:
#: Anchored to the end of the turn on purpose. Matching a digit anywhere would put two dead
#: seconds after "yes, 8500 works for me", which is the sluggishness these rules exist to
#: avoid everywhere else.
#: ``customer`` is the caller. Vapi accepts exactly ``assistant``, ``customer`` or ``both``
#: here, and rejects the whole call with a 400 for anything else -- these rules said "user",
#: so every dial on 30 Aug was refused before it rang. The name is checked by
#: ``test_endpointing_rules_use_a_speaker_vapi_accepts``; it is not a synonym to be tidied.
ENDPOINTING_SPEAKERS = frozenset({"assistant", "customer", "both"})

_NUMERIC_ENDPOINTING_RULES: list[dict[str, Any]] = [
    {"type": "customer", "regex": r"\d\s*$", "timeoutSeconds": 2.0},
    {"type": "customer", "regex": rf"\b({_NUMBER_WORDS})\s*$", "timeoutSeconds": 2.0},
]

#: Vapi's own model, and the only smart-endpointing provider that is not English-only.
_NON_ENGLISH_ENDPOINTING_PROVIDER = "vapi"
_ENGLISH_ENDPOINTING_PROVIDER = "livekit"


def _endpointing_provider(profile: CompanyProfile, settings: Settings) -> str:
    """Which model decides the caller has finished speaking.

    Derived from the profile when unset because ``livekit`` is English-only and silently
    degrades on anything else: a Spanish call would fall back to punctuation heuristics with
    nothing in the logs to say so. An explicit setting always wins -- these ids move, and
    this is the one place a new one can be tried without a code change.
    """
    configured = settings.vapi_endpointing_provider.strip()
    if configured:
        return configured
    if profile.primary_language.lower().startswith("en"):
        return _ENGLISH_ENDPOINTING_PROVIDER
    return _NON_ENGLISH_ENDPOINTING_PROVIDER


def build_start_speaking_plan(profile: CompanyProfile, settings: Settings) -> dict[str, Any]:
    """When the agent starts talking after the caller stops.

    Without this Vapi endpoints on transcription punctuation, and a carrier who pauses for
    breath gets a period from the transcriber and an interruption 100ms later. A smart
    endpointing model reads the content of the turn instead of its punctuation, which is the
    difference between waiting through "the rate would be, uh" and talking over it.
    """
    return {
        "waitSeconds": settings.vapi_start_speaking_wait_seconds,
        "smartEndpointingPlan": {"provider": _endpointing_provider(profile, settings)},
        "customEndpointingRules": [dict(rule) for rule in _NUMERIC_ENDPOINTING_RULES],
    }


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
        "startSpeakingPlan": build_start_speaking_plan(profile, settings),
        "stopSpeakingPlan": build_stop_speaking_plan(context),
        # Stated, not inherited. Both were omitted until 30 Aug, so both took whatever the
        # Vapi account default happened to be -- and the default backgroundSound is an
        # ambient office loop, which is what a carrier hears as a bad line. Silence is the
        # only honest choice for a machine that is not in an office.
        "backgroundSound": "off",
        "backgroundDenoisingEnabled": True,
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
