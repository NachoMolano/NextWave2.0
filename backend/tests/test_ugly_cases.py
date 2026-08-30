"""docs/UGLY_CASES.md, as tests. Every row in that table is a test in this file.

These are the cases a judge is expected to try live, plus the failure modes the invariants
in AGENTS.md exist to prevent. All twenty run against InMemoryStore: no network, no
database, no phone call.

Each test is named for its row and carries the row number, so a failure points straight at
the line in the table it broke.

OWNER: Track A.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.domain import (
    ApprovalKind,
    ApprovalReason,
    CallDirection,
    CallRecord,
    CallStatus,
    Carrier,
    CommitmentState,
    DeliveryStatus,
    IncidentSubject,
    Money,
    NotificationChannel,
    Order,
    OrderStatus,
    OutboundMessage,
    PolicyOutcome,
    QuoteStatus,
    ReasonCode,
)
from app.tools.calls import CallLedger
from app.tools.commitments import CommitmentCoordinator
from app.tools.model import (
    RESPONSES,
    ConfirmPreagreementArgs,
    LookupOrderArgs,
    ModelTools,
    ProposeQuoteArgs,
    QuotedComponent,
    ReportIncidentArgs,
    VerifyCallerArgs,
)
from app.tools.parse import Ambiguous, parse_amount, parse_date
from tests.fakes import InMemoryStore, RecordingNotifier

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
EQUIPMENT = "40-foot container chassis"


def now() -> datetime:
    return NOW


class World:
    """One order under a 9,000 USD mandate, two carriers, and the tool surface over them."""

    def __init__(self, *, notifier_succeeds: bool = True) -> None:
        self.store = InMemoryStore()
        self.notifier = RecordingNotifier(succeed=notifier_succeeds)
        self.ledger = CallLedger(self.store, now=now)
        self.commitments = CommitmentCoordinator(self.store, self.notifier, now=now)
        self.tools = ModelTools(
            self.store, now=now, ledger=self.ledger, commitments=self.commitments
        )

        self.store.add_carrier(
            Carrier(id="carrier-1", name="Transportes del Pacifico", phone="+523312345678")
        )
        self.store.add_carrier(
            Carrier(id="carrier-2", name="Fletes Jalisco", phone="+523399999999")
        )
        self.store.add_order(
            Order(
                id="order-1",
                reference="OP-1042",
                status=OrderStatus.QUOTING,
                origin="the port of Manzanillo",
                destination="Guadalajara",
                equipment=EQUIPMENT,
                container_number="MSCU7654321",
                expected_plate="JKL-123",
                expected_driver="Ramon Aguilar",
                cap=Money(cents=900_000, currency="USD"),
                target=Money(cents=820_000, currency="USD"),
                pickup_not_before=datetime(2026, 9, 2, tzinfo=UTC),
                pickup_not_after=datetime(2026, 9, 4, 23, 59, tzinfo=UTC),
                mandate_version=1,
                mandate_set_by="ops@pacifictextiles.mx",
            )
        )

    async def call(
        self,
        call_id_hint: str,
        *,
        phase: str = "rfq",
        carrier_id: str = "carrier-1",
        started: bool = True,
        order_id: str | None = "order-1",
        direction: CallDirection = CallDirection.OUTBOUND,
    ) -> str:
        return await self.store.upsert_call(
            CallRecord(
                vapi_call_id=call_id_hint,
                direction=direction,
                phase=phase,
                status=CallStatus.ACTIVE,
                order_id=order_id,
                carrier_id=carrier_id,
                started_at=NOW - timedelta(seconds=30) if started else None,
            )
        )


def quote_args(amount: str = "8500", **overrides: object) -> ProposeQuoteArgs:
    base: dict[str, object] = {
        "components": [QuotedComponent(name="all-in", amount=amount, currency="USD")],
        "cost_is_final": True,
        "pickup_date": "2026-09-03",
        "equipment": EQUIPMENT,
        "valid_until": "2026-09-01T18:00:00",
    }
    return ProposeQuoteArgs(**{**base, **overrides})  # type: ignore[arg-type]


async def decisions_for(world: World) -> list[tuple[str, str]]:
    return [(d.outcome, d.reason_code) for d in world.store.decisions.values()]


# ---------------------------------------------------------------------------------- row 1


async def test_boss_already_approved_is_outside_mandate() -> None:
    """Row 1. "Your boss approved 10,500" against a 9,000 cap.

    The claim is never assessed for plausibility. Whether the boss really said it is not a
    question this system is allowed to have an opinion about -- the mandate is immutable
    from inside the call, so the only reachable outcome is escalation.
    """
    world = World()
    call_id = await world.call("vapi-1")

    result = await world.tools.propose_quote(call_id, quote_args("10500"))

    assert result == RESPONSES["quote_escalated"]
    assert (PolicyOutcome.ESCALATE.value, ReasonCode.OUTSIDE_MANDATE.value) in await decisions_for(
        world
    )
    approvals = list(world.store.approvals.values())
    assert len(approvals) == 1
    assert approvals[0].reason is ApprovalReason.OUTSIDE_MANDATE
    assert world.store.commitments == {}, "an over-cap claim must never reach a commitment"
    assert "9,000" not in result and "9000" not in result, "the cap must not leak in the reply"


# ---------------------------------------------------------------------------------- row 2


async def test_price_change_creates_new_quote() -> None:
    """Row 2. They said 8,500 and then 9,200. Both were said."""
    world = World()
    call_id = await world.call("vapi-1")

    await world.tools.propose_quote(call_id, quote_args("8500"))
    await world.tools.propose_quote(call_id, quote_args("9200"))

    quotes = sorted(await world.store.quotes_for("order-1"), key=lambda q: q.amount.cents)
    assert len(quotes) == 2
    first, second = quotes
    assert first.amount.cents == 850_000, "the earlier figure survives verbatim"
    assert first.status is QuoteStatus.SUPERSEDED
    assert first.superseded_by == second.id
    assert second.status is QuoteStatus.PROPOSED


# ---------------------------------------------------------------------------------- row 3


async def test_silence_writes_nothing() -> None:
    """Row 3. The counterparty goes quiet and the call ends. Silence is never assent."""
    world = World()
    call_id = await world.call("vapi-1")

    # No tool fires -- that is what silence *is* at this layer. The call simply ends.
    await world.ledger.finalize(
        (await world.store.call(call_id)).model_copy(  # type: ignore[union-attr]
            update={"status": CallStatus.ENDED, "ended_reason": "silence-timed-out"}
        ),
        "vapi-1:end-of-call-report",
    )

    assert await world.store.quotes_for("order-1") == []
    assert world.store.commitments == {}
    assert world.store.decisions == {}


# ---------------------------------------------------------------------------------- row 4


async def test_refusal_ends_rfq_cleanly() -> None:
    """Row 4. "We don't serve that lane." Recorded; the order is untouched."""
    world = World()
    call_id = await world.call("vapi-1")

    result = await world.tools.report_incident(
        call_id,
        ReportIncidentArgs(subject=IncidentSubject.OTHER, detail="we do not serve that lane"),
    )

    assert result == RESPONSES["incident_recorded"]
    order = await world.store.order("order-1")
    assert order is not None and order.status is OrderStatus.QUOTING, "the market continues"
    assert world.store.commitments == {}


# ---------------------------------------------------------------------------------- row 5


async def test_above_cap_offer_never_commits() -> None:
    """Row 5. An inbound "today only" offer at 9,800. Urgency is not authority."""
    world = World()
    call_id = await world.call("vapi-in", phase="inbound", direction=CallDirection.INBOUND)

    result = await world.tools.propose_quote(call_id, quote_args("9800"))

    assert result == RESPONSES["quote_escalated"]
    assert world.store.commitments == {}
    assert (PolicyOutcome.ESCALATE.value, ReasonCode.OUTSIDE_MANDATE.value) in await decisions_for(
        world
    )


# ---------------------------------------------------------------------------------- row 6


async def test_ambiguous_amount_asks_and_writes_nothing() -> None:
    """Row 6. "Eight five" is 8,500 or 85,000, and nothing in the utterance chooses.

    The strong assertion is the second one. Asking is not enough on its own: a quotes row
    written "for the record" would make an unreadable figure look like a fact about this
    order, and the ranking would later pick it up.
    """
    world = World()
    call_id = await world.call("vapi-1")

    result = await world.tools.propose_quote(call_id, quote_args("eight five"))

    assert result == RESPONSES["amount_unclear"]
    assert await world.store.quotes_for("order-1") == []
    assert world.store.decisions == {}
    assert world.store.events == {}


# ---------------------------------------------------------------------------------- row 7


async def test_weekday_is_not_a_date() -> None:
    """Row 7. "Thursday" is not a calendar date until somebody reads one back."""
    world = World()
    call_id = await world.call("vapi-1")

    result = await world.tools.propose_quote(call_id, quote_args(pickup_date="Thursday"))

    assert result == RESPONSES["date_unclear"]
    assert await world.store.quotes_for("order-1") == []


# ---------------------------------------------------------------------------------- row 8


async def test_contradiction_keeps_both_rows() -> None:
    """Row 8. Two incompatible figures are two facts, never one corrected fact."""
    world = World()
    call_id = await world.call("vapi-1")

    await world.tools.propose_quote(call_id, quote_args("8500"))
    await world.tools.propose_quote(call_id, quote_args("8500", cost_is_final=False))

    quotes = await world.store.quotes_for("order-1")
    assert len(quotes) == 2
    assert {q.cost_is_final for q in quotes} == {True, False}
    assert any(q.status is QuoteStatus.SUPERSEDED for q in quotes)


# ---------------------------------------------------------------------------------- row 9


async def test_confirm_outside_award_phase_is_refused() -> None:
    """Row 9. RFQ and AWARD are separate phases (invariant 5).

    Several carriers may hold live offers at once; only the one call we opened to close may
    close. A confirmation arriving on an rfq call is the model talking itself into a phase
    it was never put in.
    """
    world = World()
    rfq_call = await world.call("vapi-1", phase="rfq")
    await world.tools.propose_quote(rfq_call, quote_args("8500"))
    quote_id = next(iter(world.store.quotes))

    result = await world.tools.confirm_preagreement(
        rfq_call,
        ConfirmPreagreementArgs(quote_id=quote_id, carrier_confirmed_exact_recap=True),
    )

    assert result == RESPONSES["preagreement_refused"]
    assert world.store.commitments == {}
    assert (PolicyOutcome.DENY.value, ReasonCode.CONFLICTING_STATE.value) in await decisions_for(
        world
    )


# --------------------------------------------------------------------------------- row 10


async def test_failed_recap_leaves_commitment_unpromoted() -> None:
    """Row 10. A recap that did not leave means there was no commitment.

    Not a defective commitment -- none at all. And it is never re-sent: a send whose outcome
    is unknown may already have reached the carrier, and a second one is a second booking.
    """
    world = World(notifier_succeeds=False)
    award_call = await world.call("vapi-award", phase="award")
    await world.tools.propose_quote(award_call, quote_args("8500"))
    quote_id = next(iter(world.store.quotes))
    await world.tools.confirm_preagreement(
        award_call,
        ConfirmPreagreementArgs(quote_id=quote_id, carrier_confirmed_exact_recap=True),
    )
    commitment_id = next(iter(world.store.commitments))

    result = await world.commitments.send_recap_and_promote(
        commitment_id,
        OutboundMessage(
            channel=NotificationChannel.EMAIL, to_address="ops@example.com", body="recap"
        ),
    )

    assert result.state is CommitmentState.RECAP_SENT
    assert result.state is not CommitmentState.COMMITTED
    stored = await world.store.commitment(commitment_id)
    assert stored is not None and stored.state is CommitmentState.RECAP_SENT
    order = await world.store.order("order-1")
    assert order is not None and order.status is not OrderStatus.BOOKED
    assert len(world.notifier.sent) == 1, "a failed recap is never retried"
    assert any(a.reason is ApprovalReason.POLICY_FAILURE for a in world.store.approvals.values())


async def test_a_delivered_recap_is_what_promotes_a_commitment() -> None:
    """The other half of row 10: the gate opens only when the recap actually left."""
    world = World(notifier_succeeds=True)
    award_call = await world.call("vapi-award", phase="award")
    await world.tools.propose_quote(award_call, quote_args("8500"))
    quote_id = next(iter(world.store.quotes))
    await world.tools.confirm_preagreement(
        award_call,
        ConfirmPreagreementArgs(quote_id=quote_id, carrier_confirmed_exact_recap=True),
    )
    commitment_id = next(iter(world.store.commitments))

    result = await world.commitments.send_recap_and_promote(
        commitment_id,
        OutboundMessage(
            channel=NotificationChannel.EMAIL, to_address="ops@example.com", body="recap"
        ),
    )

    assert result.state is CommitmentState.COMMITTED
    order = await world.store.order("order-1")
    assert order is not None and order.status is OrderStatus.BOOKED
    assert world.store.deliveries[0][1].status is DeliveryStatus.SENT


# --------------------------------------------------------------------------------- row 11


async def test_missing_anchor_is_not_committed() -> None:
    """Row 11. Nothing binds on an audio offset we did not measure.

    The call never reported a start, so there is no instant in the recording to point at.
    A quote can still be recorded -- it is a record of what somebody said -- but a
    commitment is exactly the thing that cannot rest on unanchored evidence.
    """
    world = World()
    award_call = await world.call("vapi-award", phase="award", started=False)
    await world.tools.propose_quote(award_call, quote_args("8500"))
    quote_id = next(iter(world.store.quotes))

    result = await world.tools.confirm_preagreement(
        award_call,
        ConfirmPreagreementArgs(quote_id=quote_id, carrier_confirmed_exact_recap=True),
    )

    assert result == RESPONSES["preagreement_refused"]
    assert world.store.commitments == {}
    assert (PolicyOutcome.DENY.value, ReasonCode.EVIDENCE_MISSING.value) in await decisions_for(
        world
    )


async def test_a_yes_without_an_exact_recap_is_not_evidence() -> None:
    """Also row 11: a "sure" in reply to five terms is not a yes to five terms."""
    world = World()
    award_call = await world.call("vapi-award", phase="award")
    await world.tools.propose_quote(award_call, quote_args("8500"))
    quote_id = next(iter(world.store.quotes))

    result = await world.tools.confirm_preagreement(
        award_call,
        ConfirmPreagreementArgs(quote_id=quote_id, carrier_confirmed_exact_recap=False),
    )

    assert result == RESPONSES["preagreement_refused"]
    assert world.store.commitments == {}


# --------------------------------------------------------------------------------- row 12


async def test_webhook_redelivery_is_idempotent() -> None:
    """Row 12. Vapi redelivers. The second delivery is a no-op, not a second row."""
    world = World()
    call_id = await world.call("vapi-1")
    ended = (await world.store.call(call_id)).model_copy(  # type: ignore[union-attr]
        update={"status": CallStatus.ENDED, "ended_reason": "customer-ended-call"}
    )

    first = await world.ledger.finalize(ended, "vapi-1:end-of-call-report")
    second = await world.ledger.finalize(ended, "vapi-1:end-of-call-report")

    assert first == call_id
    assert second is None, "a repeated key must return None so the caller stops"
    assert len(world.store.calls) == 1


async def test_a_repeated_tool_call_writes_one_quote() -> None:
    """Also row 12: the same figure said twice is one fact, not two quotes."""
    world = World()
    call_id = await world.call("vapi-1")

    first = await world.tools.propose_quote(call_id, quote_args("8500"))
    second = await world.tools.propose_quote(call_id, quote_args("8500"))

    assert first == RESPONSES["quote_recorded"]
    assert second == RESPONSES["quote_replayed"]
    assert len(await world.store.quotes_for("order-1")) == 1


# --------------------------------------------------------------------------------- row 13


async def test_internal_failure_writes_no_commitment() -> None:
    """Row 13. Fail closed. A technical failure never degrades into permission.

    The tool server turns this into an HTTP 200 with an error string (Vapi ignores any other
    status code, so a 500 would fail *open*). What is asserted here is the half this layer
    owns: when the store breaks mid-handler, nothing partial is left authorized.
    """
    world = World()
    call_id = await world.call("vapi-1")

    async def explode(*args: object, **kwargs: object) -> str:
        raise RuntimeError("supabase unreachable")

    world.store.add_quote = explode  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await world.tools.propose_quote(call_id, quote_args("8500"))

    assert world.store.commitments == {}
    assert world.store.decisions == {}, "no decision may be recorded for a write that failed"


# --------------------------------------------------------------------------------- row 14


async def test_single_commitment_under_race() -> None:
    """Row 14. Two carriers confirm during awarding. Two open bookings is the worst outcome.

    The refusal comes from the store, not from a check in the handler: two concurrent
    confirmations could both pass an application-level "is the slot free" test on their way
    to writing. In Postgres this is the partial unique index; InMemoryStore models it.
    """
    world = World()
    first_call = await world.call("vapi-a", phase="award", carrier_id="carrier-1")
    second_call = await world.call("vapi-b", phase="award", carrier_id="carrier-2")
    await world.tools.propose_quote(first_call, quote_args("8500"))
    await world.tools.propose_quote(second_call, quote_args("8700"))
    quotes = {q.carrier_id: q.id for q in await world.store.quotes_for("order-1")}

    first = await world.tools.confirm_preagreement(
        first_call,
        ConfirmPreagreementArgs(
            quote_id=str(quotes["carrier-1"]), carrier_confirmed_exact_recap=True
        ),
    )
    second = await world.tools.confirm_preagreement(
        second_call,
        ConfirmPreagreementArgs(
            quote_id=str(quotes["carrier-2"]), carrier_confirmed_exact_recap=True
        ),
    )

    assert first == RESPONSES["preagreement_noted"]
    assert second == RESPONSES["preagreement_refused"]
    live = [c for c in world.store.commitments.values() if c.state is CommitmentState.VERBAL]
    assert len(live) == 1
    assert (PolicyOutcome.DENY.value, ReasonCode.CONFLICTING_STATE.value) in await decisions_for(
        world
    )


# --------------------------------------------------------------------------------- row 15


async def test_spoken_over_cap_amount_is_escalated() -> None:
    """Row 15. Words are not a different code path from digits.

    "Ten thousand five hundred US dollars" has to reach the same refusal as "10500 USD", or
    the way a figure is pronounced becomes a way around the ceiling.
    """
    world = World()
    call_id = await world.call("vapi-1")

    result = await world.tools.propose_quote(
        call_id,
        quote_args(
            components=[
                QuotedComponent(name="all-in", amount="ten thousand five hundred", currency="USD")
            ]
        ),
    )

    assert result == RESPONSES["quote_escalated"]
    quotes = await world.store.quotes_for("order-1")
    assert len(quotes) == 1
    assert quotes[0].amount.cents == 1_050_000, "the spoken figure parsed to the same number"
    assert world.store.commitments == {}


# --------------------------------------------------------------------------------- row 16


async def test_foreign_quote_without_fx_fails_closed() -> None:
    """Row 16. No approved snapshot exists, so there is no rate. Never invent one."""
    world = World()
    call_id = await world.call("vapi-1")

    result = await world.tools.propose_quote(
        call_id,
        quote_args(components=[QuotedComponent(name="all-in", amount="150000", currency="MXN")]),
    )

    assert result == RESPONSES["quote_escalated"]
    assert (
        PolicyOutcome.ESCALATE.value,
        ReasonCode.FX_EVIDENCE_MISSING.value,
    ) in await decisions_for(world)
    assert world.store.commitments == {}


# --------------------------------------------------------------------------------- row 17


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"pickup_date": "2026-09-09"}, ReasonCode.INVALID_WINDOW),
        ({"equipment": "reefer"}, ReasonCode.OUTSIDE_MANDATE),
        ({"valid_until": "2026-08-29T10:00:00"}, ReasonCode.STALE_EVIDENCE),
    ],
    ids=["out-of-window", "wrong-equipment", "already-expired"],
)
async def test_quote_field_mismatch_fails_closed(
    overrides: dict[str, object], expected_reason: ReasonCode
) -> None:
    """Row 17. Each mismatch names itself. Never defaulted, never silently overwritten."""
    world = World()
    call_id = await world.call("vapi-1")

    await world.tools.propose_quote(call_id, quote_args(**overrides))

    assert expected_reason.value in [reason for _, reason in await decisions_for(world)]
    assert world.store.commitments == {}


# --------------------------------------------------------------------------------- row 18


def test_no_tool_result_can_claim_authority() -> None:
    """Row 18. The model can only claim authority using words we hand it.

    This replaces the old outbound token filter, which had somewhere to stand between the
    transcriber and the model. Vapi owns that stream now, so the property moved one layer
    down to the only strings we still control: what a tool returns.
    """
    forbidden = ("approved", "booked", "confirmed", "we have a deal", "locked in")
    # "Recorded. Nothing is booked." is the wording the build plan specifies, and it is the
    # *denial* of the claim, not the claim. The property is about what the model can assert,
    # so the negations are removed first and the remainder must be clean -- which keeps the
    # check honest rather than merely banning a character sequence.
    negations = (
        "nothing is booked",
        "subject to written confirmation",
        "i cannot record that as agreed",
    )

    for name, text in RESPONSES.items():
        lowered = text.lower()
        for negation in negations:
            lowered = lowered.replace(negation, "")
        for word in forbidden:
            assert word not in lowered, f"RESPONSES[{name!r}] hands the model '{word}'"
        assert "\n" not in text, f"RESPONSES[{name!r}] is not a single line"


def test_the_negations_row_18_allows_are_really_negations() -> None:
    """Guards the guard: an allowed phrase that stopped denying would gut the test above.

    Without this, someone could quiet a row 18 failure by adding the offending phrase to the
    negation list, and the property would keep passing while meaning nothing.
    """
    assert RESPONSES["quote_recorded"] == "Recorded. Nothing is booked."
    assert RESPONSES["preagreement_noted"].endswith("subject to written confirmation.")
    assert RESPONSES["preagreement_refused"].startswith("I cannot record that as agreed")


def test_no_tool_result_leaks_a_mandate_figure() -> None:
    """Also row 18. A counterparty can simply ask for the ceiling; it must not be sayable."""
    for name, text in RESPONSES.items():
        for figure in ("9000", "9,000", "8200", "8,200", "cap", "ceiling", "budget"):
            assert figure not in text.lower(), f"RESPONSES[{name!r}] leaks '{figure}'"


async def test_the_refusal_line_is_identical_whatever_the_reason() -> None:
    """A different refusal per reason code is an oracle for probing the mandate."""
    world = World()
    over_cap = await world.call("vapi-a")
    wrong_kit = await world.call("vapi-b")

    first = await world.tools.propose_quote(over_cap, quote_args("10500"))
    second = await world.tools.propose_quote(wrong_kit, quote_args("8500", equipment="reefer"))

    assert first == second == RESPONSES["quote_escalated"]


# --------------------------------------------------------------------------------- row 19


async def test_direct_handoff_request_raises_one_approval() -> None:
    """Row 19. "Quiero hablar con una persona." One approval, and no negotiating past it."""
    world = World()
    call_id = await world.call("vapi-in", phase="inbound", direction=CallDirection.INBOUND)

    result = await world.tools.report_incident(
        call_id,
        ReportIncidentArgs(subject=IncidentSubject.REQUEST, detail="quiero hablar con una persona"),
    )

    assert result == RESPONSES["incident_recorded"]
    approvals = list(world.store.approvals.values())
    assert len(approvals) == 1
    assert approvals[0].kind is ApprovalKind.INCIDENT
    assert world.store.commitments == {}


async def test_a_repeated_handoff_request_does_not_stack_approvals() -> None:
    """Also row 19: asking twice is one request, not two items in a human's inbox."""
    world = World()
    call_id = await world.call("vapi-in", phase="inbound", direction=CallDirection.INBOUND)
    args = ReportIncidentArgs(subject=IncidentSubject.REQUEST, detail="pass me to a person")

    await world.tools.report_incident(call_id, args)
    await world.tools.report_incident(call_id, args)

    assert len(world.store.approvals) == 1


