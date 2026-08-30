"""The market: who gets called, how the comparison reads, and the single-award lock.

Everything runs against InMemoryStore and FakeCallPlacer. Nothing here dials.

OWNER: Track E.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain import (
    Approval,
    ApprovalKind,
    ApprovalReason,
    ApprovalStatus,
    AwardConflict,
    CallPhase,
    CallReport,
    Carrier,
    Money,
    Order,
    OrderStatus,
    PolicyOutcome,
    QuoteRow,
    QuoteStatus,
    ReasonCode,
)
from app.tools.market import Market
from tests.fakes import InMemoryStore

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
EQUIPMENT = "40-foot container chassis"


def now() -> datetime:
    return NOW


def order(**overrides: object) -> Order:
    base: dict[str, object] = {
        "id": "order-1",
        "reference": "OP-1042",
        "status": OrderStatus.RECEIVED,
        "origin": "the port of Manzanillo",
        "destination": "Guadalajara",
        "equipment": EQUIPMENT,
        "cap": Money(cents=900_000, currency="USD"),
        "target": Money(cents=820_000, currency="USD"),
        "pickup_not_before": datetime(2026, 9, 2, tzinfo=UTC),
        "pickup_not_after": datetime(2026, 9, 4, 23, 59, tzinfo=UTC),
        "mandate_version": 1,
        "mandate_set_by": "ops@pacifictextiles.mx",
    }
    return Order(**{**base, **overrides})  # type: ignore[arg-type]


def quote(carrier_id: str, cents: int, **overrides: object) -> QuoteRow:
    base: dict[str, object] = {
        "order_id": "order-1",
        "carrier_id": carrier_id,
        "call_id": f"call-{carrier_id}",
        "anchor_ms": 11_200,
        "amount": Money(cents=cents, currency="USD"),
        "components": [{"name": "all-in", "amount": str(cents / 100), "currency": "USD"}],
        "cost_is_final": True,
        "pickup_at": datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
        "equipment": EQUIPMENT,
        "valid_until": NOW + timedelta(hours=6),
    }
    return QuoteRow(**{**base, **overrides})  # type: ignore[arg-type]


def seeded(carriers: int = 3) -> tuple[InMemoryStore, Market]:
    store = InMemoryStore()
    for n in range(1, carriers + 1):
        store.add_carrier(Carrier(id=f"carrier-{n}", name=f"Carrier {n}", phone=f"+5233000000{n}"))
    store.add_order(order())
    return store, Market(store, now=now)


# ---------------------------------------------------------------------------------- plan_rfq


async def test_plan_rfq_creates_one_call_per_carrier() -> None:
    store, market = seeded()

    plans = await market.plan_rfq(order(), 3)

    assert len(plans) == 3
    assert len(store.calls) == 3
    assert {p.to_number for p in plans} == {"+52330000001", "+52330000002", "+52330000003"}
    assert all(call.phase == CallPhase.RFQ.value for call in store.calls.values())


async def test_the_frozen_context_carries_the_market_as_of_dial_time() -> None:
    """The first call negotiates with nothing behind it; a later one has numbers."""
    store, market = seeded()
    await store.add_quote(quote("carrier-1", 850_000))

    plans = await market.plan_rfq(order(), 3)

    assert plans[0].context["quotes_in_hand"] == 1
    assert plans[0].context["best_rate_so_far"] == "8500"


async def test_the_context_carries_the_mandate_pickup_window() -> None:
    """It carried none, and the agent invented one on a live call.

    With ``pickup_window`` unset the agent had no date to quote against and improvised
    "pickup on or around April thirtieth" against a mandate of 2-4 September -- then policy
    denied its own quote for invalid_window. The grammar matches
    ``prompts._runtime_pickup_answer`` so the spoken answer to "when?" is read, not composed.
    """
    _store, market = seeded()

    plans = await market.plan_rfq(order(), 3)

    assert plans[0].context["pickup_window"] == "between September 2 and September 4, 2026"


async def test_a_window_spanning_two_months_still_reads_as_a_window() -> None:
    _store, market = seeded()

    plans = await market.plan_rfq(
        order(
            pickup_not_before=datetime(2026, 8, 30, tzinfo=UTC),
            pickup_not_after=datetime(2026, 9, 2, tzinfo=UTC),
        ),
        3,
    )

    assert plans[0].context["pickup_window"] == "between August 30 and September 2, 2026"


async def test_no_mandate_window_carries_no_window_rather_than_a_guess() -> None:
    """Absent stays absent. A rendered empty window is an invitation to fill it in."""
    _store, market = seeded()

    plans = await market.plan_rfq(order(pickup_not_before=None, pickup_not_after=None), 3)

    assert plans[0].context["pickup_window"] is None


async def test_the_context_never_carries_a_figure_the_carrier_did_not_give() -> None:
    """The ceiling is in the prompt so the agent can negotiate; policy still decides."""
    _store, market = seeded()

    plans = await market.plan_rfq(order(), 3)

    assert plans[0].context["price_ceiling"] == "9000"
    assert plans[0].context["reference"] == "OP-1042"


async def test_the_rfq_context_carries_the_pickup_window_we_need() -> None:
    """It shipped without one, so the agent had no date to state and asked the carrier for
    theirs -- which is what a seller does, not a buyer. The mandate owns the window."""
    _store, market = seeded()

    plans = await market.plan_rfq(order(), 3)

    assert plans[0].context["pickup_window"] == "between September 2 and September 4, 2026"


async def test_a_thin_market_escalates_instead_of_dialling() -> None:
    """Fewer than three is not a market to push through; it is a market with no comparison."""
    store, market = seeded(carriers=2)

    plans = await market.plan_rfq(order(), 3)

    assert plans == []
    assert store.calls == {}
    approvals = list(store.approvals.values())
    assert len(approvals) == 1
    assert approvals[0].reason is ApprovalReason.NO_ELIGIBLE_CANDIDATE


async def test_carriers_not_on_file_are_never_dialled() -> None:
    """Volta onboards nobody by phone."""
    store, market = seeded()
    store.add_carrier(
        Carrier(id="carrier-x", name="Unknown Hauliers", phone="+52330000009", is_on_file=False)
    )

    plans = await market.plan_rfq(order(), 5)

    assert "carrier-x" not in {p.carrier.id for p in plans}


# -------------------------------------------------------------------------------------- rank


async def test_rank_keeps_the_losers_and_their_reason_codes() -> None:
    """A comparison listing only the winner cannot be audited."""
    store, market = seeded()
    await store.add_quote(quote("carrier-1", 880_000))
    await store.add_quote(quote("carrier-2", 810_000))
    await store.add_quote(quote("carrier-3", 1_050_000))

    comparison = await market.rank(order())

    assert len(comparison.entries) == 3
    assert all(entry.reason_code for entry in comparison.entries)
    winner = next(e for e in comparison.entries if e.is_winner)
    assert winner.amount.cents == 810_000
    over_cap = next(e for e in comparison.entries if e.amount.cents == 1_050_000)
    assert over_cap.outcome == PolicyOutcome.ESCALATE.value
    assert over_cap.reason_code == ReasonCode.OUTSIDE_MANDATE.value
    assert over_cap.is_winner is False


async def test_rank_records_a_decision_per_quote() -> None:
    """The refusals are the auditable half. "Why not that one" needs a row to point at."""
    store, market = seeded()
    await store.add_quote(quote("carrier-1", 810_000))
    await store.add_quote(quote("carrier-2", 1_050_000))

    await market.rank(order())

    assert len(store.decisions) == 2
    assert {d.cap_at_decision_cents for d in store.decisions.values()} == {900_000}


async def test_rank_ignores_a_superseded_quote() -> None:
    """The row survives as evidence but it was replaced by a later utterance."""
    store, market = seeded()
    old = await store.add_quote(quote("carrier-1", 810_000))
    new = await store.add_quote(quote("carrier-1", 880_000))
    await store.supersede_quote(old, new)

    comparison = await market.rank(order())

    assert [e.quote_id for e in comparison.entries] == [new]


async def test_a_market_where_everything_is_over_the_cap_has_no_winner() -> None:
    store, market = seeded()
    await store.add_quote(quote("carrier-1", 1_050_000))
    await store.add_quote(quote("carrier-2", 1_100_000))

    comparison = await market.rank(order())

    assert comparison.winner_quote_id is None
    assert len(comparison.entries) == 2, "the refusals still have to be shown to a human"


async def test_ranking_is_stable_when_run_twice() -> None:
    """The same market ranked twice picks the same winner, or the comparison is not evidence."""
    store, market = seeded()
    await store.add_quote(quote("carrier-1", 850_000))
    await store.add_quote(quote("carrier-2", 850_000, pickup_at=datetime(2026, 9, 2, tzinfo=UTC)))

    first = await market.rank(order())
    second = await market.rank(order())

    assert first.winner_quote_id == second.winner_quote_id


async def test_rank_on_an_order_with_no_mandate_yields_an_empty_comparison() -> None:
    """Nothing is authorized under a mandate that was never granted."""
    store = InMemoryStore()
    store.add_order(Order(id="order-1", reference="OP-1042"))
    market = Market(store, now=now)

    comparison = await market.rank(await store.order("order-1"))  # type: ignore[arg-type]

    assert comparison.entries == []
    assert comparison.winner_quote_id is None


# ---------------------------------------------------------------------- the approval and award


async def test_request_award_approval_hands_over_the_whole_comparison() -> None:
    store, market = seeded()
    await store.add_quote(quote("carrier-1", 810_000))
    await store.add_quote(quote("carrier-2", 1_050_000))
    comparison = await market.rank(order())

    approval = await market.request_award_approval(order(), comparison)

    assert approval.kind is ApprovalKind.AWARD_APPROVAL
    assert approval.reason is ApprovalReason.AWARD_SELECTED
    assert len(approval.context["entries"]) == 2, "the loser travels with the request"
    order_row = await store.order("order-1")
    assert order_row is not None and order_row.status is OrderStatus.AWAITING_APPROVAL


async def test_renegotiation_context_uses_the_first_call_report_as_guidance() -> None:
    store, market = seeded()
    quote_id = await store.add_quote(quote("carrier-1", 850_000))
    await store.save_report(
        CallReport(
            call_id="call-carrier-1",
            summary="Quoted 8,500 USD but objected to the pickup hour.",
            objections=["Pickup is too early"],
            conditions=["Subject to chassis availability"],
        )
    )
    comparison = await market.rank(order())

    plans = await market.plan_renegotiation(order(), comparison)

    assert len(plans) == 1
    assert plans[0].context["phase"] == CallPhase.RENEGOTIATION.value
    assert "Pickup is too early" in str(plans[0].context["agreed_terms"])
    assert quote_id in {entry.quote_id for entry in comparison.entries}


async def test_an_award_needs_an_approved_approval() -> None:
    """Nothing else authorizes one. An open request is not a decision."""
    store, market = seeded()
    quote_id = await store.add_quote(quote("carrier-1", 810_000))
    pending = Approval(
        id="approval-1",
        order_id="order-1",
        kind=ApprovalKind.AWARD_APPROVAL,
        reason=ApprovalReason.AWARD_SELECTED,
        context={"winner_quote_id": quote_id, "mandate_version": 1},
        status=ApprovalStatus.OPEN,
    )

    with pytest.raises(ValueError, match="approved approval"):
        await market.award(order(), pending)


async def test_a_granted_award_accepts_exactly_one_quote() -> None:
    store, market = seeded()
    quote_id = await store.add_quote(quote("carrier-1", 810_000))
    approved = Approval(
        id="approval-1",
        order_id="order-1",
        kind=ApprovalKind.AWARD_APPROVAL,
        reason=ApprovalReason.AWARD_SELECTED,
        context={"winner_quote_id": quote_id, "mandate_version": 1},
        status=ApprovalStatus.APPROVED,
    )

    awarded = await market.award(order(), approved)

    assert awarded == quote_id
    assert store.quotes[quote_id].status is QuoteStatus.ACCEPTED
    order_row = await store.order("order-1")
    assert order_row is not None and order_row.status is OrderStatus.AWARDING


async def test_an_accepted_award_plans_one_exact_confirmation_call() -> None:
    store, market = seeded()
    quote_id = await store.add_quote(quote("carrier-1", 810_000, status=QuoteStatus.ACCEPTED))

    plans = await market.plan_award(order(), quote_id)

    assert len(plans) == 1
    assert plans[0].carrier.id == "carrier-1"
    assert plans[0].context["phase"] == CallPhase.AWARD.value
    assert "8,100.00 USD" in str(plans[0].context["agreed_terms"])
    call = await store.call(plans[0].call_id)
    assert call is not None and call.phase == CallPhase.AWARD.value
    assert await market.plan_award(order(), quote_id) == [], "a replay never dials twice"


async def test_a_failed_award_dial_can_plan_one_safe_retry() -> None:
    store, market = seeded()
    quote_id = await store.add_quote(quote("carrier-1", 810_000, status=QuoteStatus.ACCEPTED))
    first = await market.plan_award(order(), quote_id)
    await market.mark_dial_round_failed(first)

    retry = await market.plan_award(order(), quote_id)

    assert len(retry) == 1
    assert retry[0].call_id != first[0].call_id


async def test_a_second_award_conflicts_and_raises_an_approval() -> None:
    """Two open bookings is the worst failure in the brief.

    The database refuses the second, and the answer is a person -- never a retry that
    eventually picks one.
    """
    store, market = seeded()
    first_id = await store.add_quote(quote("carrier-1", 810_000))
    second_id = await store.add_quote(quote("carrier-2", 830_000))

    def approval_for(quote_id: str) -> Approval:
        return Approval(
            id=f"approval-{quote_id}",
            order_id="order-1",
            kind=ApprovalKind.AWARD_APPROVAL,
            reason=ApprovalReason.AWARD_SELECTED,
            context={"winner_quote_id": quote_id, "mandate_version": 1},
            status=ApprovalStatus.APPROVED,
        )

    await market.award(order(), approval_for(first_id))
    with pytest.raises(AwardConflict):
        await market.award(order(), approval_for(second_id))

    accepted = [q for q in store.quotes.values() if q.status is QuoteStatus.ACCEPTED]
    assert len(accepted) == 1
    assert accepted[0].id == first_id
    assert any(a.reason is ApprovalReason.CONFLICTING_INFORMATION for a in store.approvals.values())


async def test_a_market_nobody_quoted_is_an_escalation_not_an_award() -> None:
    """OP-MIA-0002 reached the Comparison stage with three unanswered calls.

    The table was empty, the recommendation was "no eligible candidate", and the only
    button offered was Approve -- which could never do anything but refuse itself. An empty
    market is not a comparison a person can decide; it is a market that has to run again.
    """
    store, market = seeded()

    approval = await market.request_award_approval(order(), await market.rank(order()))

    assert approval.kind is ApprovalKind.ESCALATION
    assert approval.reason is ApprovalReason.NO_ELIGIBLE_CANDIDATE
    saved = await store.order("order-1")
    assert saved is not None
    # Back where the next action is "open the market", not "approve nothing".
    assert saved.status is OrderStatus.RECEIVED


async def test_quotes_that_all_miss_the_ceiling_still_reach_a_person_as_an_award() -> None:
    """The other half of the pair. Carriers quoted and none cleared the cap: that is a real
    comparison, and raising the ceiling or walking away are both decisions a person makes
    from it."""
    store, market = seeded()
    await store.add_quote(quote("carrier-1", 1_500_000))

    approval = await market.request_award_approval(order(), await market.rank(order()))

    assert approval.kind is ApprovalKind.AWARD_APPROVAL
    assert approval.reason is ApprovalReason.NO_ELIGIBLE_CANDIDATE
    saved = await store.order("order-1")
    assert saved is not None and saved.status is OrderStatus.AWAITING_APPROVAL
