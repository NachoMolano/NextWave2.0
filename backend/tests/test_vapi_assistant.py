"""The transient assistant: what goes on the wire before a phone rings.

Two kinds of assertion here. The first kind is about shape -- the tool schemas Vapi
validates against, the server URLs, the recording plan -- and it can only prove the payload
is self-consistent, not that Vapi accepts it. CP4 is what proves that.

The second kind is about leaks, and it is the one that matters even before CP4: the ceiling
and the target are in the prompt so the agent can negotiate with judgement, and they must not
be in anything it says or in anything a counterparty can ask a tool for.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.config import Settings
from app.domain import CallContext, CallPhase
from app.vapi.assistant import (
    ENDPOINTING_SPEAKERS,
    STOP_SPEAKING_PLAN,
    TOOL_ARGUMENT_MODELS,
    WARM_TRANSFER_PLAN,
    build_assistant,
    build_tool_definitions,
    profile_from_settings,
    spoken_today,
    transfer_message,
)

NOW = datetime(2026, 8, 30, 18, 0, tzinfo=UTC)


def _settings(**overrides: Any) -> Settings:
    """Vendor ids that are obviously placeholders.

    Real ones live in .env and are verified against Vapi's docs before a call is placed;
    hardcoding a plausible-looking id in a test is how a stale one ends up in source.
    """
    base: dict[str, Any] = {
        "vapi_model": "test-provider/test-model",
        "vapi_voice_id": "test-voice-provider/test-voice",
        "vapi_transcriber": "test-transcriber-provider/test-transcriber",
        "vapi_server_secret": "shared-secret-for-tests",
        "public_base_url": "https://volta.example.ngrok.app",
        "vapi_tool_timeout_seconds": 20,
        "recording_enabled": True,
        "recording_consent_notice": "This call is recorded for operational evidence.",
    }
    base.update(overrides)
    return Settings(**base)


PROFILE = profile_from_settings(_settings())


def _context(phase: CallPhase = CallPhase.RFQ, **overrides: Any) -> CallContext:
    base: dict[str, Any] = {
        "phase": phase,
        "today": "Sunday, 30 August 2026",
        "reference": "OP-1042",
        "origin": "Manzanillo",
        "destination": "Guadalajara",
        "cargo": "textiles",
        "equipment": "40-foot container chassis",
        "counterparty_name": "Transportes del Pacifico",
    }
    base.update(overrides)
    return CallContext(**base)


# ------------------------------------------------------------------------------ the shape


@pytest.mark.parametrize(
    "phase",
    [
        CallPhase.RFQ,
        CallPhase.AWARD,
        CallPhase.RENEGOTIATION,
        CallPhase.INBOUND,
        CallPhase.STATUS_CHECK,
    ],
    ids=lambda phase: phase.value,
)
def test_every_speakable_phase_composes_an_assistant(phase: CallPhase) -> None:
    """Every phase the system can enter must produce a complete transient assistant."""
    assistant = build_assistant(PROFILE, _context(phase), _settings())

    assert assistant["firstMessage"]
    model = assistant["model"]
    assert isinstance(model, dict)
    assert model["messages"][0]["role"] == "system"
    assert model["messages"][0]["content"].strip()


def test_the_recording_is_on_because_evidence_depends_on_it() -> None:
    """``commitments.evidence_anchor_ms`` is NOT NULL. No recording, no commitment."""
    assistant = build_assistant(PROFILE, _context(), _settings())

    plan = assistant["artifactPlan"]
    assert isinstance(plan, dict)
    assert plan["recordingEnabled"] is True
    assert plan["transcriptPlan"]["enabled"] is True


def test_recording_is_opt_in_and_disables_preagreement_without_evidence() -> None:
    settings = _settings(recording_enabled=False, recording_consent_notice="")
    assistant = build_assistant(PROFILE, _context(), settings)

    assert assistant["artifactPlan"]["recordingEnabled"] is False
    names = {
        tool["function"]["name"]
        for tool in assistant["model"]["tools"]
        if tool["type"] == "function"
    }
    assert "confirm_preagreement" not in names


def test_recording_notice_is_spoken_before_the_greeting() -> None:
    assistant = build_assistant(PROFILE, _context(), _settings())
    assert str(assistant["firstMessage"]).startswith("This call is recorded")


def test_the_tool_url_and_the_event_url_are_different_endpoints() -> None:
    """A stranger's speech reaches /vapi/tools. It must not reach the assistant composer."""
    settings = _settings()
    assistant = build_assistant(PROFILE, _context(), settings)

    server = assistant["server"]
    assert isinstance(server, dict)
    assert server["url"] == "https://volta.example.ngrok.app/vapi/events"
    assert server["secret"] == settings.vapi_server_secret

    model = assistant["model"]
    assert isinstance(model, dict)
    for tool in model["tools"]:
        if tool["type"] != "function":
            continue
        assert tool["server"]["url"] == "https://volta.example.ngrok.app/vapi/tools"
        assert tool["server"]["secret"] == settings.vapi_server_secret