# --------------------------------------------------------------------------------- row 20


HOSTILE = json.loads(
    (Path(__file__).parent / "fixtures" / "hostile" / "utterances.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    "case", HOSTILE["ambiguous_amounts"], ids=lambda c: c["heard"].replace(" ", "-")
)
def test_every_hostile_amount_is_refused(case: dict[str, str]) -> None:
    """The fixture is the suite, not documentation.

    Anything added to fixtures/hostile/utterances.json becomes a test the moment it lands,
    which is the only way a collection of adversarial examples stays honest -- a fixture
    nothing asserts over is a list of things somebody once worried about.
    """
    assert isinstance(parse_amount(case["heard"]), Ambiguous), case["why"]


@pytest.mark.parametrize(
    "case", HOSTILE["unresolved_dates"], ids=lambda c: c["heard"].replace(" ", "-")
)
def test_every_hostile_date_is_refused(case: dict[str, str]) -> None:
    assert isinstance(parse_date(case["heard"], NOW.date()), Ambiguous), case["why"]


async def test_lookup_before_identity_gives_nothing_away() -> None:
    """Row 20. The refusal is the same whether or not the order exists.

    A different answer for a real reference would turn this tool into an oracle: a caller
    could enumerate folios by watching which ones change the wording.
    """
    world = World()
    call_id = await world.call(
        "vapi-in", phase="inbound", direction=CallDirection.INBOUND, order_id=None
    )

    real = await world.tools.lookup_order(call_id, LookupOrderArgs(reference="OP-1042"))
    invented = await world.tools.lookup_order(call_id, LookupOrderArgs(reference="OP-9999"))

    assert real == invented == RESPONSES["identity_required"]
    assert "Manzanillo" not in real and "Guadalajara" not in real


async def test_verification_never_echoes_the_expected_value() -> None:
    """Also row 20: a caller who is told the plate can repeat the plate."""
    world = World()
    call_id = await world.call(
        "vapi-in", phase="inbound", direction=CallDirection.INBOUND, order_id=None
    )

    wrong = await world.tools.verify_caller(
        call_id, VerifyCallerArgs(fact_kind="plate", fact_value="AAA-000")
    )

    assert wrong == RESPONSES["identity_no_match"]
    assert "JKL" not in wrong


async def test_two_matched_facts_unlock_the_order() -> None:
    """The path a legitimate caller actually walks: folio, then plate, then details."""
    world = World()
    call_id = await world.call(
        "vapi-in", phase="inbound", direction=CallDirection.INBOUND, order_id=None
    )

    assert (
        await world.tools.verify_caller(
            call_id, VerifyCallerArgs(fact_kind="reference", fact_value="OP-1042")
        )
        == RESPONSES["identity_match"]
    )
    assert (
        await world.tools.verify_caller(
            call_id, VerifyCallerArgs(fact_kind="plate", fact_value="jkl 123")
        )
        == RESPONSES["identity_match"]
    ), "normalised: punctuation and case are transcription artefacts, not differences"

    detail = await world.tools.lookup_order(call_id, LookupOrderArgs())

    assert "OP-1042" in detail
    call = await world.store.call(call_id)
    assert call is not None and call.identity_verified is True
