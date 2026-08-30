"""POST /vapi/events -- the four server messages, and what each one is allowed to do.

Three properties this file exists to pin down:

  * a redelivered ``end-of-call-report`` is a no-op, not a second brief;
  * an ``assistant-request`` answers inside the 7.5-second budget even when the carrier
    lookup hangs, because a late assistant is a dropped call;
  * the escalation destination is decided here, server-side, and refused when there is
    nowhere to send it.

No network, no database, no phone call.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.domain import (
    ApprovalKind,
    ApprovalReason,
    CallContext,
    CallDirection,
    CallPhase,
    CallRecord,
    CallReport,
    CallStatus,
    Carrier,
    CompanyProfile,
    Store,
    Turn,
)
from app.tools.calls import CallLedger
from app.vapi.webhook import create_webhook_router
from tests.fakes import InMemoryStore, ScriptedReportModel

SECRET = "shared-secret-for-tests"
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
ESCALATION_NUMBER = "+523300000000"
FIXTURES = Path(__file__).parent / "fixtures" / "vapi"

PROFILE = CompanyProfile(
    display_name="Pacific Textiles",
    business_type="importer",
    city="Guadalajara",
    country="Mexico",
    timezone="America/Mexico_City",
    primary_language="en",
    fallback_language="es-MX",
)


# --------------------------------------------------------------------------------- doubles


class RecordingLedger(CallLedger):
    """Track E's call ledger, reduced to the behaviour this router depends on.

    A subclass of the real class so a signature change over there breaks these tests rather
    than leaving them green against a shape that no longer exists.
    """

    def __init__(self, store: InMemoryStore) -> None:
        super().__init__(store, now=lambda: NOW)
        self.store = store
        self.applied_keys: set[str] = set()
        self.upserts: list[CallRecord] = []
        self.finalized: list[CallRecord] = []

    async def upsert_from_webhook(self, call: CallRecord, event_key: str) -> str | None:
        if event_key in self.applied_keys:
            return None
        self.applied_keys.add(event_key)
        self.upserts.append(call)
        return await self.store.upsert_call(call)

    async def finalize(self, call: CallRecord, event_key: str) -> str | None:
        if event_key in self.applied_keys:
            return None
        self.applied_keys.add(event_key)
        self.finalized.append(call)
        existing = await self.store.call_by_vapi_id(call.vapi_call_id)
        merged = call.model_copy(
            update={
                "context": existing.context if existing else {},
                "order_id": existing.order_id if existing else None,
                "carrier_id": existing.carrier_id if existing else None,
            }
        )
        return await self.store.upsert_call(merged)

    async def anchor_ms(self, call_id: str) -> int:
        return 0


def _assistant(context: CallContext) -> dict[str, object]:
    """A stand-in composer. assistant.py's own composition is tested in test_vapi_assistant."""
    return {"firstMessage": "hello", "phase": context.phase.value, "name": context.expected_carrier}


# --------------------------------------------------------------------------------- harness


def _router(
    store: Store,
    ledger: CallLedger,
    *,
    reporter: ScriptedReportModel | None = None,
    escalation_number: str = ESCALATION_NUMBER,
    secret: str = SECRET,
    compose: Any = _assistant,
) -> Any:
    return create_webhook_router(
        store=store,
        ledger=ledger,
        reporter=reporter or ScriptedReportModel(),
        profile=PROFILE,
        build_assistant_for=compose,
        escalation_number=escalation_number,
        server_secret=secret,
        now=lambda: NOW,
    )


async def _post(
    payload: dict[str, Any],
    store: Store,
    ledger: CallLedger,
    *,
    header: str | None = SECRET,
    **kwargs: Any,
) -> tuple[int, dict[str, Any]]:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(_router(store, ledger, **kwargs), prefix="/vapi")
    headers = {"X-Vapi-Secret": header} if header is not None else {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://webhook.test"
    ) as client:
        response = await client.post("/vapi/events", json=payload, headers=headers)
    return response.status_code, response.json()


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


# --------------------------------------------------------------------------- authentication


async def test_a_wrong_secret_is_refused_before_the_body_is_parsed() -> None:
    store = InMemoryStore()
    ledger = RecordingLedger(store)

    code, _ = await _post(_fixture("status_update.json"), store, ledger, header="wrong")

    assert code == 401
    assert ledger.upserts == []


async def test_an_unconfigured_secret_refuses_everything() -> None:
    """Fail closed: an empty VAPI_SERVER_SECRET is not an invitation."""
    store = InMemoryStore()
    ledger = RecordingLedger(store)

    code, _ = await _post(_fixture("status_update.json"), store, ledger, secret="", header="")

    assert code == 401
    assert ledger.upserts == []


# ------------------------------------------------------------------------------ status-update