def test_exactly_the_five_tools_plus_transfer_are_offered() -> None:
    """The complete mutation surface a stranger on the phone can reach."""
    tools = build_tool_definitions(_settings())

    functions = [t["function"]["name"] for t in tools if t["type"] == "function"]
    assert functions == list(TOOL_ARGUMENT_MODELS)
    assert [t["type"] for t in tools].count("transferCall") == 1


def test_the_transfer_tool_carries_no_destination_of_its_own() -> None:
    """An empty transferCall is what makes Vapi ask us where to send the call.

    A destination baked into the tool would let the model pick from a list we published;
    with none, the only path is transfer-destination-request, which webhook.py answers after
    writing the approval.
    """
    (transfer,) = [t for t in build_tool_definitions(_settings()) if t["type"] == "transferCall"]

    assert "destinations" not in transfer
    assert WARM_TRANSFER_PLAN["mode"] == "warm-transfer-say-summary"


def test_tool_schemas_are_self_contained() -> None:
    """No ``$ref``/``$defs``: providers differ on whether they resolve them inside a function
    schema, and a schema the provider silently drops is a tool called with the wrong shape."""
    for tool in build_tool_definitions(_settings()):
        if tool["type"] != "function":
            continue
        rendered = repr(tool["function"]["parameters"])
        assert "$ref" not in rendered, f"{tool['function']['name']} still references a $def"
        assert "$defs" not in rendered


def test_the_nested_quote_component_survives_inlining() -> None:
    """propose_quote is the one with a nested model, so it is the one inlining can break."""
    (propose,) = [
        t
        for t in build_tool_definitions(_settings())
        if t["type"] == "function" and t["function"]["name"] == "propose_quote"
    ]

    components = propose["function"]["parameters"]["properties"]["components"]
    item = components["items"]
    assert set(item["properties"]) == {"name", "amount", "currency"}
    assert item["properties"]["currency"]["description"]


def test_the_tool_timeout_is_stated_rather_than_inherited() -> None:
    """Vapi's default wait is undocumented, so it is set explicitly."""
    for tool in build_tool_definitions(_settings(vapi_tool_timeout_seconds=45)):
        if tool["type"] == "function":
            assert tool["server"]["timeoutSeconds"] == 45


# ------------------------------------------------------------------------------ turn taking


def test_turn_taking_is_stated_rather_than_inherited() -> None:
    """Both plans on the wire, every time.

    Send nothing and Vapi endpoints on transcription punctuation: a carrier who pauses for
    breath gets a full stop from the transcriber and is talked over 100ms later. The
    defaults are documented but not contractual, so neither plan is left to them.
    """
    assistant = build_assistant(PROFILE, _context(), _settings())

    start = assistant["startSpeakingPlan"]
    assert isinstance(start, dict)
    assert start["smartEndpointingPlan"]["provider"], "a model decides, not punctuation"
    stop = assistant["stopSpeakingPlan"]
    assert "numWords" in stop and "voiceSeconds" in stop, "the trigger is ours to state"


