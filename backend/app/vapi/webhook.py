"""POST /vapi/events -- every Vapi server message, on one URL.

Verify X-Vapi-Secret before parsing the body, then switch on message.type:

  assistant-request           inbound. Look up the carrier by message.call.from.phoneNumber
                              and return a transient assistant. HARD BUDGET: 7.5 seconds,
                              fixed and not configurable. Put a ~2s timeout on the lookup and
                              fall back to the unverified-caller assistant rather than
                              blowing the deadline.
  status-update               -> tools/calls.py
  end-of-call-report          store recording + transcript, then agent/report.py
  transfer-destination-request  write the approval, then return the manager's number -- or
                              refuse. Escalation is decided here, server-side, not by the
                              model choosing a destination.

Every branch answers 200, including the ones that fail. A non-200 makes Vapi retry, and a
retry of ``assistant-request`` is 7.5 more seconds a carrier spends listening to silence;
every write on this path is keyed on ``events.idempotency_key`` anyway, so the retry buys
nothing a redelivery would not already have covered. The exception is authentication: a
request with the wrong secret is not Vapi, and gets a 401.

Writes here go through ``tools/`` -- ``CallLedger`` owns the call row. The ``Store`` protocol
is used for the reads that correlate a call and for the two evidence rows that have no
policy question in them: the post-call report and the approval that an escalation raises.

STATUS: built. OWNER: Track B.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Request, Response, status

from app.domain import (
    Approval,
    ApprovalKind,
    ApprovalReason,
    CallContext,
    CallDirection,
    CallPhase,
    CallRecord,
    CallStatus,
    CompanyProfile,
    ReportModel,
    Store,
    Turn,
)
from app.tools.calls import CallLedger
from app.vapi.assistant import WARM_TRANSFER_PLAN, spoken_today, transfer_message

__all__ = ["create_webhook_router"]

log = structlog.get_logger(__name__)

#: Vapi gives an assistant-request 7.5 seconds, fixed and not configurable. The lookup gets
#: a fraction of it so there is room left to compose a prompt and serialise it: an inbound
#: call answered by an assistant that knows nothing is a working call, and one answered by
#: nothing at all is a hang-up.
_LOOKUP_TIMEOUT_SECONDS = 2.0

#: Above this, ``secondsFromStart`` is not an offset. The field is undocumented and has been
#: reported returning an epoch value; a 1.7-billion-second "offset" is that bug, not a call
#: that lasted fifty years. Dropped rather than stored, because evidence that is wrong is
#: worse than evidence that is missing -- tools/calls.py measures the real anchor itself.
_MAX_PLAUSIBLE_OFFSET_SECONDS = 24 * 60 * 60

_STATUS_BY_VAPI_STATUS: dict[str, CallStatus] = {
    "scheduled": CallStatus.QUEUED,
    "queued": CallStatus.QUEUED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.ACTIVE,
    "forwarding": CallStatus.ACTIVE,
    "ended": CallStatus.ENDED,
}

_SPEAKER_BY_ROLE: dict[str, str] = {
    "assistant": "agent",
    "bot": "agent",
    "user": "caller",
    "customer": "caller",
}


def _as_dict(value: object) -> dict[str, Any]:
    """A dict, or an empty one. Every ``.get`` below then reads as "absent", never as a crash."""
    return value if isinstance(value, dict) else {}


def _dig(payload: object, *path: str) -> Any:
    """Walk a nested dict, returning None the moment the path stops being a dict.

    Webhook payloads are a stranger's data structure. Every field this module reads is
    optional as far as the code is concerned, because a shape that changed on the vendor's
    side must degrade into a missing value, not a KeyError inside a live call.
    """
    node: Any = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _recording_url(artifact: object) -> str | None:
    """The recording, from whichever field this account's Vapi version puts it in.

    ``artifact.recordingUrl`` is the flat form the fixtures were written against; current
    docs describe ``artifact.recording``, which is sometimes a string and sometimes an
    object with per-channel URLs. All three are read because the cost of guessing wrong is a
    commitment with no audio behind it.
    """
    flat = _dig(artifact, "recordingUrl")
    if isinstance(flat, str) and flat:
        return flat
    recording = _dig(artifact, "recording")
    if isinstance(recording, str) and recording:
        return recording
    for path in (("recording", "stereoUrl"), ("recording", "mono", "combinedUrl")):
        candidate = _dig(artifact, *path)
        if isinstance(candidate, str) and candidate:
            return candidate
    stereo = _dig(artifact, "stereoRecordingUrl")
    return stereo if isinstance(stereo, str) and stereo else None


def _turns(artifact: object) -> list[Turn]:
    messages = _dig(artifact, "messages")
    if not isinstance(messages, list):
        return []
    turns: list[Turn] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = message.get("message")
        role = message.get("role")
        if not isinstance(text, str) or not text.strip():
            continue
        speaker = _SPEAKER_BY_ROLE.get(str(role).lower(), "other")
        seconds = message.get("secondsFromStart")
        offset_ms: int | None = None
        if isinstance(seconds, int | float) and 0 <= seconds <= _MAX_PLAUSIBLE_OFFSET_SECONDS:
            offset_ms = int(seconds * 1000)
        turns.append(Turn(speaker=speaker, text=text, offset_ms=offset_ms))
    return turns


def _cost_cents(message: object) -> int | None:
    cost = _dig(message, "cost")
    if isinstance(cost, int | float):
        return round(cost * 100)
    return None


def create_webhook_router(
    *,
    store: Store,
    ledger: CallLedger,
    reporter: ReportModel,
    profile: CompanyProfile,
    build_assistant_for: Callable[[CallContext], dict[str, object]],
    escalation_number: str,
    server_secret: str,
    now: Callable[[], datetime],
) -> APIRouter:
    """The event endpoint, with every dependency chosen by the composition root.

    ``build_assistant_for`` is injected rather than calling ``build_assistant`` directly so
    that main.py holds the ``Settings`` and this module never needs them -- and so a test can
    compose an assistant without a filled-in environment.
    """
    router = APIRouter()

    # ------------------------------------------------------------------ inbound

    async def _carrier_for(from_number: str | None) -> tuple[str | None, str | None]:
        """(carrier id, carrier name) for a caller, inside the latency budget.

        A timeout is not an error here. Not knowing who is calling is one of the states the
        inbound prompt is written for: the agent gives nothing away, records every claim as
        unverified and escalates. Being late is the only state it cannot recover from.
        """
        if not from_number:
            return None, None
        try:
            carrier = await asyncio.wait_for(
                store.carrier_by_phone(from_number), timeout=_LOOKUP_TIMEOUT_SECONDS
            )
        except TimeoutError:
            log.warning("vapi.inbound.lookup_timeout", from_number=from_number)
            return None, None
        except Exception:
            log.exception("vapi.inbound.lookup_failed", from_number=from_number)
            return None, None
        if carrier is None:
            return None, None
        return carrier.id, carrier.name

    async def _handle_assistant_request(message: dict[str, Any]) -> dict[str, object]:
        call = _as_dict(message.get("call"))
        vapi_call_id = call.get("id")
        from_number = _dig(call, "from", "phoneNumber") or _dig(call, "customer", "number")
        from_number = from_number if isinstance(from_number, str) else None

        carrier_id, carrier_name = await _carrier_for(from_number)

        context = CallContext(
            phase=CallPhase.INBOUND,
            today=spoken_today(now(), profile.timezone),
            counterparty_name=carrier_name,
            expected_carrier=carrier_name,
        )

        try:
            assistant = build_assistant_for(context)
        except Exception:
            # Composition failed, so there is no assistant to answer with. Vapi's documented
            # shape for "we cannot serve this" is an error string; it is better than a
            # malformed assistant, which drops the call without saying anything.
            log.exception("vapi.inbound.compose_failed", vapi_call_id=vapi_call_id)
            return {"error": "assistant unavailable"}

        # Store the context now rather than waiting for status-update. A call is replayable
        # only if the exact context its prompt was built from survives, and the first tool
        # call can arrive before the first status webhook does.
        if isinstance(vapi_call_id, str) and vapi_call_id:
            record = CallRecord(
                vapi_call_id=vapi_call_id,
                direction=CallDirection.INBOUND,
                phase=CallPhase.INBOUND.value,
                status=CallStatus.RINGING,
                carrier_id=carrier_id,
                from_number=from_number,
                started_at=now(),
                context=context.model_dump(mode="json"),
            )
            try:
                await ledger.upsert_from_webhook(record, f"{vapi_call_id}:assistant-request")
            except Exception:
                # The call still gets answered. An unrecorded inbound call is a gap in the
                # evidence; a dropped one is a carrier who could not reach us at all.
                log.exception("vapi.inbound.call_row_failed", vapi_call_id=vapi_call_id)

        return {"assistant": assistant}

    # ------------------------------------------------------------------ lifecycle

    async def _handle_status_update(message: dict[str, Any]) -> dict[str, object]:
        call = _as_dict(message.get("call"))
        vapi_call_id = call.get("id")
        if not isinstance(vapi_call_id, str) or not vapi_call_id:
            log.warning("vapi.status.no_call_id")
            return {"received": False}

        raw_status = message.get("status")
        mapped = _STATUS_BY_VAPI_STATUS.get(str(raw_status), CallStatus.QUEUED)
        inbound = str(call.get("type", "")).startswith("inbound")

        record = CallRecord(
            vapi_call_id=vapi_call_id,
            direction=CallDirection.INBOUND if inbound else CallDirection.OUTBOUND,
            phase=CallPhase.INBOUND.value if inbound else CallPhase.RFQ.value,
            status=mapped,
            from_number=_dig(call, "from", "phoneNumber"),
            to_number=_dig(call, "customer", "number"),
        )
        applied = await ledger.upsert_from_webhook(
            record, f"{vapi_call_id}:status-update:{raw_status}"
        )
        return {"received": True, "duplicate": applied is None}

    async def _handle_end_of_call_report(message: dict[str, Any]) -> dict[str, object]:
        call = _as_dict(message.get("call"))
        vapi_call_id = call.get("id")
        if not isinstance(vapi_call_id, str) or not vapi_call_id:
            log.warning("vapi.end_of_call.no_call_id")
            return {"received": False}

        artifact = message.get("artifact")
        inbound = str(call.get("type", "")).startswith("inbound")
        record = CallRecord(
            vapi_call_id=vapi_call_id,
            direction=CallDirection.INBOUND if inbound else CallDirection.OUTBOUND,
            phase=CallPhase.INBOUND.value if inbound else CallPhase.RFQ.value,
            status=CallStatus.ENDED,
            ended_at=now(),
            ended_reason=message.get("endedReason")
            if isinstance(message.get("endedReason"), str)
            else None,
            recording_url=_recording_url(artifact),
            transcript=_turns(artifact),
            cost_cents=_cost_cents(message),
        )

        call_id = await ledger.finalize(record, f"{vapi_call_id}:end-of-call-report")
        if call_id is None:
            # Vapi redelivered a report we already processed. Generating the brief again
            # would spend a model call to overwrite an identical row -- and if the second
            # extraction disagreed with the first, it would overwrite evidence.
            log.info("vapi.end_of_call.duplicate", vapi_call_id=vapi_call_id)
            return {"received": True, "duplicate": True}

        stored = await store.call(call_id)
        context = CallContext(
            phase=CallPhase.INBOUND if inbound else CallPhase.RFQ,
            today=spoken_today(now(), profile.timezone),
        )
        if stored is not None and stored.context:
            try:
                context = CallContext.model_validate(stored.context)
            except Exception:
                # An unreadable stored context is a Phase 0 shape that moved. The brief is
                # still worth having; it just loses the operation detail.
                log.exception("vapi.end_of_call.context_unreadable", call_id=call_id)

        turns = stored.transcript if stored is not None and stored.transcript else record.transcript
        try:
            report = await reporter.report(call_id, turns, context)
            await store.save_report(report)
        except Exception:
            # The transcript and recording are already stored, which is the part that is
            # evidence. The brief is a convenience on top of it, and losing it must not
            # cost us the row underneath.
            log.exception("vapi.end_of_call.report_failed", call_id=call_id)
            return {"received": True, "reported": False}

        return {"received": True, "reported": True}

    # ------------------------------------------------------------------ escalation

    async def _handle_transfer_request(message: dict[str, Any]) -> dict[str, object]:
        call = _as_dict(message.get("call"))
        vapi_call_id = call.get("id")
        record = None
        if isinstance(vapi_call_id, str) and vapi_call_id:
            try:
                record = await store.call_by_vapi_id(vapi_call_id)
            except Exception:
                log.exception("vapi.transfer.correlation_failed", vapi_call_id=vapi_call_id)

        # Raised before the destination is returned, and raised even when we refuse. The
        # approval is the record that a person was asked for; whether the bridge succeeded
        # is a separate fact.
        try:
            await store.raise_approval(
                Approval(
                    order_id=record.order_id if record is not None else None,
                    call_id=record.id if record is not None else None,
                    kind=ApprovalKind.ESCALATION,
                    reason=ApprovalReason.DIRECT_REQUEST,
                    context={
                        "vapi_call_id": vapi_call_id,
                        "source": "transfer-destination-request",
                    },
                    raised_at=now(),
                )
            )
        except Exception:
            log.exception("vapi.transfer.approval_failed", vapi_call_id=vapi_call_id)

        if not escalation_number:
            # Refusing is the honest answer. Bridging to a number nobody configured drops
            # the caller into silence and reports it as a successful hand-off.
            log.error("vapi.transfer.no_destination", vapi_call_id=vapi_call_id)
            return {"error": "no escalation destination is configured"}

        return {
            "destination": {
                "type": "number",
                "number": escalation_number,
                "message": transfer_message(profile),
                "transferPlan": dict(WARM_TRANSFER_PLAN),
            }
        }

    _HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
        "assistant-request": _handle_assistant_request,
        "status-update": _handle_status_update,
        "end-of-call-report": _handle_end_of_call_report,
        "transfer-destination-request": _handle_transfer_request,
    }

    @router.post("/events")
    async def handle_event(request: Request, response: Response) -> dict[str, object]:
        if not server_secret or request.headers.get("x-vapi-secret") != server_secret:
            log.warning("vapi.events.unauthenticated", configured=bool(server_secret))
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return {"error": "unauthorized"}

        try:
            body = await request.json()
        except Exception:
            log.exception("vapi.events.unparseable_body")
            return {"received": False}

        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, dict):
            log.warning("vapi.events.no_message")
            return {"received": False}

        message_type = message.get("type")
        handler = _HANDLERS.get(str(message_type))
        if handler is None:
            # Vapi sends more message types than we subscribe to, and it adds new ones.
            # An unknown type is not a failure; it is a message for somebody else.
            log.debug("vapi.events.ignored", type=message_type)
            return {"received": True, "handled": False}

        try:
            result = await handler(message)
        except Exception:
            # 200, not 500. A retry cannot fix a bug in here, and for assistant-request it
            # spends another 7.5 seconds of a carrier's patience finding that out.
            log.exception("vapi.events.handler_failed", type=message_type)
            return {"received": False}
        return dict(result)

    return router