async def test_a_status_update_reaches_the_call_ledger() -> None:
    store = InMemoryStore()
    ledger = RecordingLedger(store)

    code, body = await _post(_fixture("status_update.json"), store, ledger)

    assert code == 200
    assert body["received"] is True
    assert body["duplicate"] is False
    (record,) = ledger.upserts
    assert record.vapi_call_id == "vapi-call-provisional-1"
    assert record.status is CallStatus.ACTIVE, "'in-progress' is an active call"
    assert record.direction is CallDirection.OUTBOUND


async def test_a_redelivered_status_update_is_a_no_op() -> None:
    """Vapi redelivers. A second delivery must not create a second row."""
    store = InMemoryStore()
    ledger = RecordingLedger(store)
    payload = _fixture("status_update.json")

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(_router(store, ledger), prefix="/vapi")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://webhook.test"
    ) as client:
        first = await client.post("/vapi/events", json=payload, headers={"X-Vapi-Secret": SECRET})
        second = await client.post("/vapi/events", json=payload, headers={"X-Vapi-Secret": SECRET})

    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert len(ledger.upserts) == 1
    assert len(store.calls) == 1


# ------------------------------------------------------------------------ end-of-call-report


async def test_an_end_of_call_report_stores_the_recording_and_the_transcript() -> None:
    store = InMemoryStore()
    ledger = RecordingLedger(store)
    reporter = ScriptedReportModel()

    code, body = await _post(_fixture("end_of_call_report.json"), store, ledger, reporter=reporter)

    assert code == 200
    assert body["reported"] is True
    (record,) = ledger.finalized
    assert record.recording_url == "https://storage.vapi.ai/provisional.wav"
    assert record.status is CallStatus.ENDED
    assert record.ended_reason == "customer-ended-call"
    assert [t.speaker for t in record.transcript] == ["agent", "caller"]
    assert record.transcript[1].text == "Eight thousand five hundred dollars."
    assert record.transcript[1].offset_ms == 11_200
    assert record.cost_cents == 7, "0.0731 dollars, in cents"
    assert len(store.reports) == 1


async def test_replaying_the_same_end_of_call_report_is_a_no_op() -> None:
    """The headline idempotency case: a redelivery must not re-run the extraction.

    Re-running is not merely wasteful. If the second extraction disagreed with the first it
    would overwrite evidence a human has already been shown.
    """
    store = InMemoryStore()
    ledger = RecordingLedger(store)
    reporter = ScriptedReportModel()
    payload = _fixture("end_of_call_report.json")

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(_router(store, ledger, reporter=reporter), prefix="/vapi")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://webhook.test"
    ) as client:
        first = await client.post("/vapi/events", json=payload, headers={"X-Vapi-Secret": SECRET})
        second = await client.post("/vapi/events", json=payload, headers={"X-Vapi-Secret": SECRET})

    assert first.json() == {"received": True, "reported": True}
    assert second.json() == {"received": True, "duplicate": True}
    assert len(ledger.finalized) == 1
    assert reporter.calls == [next(iter(store.calls))], "the model ran exactly once"
    assert len(store.reports) == 1


async def test_an_epoch_shaped_offset_is_dropped_rather_than_stored() -> None:
    """``secondsFromStart`` is undocumented and has been reported returning an epoch value.

    A 1.7-billion-second "offset" is that bug, not a fifty-year call. Evidence that is wrong
    is worse than evidence that is missing: tools/calls.py measures the real anchor itself.
    """
    store = InMemoryStore()
    ledger = RecordingLedger(store)
    payload = _fixture("end_of_call_report.json")
    payload["message"]["artifact"]["messages"][1]["secondsFromStart"] = 1_756_500_000

    await _post(payload, store, ledger)

    (record,) = ledger.finalized
    assert record.transcript[1].offset_ms is None
    assert record.transcript[0].offset_ms == 400, "a plausible offset still survives"


@pytest.mark.parametrize(
    ("artifact", "expected"),
    [
        ({"recordingUrl": "https://a.wav"}, "https://a.wav"),
        ({"recording": "https://b.wav"}, "https://b.wav"),
        ({"recording": {"stereoUrl": "https://c.wav"}}, "https://c.wav"),
        ({"recording": {"mono": {"combinedUrl": "https://d.wav"}}}, "https://d.wav"),
        ({}, None),
    ],
    ids=["flat", "recording-string", "stereo", "mono-combined", "absent"],
)
async def test_the_recording_is_found_wherever_this_vapi_version_puts_it(
    artifact: dict[str, Any], expected: str | None
) -> None:
    """The fixtures use the flat field; current docs describe ``artifact.recording``.

    Guessing wrong costs a commitment with no audio behind it, so all the shapes are read.
    """
    store = InMemoryStore()
    ledger = RecordingLedger(store)
    payload = {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": "vapi-call-1", "type": "outboundPhoneCall"},
            "artifact": artifact,
        }
    }

    await _post(payload, store, ledger)

    assert ledger.finalized[0].recording_url == expected