def test_the_recap_yields_faster_than_the_rest_of_the_call() -> None:
    """The award phase keeps the twitchy trigger; nothing else does.

    ``numWords: 0`` yields on 100ms of anything voice-shaped, which is right while reading
    terms back -- a carrier objecting mid-recap must be heard before the sentence ends -- and
    wrong on an inbound call from a moving truck, where cab noise is also voice-shaped. The
    30 Aug inbound call has the agent cut dead after "Thank you for" and never recovering.
    """
    award = build_assistant(PROFILE, _context(CallPhase.AWARD), _settings())["stopSpeakingPlan"]
    inbound = build_assistant(PROFILE, _context(CallPhase.INBOUND), _settings())["stopSpeakingPlan"]

    assert award["numWords"] == 0, "an objection to the terms cannot wait for two words"
    assert inbound["numWords"] > 0, "voice-activity alone is not a turn on a bad line"
    assert inbound["voiceSeconds"] > award["voiceSeconds"]


def test_the_call_does_not_inherit_an_office_it_is_not_sitting_in() -> None:
    """Both audio keys stated, because the Vapi default backgroundSound is ambient office.

    Omitted until 30 Aug, so every call carried whatever the account default was, and a
    carrier hearing office chatter behind a voice hears a bad line -- reported as
    interference on the inbound leg, where nobody could explain why outbound sounded fine.
    """
    assistant = build_assistant(PROFILE, _context(), _settings())

    assert assistant["backgroundSound"] == "off"
    assert assistant["backgroundDenoisingEnabled"] is True


def test_a_backchannel_does_not_cut_the_recap_short() -> None:
    """ "mm-hmm" through the terms read-back is agreement, not an interruption.

    The recap is what ``confirm_preagreement`` attests to. If a carrier's acknowledgement
    stops the agent mid-sentence, the terms they said yes to are not the terms we read.
    """
    acknowledgements = build_assistant(PROFILE, _context(), _settings())["stopSpeakingPlan"][
        "acknowledgementPhrases"
    ]

    assert {"mm-hmm", "okay", "right"} <= set(acknowledgements)


def test_a_number_still_being_spoken_is_not_a_finished_turn() -> None:
    """Invariant 8, enforced at the endpointing layer.

    "eight" then "five hundred" is one amount. Endpointing on the pause between them turns
    8500 into 8, and the agent proposes against a figure the carrier never said.
    """
    rules = build_assistant(PROFILE, _context(), _settings())["startSpeakingPlan"][
        "customEndpointingRules"
    ]

    assert rules, "no rule means the punctuation heuristic decides mid-number"
    # "user" is what this said, and what the code said, and Vapi rejects the entire call for
    # it. A test that agrees with the bug is how three carriers went un-dialled.
    assert all(rule["type"] == "customer" for rule in rules)
    assert all(rule["timeoutSeconds"] >= 1.0 for rule in rules)


def test_the_endpointing_provider_follows_the_language_when_unset() -> None:
    """``livekit`` is English-only and degrades silently on anything else.

    Nothing in the logs says the smart model was ignored, so the Spanish call just feels
    worse for reasons nobody can name. Derived rather than defaulted for that reason.
    """
    english = build_assistant(PROFILE, _context(), _settings())
    spanish_settings = _settings(company_primary_language="es-MX")
    spanish = build_assistant(profile_from_settings(spanish_settings), _context(), spanish_settings)

    assert english["startSpeakingPlan"]["smartEndpointingPlan"]["provider"] == "livekit"
    assert spanish["startSpeakingPlan"]["smartEndpointingPlan"]["provider"] == "vapi"


def test_an_explicit_endpointing_provider_wins() -> None:
    """The ids move. Trying a new one must not need a code change."""
    settings = _settings(vapi_endpointing_provider="krisp")

    assistant = build_assistant(PROFILE, _context(), settings)

    assert assistant["startSpeakingPlan"]["smartEndpointingPlan"]["provider"] == "krisp"


def test_the_wait_before_speaking_comes_from_config() -> None:
    settings = _settings(vapi_start_speaking_wait_seconds=0.9)

    assistant = build_assistant(PROFILE, _context(), settings)

    assert assistant["startSpeakingPlan"]["waitSeconds"] == 0.9


def test_one_calls_plan_cannot_be_edited_into_the_next() -> None:
    """The plan is a module constant. A payload that shares its list shares its mutations."""
    assistant = build_assistant(PROFILE, _context(), _settings())

    assistant["stopSpeakingPlan"]["acknowledgementPhrases"].append("whatever you say")

    assert "whatever you say" not in STOP_SPEAKING_PLAN["acknowledgementPhrases"]


# ----------------------------------------------------------------------------- the leaks


def test_no_mandate_figure_reaches_the_greeting() -> None:
    """The greeting is the one line guaranteed to be spoken, so it is the cheapest leak check."""
    context = _context(price_ceiling=Decimal("9000"), target_price=Decimal("8200"))

    assistant = build_assistant(PROFILE, context, _settings())

    greeting = assistant["firstMessage"]
    assert isinstance(greeting, str)
    for figure in ("9000", "9,000", "8200", "8,200"):
        assert figure not in greeting


def test_no_mandate_language_reaches_a_tool_description() -> None:
    """Tool descriptions are model-visible text, and the model can be asked to recite them.

    Not a figure check -- the descriptions are static, so no figure could reach them. It is
    a vocabulary check: a description that names the ceiling teaches the model that a
    ceiling exists and is worth asking about.
    """
    rendered = repr(build_tool_definitions(_settings())).lower()

    for phrase in ("ceiling", "price cap", "target price", "mandate", "maximum we"):
        assert phrase not in rendered, f"a tool description names {phrase!r}"


def test_a_tool_description_never_reads_as_an_authorization() -> None:
    """A description saying "book" is language the model can quote back as permission."""
    for tool in build_tool_definitions(_settings()):
        description = tool["function"].get("description", "").lower()
        for forbidden in ("you may approve", "book the load", "award the"):
            assert forbidden not in description


# ------------------------------------------------------------------- vendor ids and config


@pytest.mark.parametrize(
    ("field", "key"),
    [
        ("vapi_model", "VAPI_MODEL"),
        ("vapi_voice_id", "VAPI_VOICE_ID"),
        ("vapi_transcriber", "VAPI_TRANSCRIBER"),
    ],
)
def test_a_vendor_id_without_its_provider_refuses_to_compose(field: str, key: str) -> None:
    """A wrong provider is a 400 from Vapi after the assistant was already composed."""
    with pytest.raises(ValueError, match=key):
        build_assistant(PROFILE, _context(), _settings(**{field: "gpt-4o"}))


@pytest.mark.parametrize("field", ["vapi_model", "vapi_voice_id", "vapi_transcriber"])
def test_an_unset_vendor_id_refuses_to_compose(field: str) -> None:
    """A call that connects and cannot speak costs the credits and the carrier's patience."""
    with pytest.raises(ValueError):
        build_assistant(PROFILE, _context(), _settings(**{field: ""}))


@pytest.mark.parametrize(
    ("field", "key"),
    [("public_base_url", "PUBLIC_BASE_URL"), ("vapi_server_secret", "VAPI_SERVER_SECRET")],
)
def test_an_unreachable_server_url_or_missing_secret_refuses_to_compose(
    field: str, key: str
) -> None:
    """An assistant with no tool server is an agent that can talk and cannot record."""
    with pytest.raises(ValueError, match=key):
        build_assistant(PROFILE, _context(), _settings(**{field: ""}))


def test_every_vendor_id_on_the_wire_came_from_config() -> None:
    """The ids move and most tutorials online are stale, so none of them lives in source.

    Asserted as a property of the payload rather than by grepping the module: what matters
    is that changing ``.env`` changes what Vapi receives, with no default underneath it that
    a forgotten key could fall back to.
    """
    settings = _settings(
        vapi_model="provider-a/model-a",
        vapi_voice_id="provider-b/voice-b",
        vapi_transcriber="provider-c/transcriber-c",
    )

    assistant = build_assistant(PROFILE, _context(), settings)

    assert assistant["model"]["provider"] == "provider-a"
    assert assistant["model"]["model"] == "model-a"
    assert assistant["voice"] == {"provider": "provider-b", "voiceId": "voice-b"}
    assert assistant["transcriber"]["provider"] == "provider-c"
    assert assistant["transcriber"]["model"] == "transcriber-c"
    assert assistant["transcriber"]["language"] == "en", "from the profile, not a default"