async def test_a_failing_report_model_keeps_the_transcript_it_already_stored() -> None:
    """The brief is a convenience. The transcript and the recording are the evidence."""

    class BrokenReporter(ScriptedReportModel):
        async def report(self, call_id: str, turns: list[Turn], context: CallContext) -> CallReport:
            raise RuntimeError("the extraction model is down")

    store = InMemoryStore()
    ledger = RecordingLedger(store)

    code, body = await _post(
        _fixture("end_of_call_report.json"), store, ledger, reporter=BrokenReporter()
    )

    assert code == 200
    assert body == {"received": True, "reported": False}
    assert len(ledger.finalized) == 1, "the call row was still written"
    assert store.reports == {}


# ------------------------------------------------------------------------ assistant-request


async def test_a_known_carrier_is_named_in_the_inbound_context() -> None:
    store = InMemoryStore()
    store.add_carrier(
        Carrier(id="carrier-1", name="Transportes del Pacifico", phone="+523312345678")
    )
    ledger = RecordingLedger(store)

    code, body = await _post(_fixture("assistant_request.json"), store, ledger)

    assert code == 200
    assert body["assistant"]["phase"] == "inbound"
    assert body["assistant"]["name"] == "Transportes del Pacifico"
    (record,) = ledger.upserts
    assert record.carrier_id == "carrier-1"
    assert record.direction is CallDirection.INBOUND
    assert record.context["phase"] == "inbound"


async def test_a_number_not_on_file_still_gets_an_assistant() -> None:
    """Not knowing who is calling is a state the inbound prompt is written for.

    The agent gives nothing away, records every claim as unverified and escalates. Refusing
    to answer is the one outcome it cannot recover from.
    """
    store = InMemoryStore()  # no carriers
    ledger = RecordingLedger(store)

    code, body = await _post(_fixture("assistant_request.json"), store, ledger)

    assert code == 200
    assert "assistant" in body
    assert body["assistant"]["name"] is None
    assert ledger.upserts[0].carrier_id is None, "null carrier_id is already information"


async def test_a_hanging_lookup_falls_back_instead_of_blowing_the_budget() -> None:
    """Vapi gives an assistant-request 7.5 seconds, fixed and not configurable.

    The lookup is capped well below that, and a timeout produces the unverified-caller
    assistant. The assertion is on wall-clock time because that is the actual failure.
    """

    class HangingStore(InMemoryStore):
        async def carrier_by_phone(self, phone: str) -> None:
            await asyncio.sleep(30)
            raise AssertionError("unreachable: the lookup must have been cancelled")

    store = HangingStore()
    ledger = RecordingLedger(store)

    started = asyncio.get_running_loop().time()
    code, body = await _post(_fixture("assistant_request.json"), store, ledger)
    elapsed = asyncio.get_running_loop().time() - started

    assert code == 200
    assert "assistant" in body, "a late assistant is a dropped call; an ignorant one is not"
    assert elapsed < 7.5, f"answered in {elapsed:.1f}s, past Vapi's fixed budget"


async def test_a_lookup_that_raises_falls_back_rather_than_dropping_the_call() -> None:
    class BrokenStore(InMemoryStore):
        async def carrier_by_phone(self, phone: str) -> None:
            raise ConnectionError("supabase is unreachable")

    store = BrokenStore()
    ledger = RecordingLedger(store)

    code, body = await _post(_fixture("assistant_request.json"), store, ledger)

    assert code == 200
    assert "assistant" in body


async def test_the_context_is_stored_before_the_first_tool_call_can_arrive() -> None:
    """A call is replayable only if the exact context its prompt was built from survives.

    Storing it here rather than waiting for status-update matters because the tool server
    correlates on the call row, and a tool call can land before the first status webhook.
    """
    store = InMemoryStore()
    ledger = RecordingLedger(store)

    await _post(_fixture("assistant_request.json"), store, ledger)

    stored = await store.call_by_vapi_id("vapi-call-provisional-2")
    assert stored is not None
    assert CallContext.model_validate(stored.context).phase is CallPhase.INBOUND


async def test_a_composer_that_raises_returns_an_error_not_a_broken_assistant() -> None:
    def explode(context: CallContext) -> dict[str, object]:
        raise ValueError("VAPI_MODEL is not set")

    store = InMemoryStore()
    ledger = RecordingLedger(store)

    code, body = await _post(_fixture("assistant_request.json"), store, ledger, compose=explode)

    assert code == 200
    assert body == {"error": "assistant unavailable"}