def test_the_profile_comes_out_of_the_environment() -> None:
    profile = profile_from_settings(_settings(company_name="Pacific Textiles"))

    assert profile.display_name == "Pacific Textiles"
    assert profile.business_type == "importer"
    assert profile.timezone == "America/Mexico_City"


# ------------------------------------------------------------------------------ the clock


def test_today_is_spoken_in_the_companys_timezone() -> None:
    """18:00 UTC is still the 30th in Guadalajara; 06:00 UTC on the 31st is the 30th there."""
    assert spoken_today(NOW, "America/Mexico_City") == "Sunday, 30 August 2026"
    assert (
        spoken_today(datetime(2026, 8, 31, 3, 0, tzinfo=UTC), "America/Mexico_City")
        == "Sunday, 30 August 2026"
    )


def test_an_unknown_timezone_degrades_instead_of_refusing() -> None:
    """A date one zone off gets corrected on the call. A composition failure does not ring."""
    assert spoken_today(NOW, "Mars/Olympus_Mons") == "Sunday, 30 August 2026"


def test_the_transfer_message_asks_them_to_stay_on_the_line() -> None:
    """The failure mode of an escalation is the counterparty hanging up during the silence."""
    assert transfer_message(PROFILE).strip()


# --------------------------------------------------------------- what Vapi's validator rejects


def test_no_argument_property_is_left_without_a_type() -> None:
    """``str | None`` compiles to a bare ``anyOf`` -- Vapi 400s the whole assistant on it.

    The refusal happens at dial time, so this is the difference between a call that rings and
    a campaign that logs a dial failure for every carrier at once.
    """
    for tool in build_tool_definitions(_settings()):
        parameters = tool["function"].get("parameters")
        if not parameters:
            continue
        for name, schema in parameters["properties"].items():
            assert "anyOf" not in schema, f"{tool['function']['name']}.{name} still a union"
            declared = schema.get("type")
            assert declared, f"{tool['function']['name']}.{name} has no type"
            if isinstance(declared, list):
                assert declared[0] != "null", "the concrete type comes first"


def test_the_parameters_object_carries_no_description() -> None:
    """Vapi accepts a description on every property but not on the parameters object itself."""
    for tool in build_tool_definitions(_settings()):
        parameters = tool["function"].get("parameters")
        if parameters:
            assert "description" not in parameters


def test_an_optional_argument_stays_optional() -> None:
    """Collapsing the union must not quietly make a field the model may omit required."""
    (quote,) = [
        t for t in build_tool_definitions(_settings()) if t["function"]["name"] == "propose_quote"
    ]
    schema = quote["function"]["parameters"]["properties"]["valid_until"]
    assert schema["type"] == ["string", "null"]
    assert "valid_until" not in quote["function"]["parameters"].get("required", [])


def test_endpointing_rules_use_a_speaker_vapi_accepts() -> None:
    """Vapi accepts exactly assistant, customer or both, and 400s the whole call otherwise.

    These rules said "user". Every dial on 30 Aug came back "Vapi refused the call with 400:
    each value in customEndpointingRules.type must be one of the following values: assistant,
    customer, both" -- three carriers un-dialled, and invisible until the campaign stopped
    swallowing dial failures. The value is a vendor enum, not a synonym to be tidied.
    """
    rules = build_assistant(PROFILE, _context(), _settings())["startSpeakingPlan"][
        "customEndpointingRules"
    ]

    assert rules, "the numeric endpointing rules protect invariant 8 and must be sent"
    for rule in rules:
        assert rule["type"] in ENDPOINTING_SPEAKERS, rule