# ---------------------------------------------------------------- transfer-destination-request


def _transfer_payload(vapi_call_id: str = "vapi-call-provisional-1") -> dict[str, Any]:
    return {
        "message": {
            "type": "transfer-destination-request",
            "call": {"id": vapi_call_id, "type": "inboundPhoneCall"},
        }
    }


async def test_the_destination_is_decided_here_and_the_approval_is_written_first() -> None:
    """Escalation is a server decision. The model asks; it does not choose a number."""
    store = InMemoryStore()
    call_id = await store.upsert_call(
        CallRecord(
            vapi_call_id="vapi-call-provisional-1",
            direction=CallDirection.INBOUND,
            phase="inbound",
            order_id="order-1",
        )
    )
    ledger = RecordingLedger(store)

    code, body = await _post(_transfer_payload(), store, ledger)

    assert code == 200
    assert body["destination"]["number"] == ESCALATION_NUMBER
    assert body["destination"]["type"] == "number"
    assert body["destination"]["transferPlan"]["mode"] == "warm-transfer-say-summary", (
        "the human must hear the context before being bridged in"
    )
    assert body["destination"]["message"].strip()

    (approval,) = store.approvals.values()
    assert approval.kind is ApprovalKind.ESCALATION
    assert approval.reason is ApprovalReason.DIRECT_REQUEST
    assert approval.call_id == call_id
    assert approval.order_id == "order-1"


async def test_with_no_destination_configured_the_transfer_is_refused_and_still_recorded() -> None:
    """Bridging to a number nobody set drops the caller into silence and calls it success."""
    store = InMemoryStore()
    ledger = RecordingLedger(store)

    code, body = await _post(_transfer_payload(), store, ledger, escalation_number="")

    assert code == 200
    assert "destination" not in body
    assert body["error"]
    assert len(store.approvals) == 1, "a person was still asked for; that is the record"


async def test_an_uncorrelated_transfer_request_still_raises_an_approval() -> None:
    store = InMemoryStore()
    ledger = RecordingLedger(store)

    code, body = await _post(_transfer_payload("unknown-call"), store, ledger)

    assert code == 200
    assert body["destination"]["number"] == ESCALATION_NUMBER
    (approval,) = store.approvals.values()
    assert approval.call_id is None


# ------------------------------------------------------------------------------ robustness


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": "not an object"},
        {"message": {}},
        {"message": {"type": "speech-update"}},
        {"message": {"type": "status-update"}},
        {"message": {"type": "end-of-call-report"}},
    ],
    ids=[
        "empty",
        "message-not-an-object",
        "no-type",
        "unsubscribed",
        "status-no-call",
        "eoc-no-call",
    ],
)
async def test_a_shape_we_did_not_expect_never_returns_a_status_vapi_will_retry(
    payload: dict[str, Any],
) -> None:
    """A 500 buys a retry that cannot fix a bug, and for assistant-request it costs 7.5s."""
    store = InMemoryStore()
    ledger = RecordingLedger(store)

    code, _ = await _post(payload, store, ledger)

    assert code == 200


async def test_a_handler_that_raises_is_logged_and_answered_200() -> None:
    class ExplodingLedger(RecordingLedger):
        async def upsert_from_webhook(self, call: CallRecord, event_key: str) -> str | None:
            raise RuntimeError("the ledger fell over")

    store = InMemoryStore()
    ledger = ExplodingLedger(store)

    code, body = await _post(_fixture("status_update.json"), store, ledger)

    assert code == 200
    assert body == {"received": False}


def test_the_system_prompt_never_enters_the_transcript() -> None:
    """The prompt carries the ceiling and the target under FIGURES YOU MUST NEVER SAY OUT LOUD.

    Vapi returns it as the first entry of artifact.messages with role "system". Keeping it --
    which a role lookup with an "other" fallback does -- published the mandate to anyone with
    portal access and handed it to the report model as though a person had said it.
    """
    from app.vapi.webhook import _turns

    turns = _turns(
        {
            "messages": [
                {
                    "role": "system",
                    "message": "FIGURES YOU MUST NEVER SAY OUT LOUD Ceiling: 11,000 USD",
                    "secondsFromStart": 0,
                },
                {"role": "assistant", "message": "This is Volta.", "secondsFromStart": 1.0},
                {"role": "user", "message": "Ten thousand five hundred.", "secondsFromStart": 9.0},
                {"role": "tool_calls", "message": "propose_quote(...)", "secondsFromStart": 9.5},
            ]
        }
    )

    assert [t.speaker for t in turns] == ["agent", "caller"]
    assert not any("Ceiling" in t.text for t in turns)
    assert not any("11,000" in t.text for t in turns)
