"""The reference monitor, tested directly. No store, no fakes, no I/O.

These are the cheapest tests in the repo and the ones that matter most: every other layer
can be wrong and recoverable, but a policy engine that says ALLOW when it should not is the
failure the whole architecture exists to prevent.

The check *order* is asserted as well as the outcomes. Which reason code comes back is not
cosmetic -- it is what a human reads when they ask why, and a proposal that is wrong in two
ways must name the first one consistently or the explanation is not reproducible.

OWNER: Track A.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain import (
    CommitmentMode,
    CostComponent,
    FxSnapshot,
    Mandate,
    PolicyOutcome,
    QuoteProposal,
    ReasonCode,
)
from app.policy import evaluate_quote, require_preagreement_evidence, select_best

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
PICKUP = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def mandate(**overrides: object) -> Mandate:
    base: dict[str, object] = {
        "mandate_id": "OP-1042:v1",
        "version": 1,
        "owner_id": "ops@pacifictextiles.mx",
        "operation_id": "order-1",
        "max_all_in_usd": Decimal("9000"),
        "pickup_not_before": datetime(2026, 9, 2, tzinfo=UTC),
        "pickup_not_after": datetime(2026, 9, 4, 23, 59, tzinfo=UTC),
        "allowed_equipment": frozenset({"40-foot container chassis"}),
        "commitment_mode": CommitmentMode.HUMAN_ESCALATION,
    }
    return Mandate(**{**base, **overrides})  # type: ignore[arg-type]


def proposal(**overrides: object) -> QuoteProposal:
    base: dict[str, object] = {
        "proposal_id": "p-1",
        "operation_id": "order-1",
        "carrier_id": "carrier-1",
        "carrier_contact_id": "contact-1",
        "components": (CostComponent(name="all-in", amount=Decimal("8500"), currency="USD"),),
        "cost_is_final": True,
        "pickup_at": PICKUP,
        "equipment": "40-foot container chassis",
        "valid_until": NOW + timedelta(hours=6),
        "source_call_id": "call-1",
        "source_event_id": "event-1",
        "transcript_anchor_ms": 11_200,
    }
    return QuoteProposal(**{**base, **overrides})  # type: ignore[arg-type]


# ------------------------------------------------------------------------------ the happy path


def test_a_quote_inside_every_limit_is_allowed() -> None:
    decision = evaluate_quote(mandate(), proposal(), {}, now=NOW)

    assert decision.outcome is PolicyOutcome.ALLOW
    assert decision.reason is ReasonCode.ALLOWED
    assert decision.cost is not None
    assert decision.cost.buffered_usd == Decimal("8500.00")


def test_the_decision_carries_the_mandate_version_it_was_made_under() -> None:
    """Copied by value so raising the cap later cannot rewrite an earlier explanation."""
    decision = evaluate_quote(mandate(version=4), proposal(), {}, now=NOW)

    assert decision.mandate_version == 4
    assert decision.proposal_id == "p-1"


# ------------------------------------------------------------------------------- the refusals


def test_over_the_cap_escalates_rather_than_denies() -> None:
    """A price we cannot authorize is a question for a person, not a dead end."""
    over = proposal(
        components=(CostComponent(name="all-in", amount=Decimal("10500"), currency="USD"),)
    )

    decision = evaluate_quote(mandate(), over, {}, now=NOW)

    assert decision.outcome is PolicyOutcome.ESCALATE
    assert decision.reason is ReasonCode.OUTSIDE_MANDATE
    assert decision.cost is not None, "the evidence has to survive the refusal"
    assert decision.cost.buffered_usd == Decimal("10500.00")


def test_exactly_at_the_cap_is_allowed() -> None:
    """The boundary is inclusive. An off-by-one here is a refusal nobody can explain."""
    at_cap = proposal(
        components=(CostComponent(name="all-in", amount=Decimal("9000"), currency="USD"),)
    )

    assert evaluate_quote(mandate(), at_cap, {}, now=NOW).outcome is PolicyOutcome.ALLOW


def test_one_cent_over_the_cap_is_not() -> None:
    over = proposal(
        components=(CostComponent(name="all-in", amount=Decimal("9000.01"), currency="USD"),)
    )

    assert evaluate_quote(mandate(), over, {}, now=NOW).outcome is PolicyOutcome.ESCALATE


def test_a_total_that_is_not_final_cannot_be_authorized() -> None:
    """ "Plus tolls" means the number on the table is not the number we would pay."""
    decision = evaluate_quote(mandate(), proposal(cost_is_final=False), {}, now=NOW)

    assert decision.outcome is PolicyOutcome.ESCALATE
    assert decision.reason is ReasonCode.INCOMPLETE_COST


def test_an_expired_quote_is_stale_evidence() -> None:
    expired = proposal(valid_until=NOW - timedelta(minutes=1))

    decision = evaluate_quote(mandate(), expired, {}, now=NOW)

    assert decision.outcome is PolicyOutcome.DENY
    assert decision.reason is ReasonCode.STALE_EVIDENCE


def test_a_pickup_outside_the_window_is_refused() -> None:
    late = proposal(pickup_at=datetime(2026, 9, 9, tzinfo=UTC))

    decision = evaluate_quote(mandate(), late, {}, now=NOW)

    assert decision.outcome is PolicyOutcome.DENY
    assert decision.reason is ReasonCode.INVALID_WINDOW


def test_the_wrong_equipment_is_outside_the_mandate() -> None:
    decision = evaluate_quote(mandate(), proposal(equipment="reefer"), {}, now=NOW)

    assert decision.outcome is PolicyOutcome.DENY
    assert decision.reason is ReasonCode.OUTSIDE_MANDATE


def test_a_proposal_for_another_operation_is_a_mismatch() -> None:
    decision = evaluate_quote(mandate(), proposal(operation_id="order-2"), {}, now=NOW)

    assert decision.outcome is PolicyOutcome.DENY
    assert decision.reason is ReasonCode.MANDATE_MISMATCH


# --------------------------------------------------------------------------- the check order


def test_mandate_mismatch_is_reported_before_the_price() -> None:
    """A quote for the wrong order is not an over-cap quote.

    The order of checks is part of the contract. "outside_mandate" here would send a human
    to look at the ceiling of an order that was never involved.
    """
    wrong_and_expensive = proposal(
        operation_id="order-2",
        components=(CostComponent(name="all-in", amount=Decimal("99000"), currency="USD"),),
    )

    assert evaluate_quote(mandate(), wrong_and_expensive, {}, now=NOW).reason is (
        ReasonCode.MANDATE_MISMATCH
    )


def test_an_unfinished_total_is_reported_before_the_window() -> None:
    """We cannot say a price is out of window when we do not yet know the price."""
    both_wrong = proposal(cost_is_final=False, pickup_at=datetime(2026, 9, 9, tzinfo=UTC))

    assert evaluate_quote(mandate(), both_wrong, {}, now=NOW).reason is (ReasonCode.INCOMPLETE_COST)


# ----------------------------------------------------------------------------------- currency


def test_a_foreign_component_without_a_snapshot_escalates() -> None:
    """Never invent a rate. An unconvertible quote is a question, not a conversion."""
    pesos = proposal(
        components=(CostComponent(name="all-in", amount=Decimal("150000"), currency="MXN"),)
    )

    decision = evaluate_quote(mandate(), pesos, {}, now=NOW)

    assert decision.outcome is PolicyOutcome.ESCALATE
    assert decision.reason is ReasonCode.FX_EVIDENCE_MISSING


def test_a_foreign_component_with_a_snapshot_but_no_approved_margin_escalates() -> None:
    """A rate alone is not authority to convert. The margin is a human decision."""
    pesos = proposal(
        components=(CostComponent(name="all-in", amount=Decimal("150000"), currency="MXN"),)
    )
    fx = {
        "MXN": FxSnapshot(
            snapshot_id="fx-1",
            quote_currency="MXN",
            usd_per_unit=Decimal("0.05"),
            observed_at=NOW - timedelta(minutes=5),
            source="test",
        )
    }

    decision = evaluate_quote(mandate(), pesos, fx, now=NOW)

    assert decision.reason is ReasonCode.FX_EVIDENCE_MISSING


def test_a_stale_snapshot_is_refused() -> None:
    pesos = proposal(
        components=(CostComponent(name="all-in", amount=Decimal("150000"), currency="MXN"),)
    )
    fx = {
        "MXN": FxSnapshot(
            snapshot_id="fx-1",
            quote_currency="MXN",
            usd_per_unit=Decimal("0.05"),
            observed_at=NOW - timedelta(days=1),
            source="test",
        )
    }

    decision = evaluate_quote(mandate(fx_margin_bps=500), pesos, fx, now=NOW)

    assert decision.outcome is PolicyOutcome.DENY
    assert decision.reason is ReasonCode.STALE_EVIDENCE


def test_a_converted_quote_carries_its_snapshot_ids() -> None:
    """The conversion has to be reproducible from the row, not from today's rate."""
    pesos = proposal(
        components=(CostComponent(name="all-in", amount=Decimal("100000"), currency="MXN"),)
    )
    fx = {
        "MXN": FxSnapshot(
            snapshot_id="fx-1",
            quote_currency="MXN",
            usd_per_unit=Decimal("0.05"),
            observed_at=NOW - timedelta(minutes=5),
            source="test",
        )
    }

    decision = evaluate_quote(mandate(fx_margin_bps=500), pesos, fx, now=NOW)

    assert decision.outcome is PolicyOutcome.ALLOW
    assert decision.cost is not None
    assert decision.cost.fx_snapshot_ids == ("fx-1",)
    assert decision.cost.unbuffered_usd == Decimal("5000.00")
    assert decision.cost.buffered_usd == Decimal("5250.00"), "500 bps on top of 5,000"


def test_mixed_currencies_keep_their_original_totals() -> None:
    """A run quoted in pesos with tolls in dollars is two facts; flattening loses one."""
    mixed = proposal(
        components=(
            CostComponent(name="linehaul", amount=Decimal("100000"), currency="MXN"),
            CostComponent(name="tolls", amount=Decimal("200"), currency="USD"),
        )
    )
    fx = {
        "MXN": FxSnapshot(
            snapshot_id="fx-1",
            quote_currency="MXN",
            usd_per_unit=Decimal("0.05"),
            observed_at=NOW,
            source="test",
        )
    }

    decision = evaluate_quote(mandate(fx_margin_bps=0), mixed, fx, now=NOW)

    assert decision.cost is not None
    assert decision.cost.original_totals == {"MXN": Decimal("100000"), "USD": Decimal("200")}
    assert decision.cost.unbuffered_usd == Decimal("5200.00")


# -------------------------------------------------------------- the pre-agreement evidence gate


def test_evidence_is_required_even_when_the_price_is_fine() -> None:
    allowed = evaluate_quote(mandate(), proposal(), {}, now=NOW)

    gated = require_preagreement_evidence(mandate(), proposal(), allowed)

    assert gated.outcome is PolicyOutcome.DENY
    assert gated.reason is ReasonCode.EVIDENCE_MISSING


def test_an_exact_recap_with_an_anchor_passes_the_gate() -> None:
    confirmed = proposal(carrier_confirmed_exact_recap=True, confirmed_at=NOW)
    allowed = evaluate_quote(mandate(), confirmed, {}, now=NOW)

    gated = require_preagreement_evidence(mandate(), confirmed, allowed)

    assert gated.outcome is PolicyOutcome.ALLOW


def test_a_confirmation_with_no_anchor_fails_the_gate() -> None:
    """A yes we cannot point at in the recording is a yes we cannot defend."""
    unanchored = proposal(
        carrier_confirmed_exact_recap=True, confirmed_at=NOW, transcript_anchor_ms=None
    )
    allowed = evaluate_quote(mandate(), unanchored, {}, now=NOW)

    assert require_preagreement_evidence(mandate(), unanchored, allowed).reason is (
        ReasonCode.EVIDENCE_MISSING
    )


def test_the_gate_never_upgrades_a_refusal() -> None:
    """It can only ever take permission away. Otherwise it is a second, weaker cap check."""
    over = proposal(
        components=(CostComponent(name="all-in", amount=Decimal("10500"), currency="USD"),),
        carrier_confirmed_exact_recap=True,
        confirmed_at=NOW,
    )
    refused = evaluate_quote(mandate(), over, {}, now=NOW)

    gated = require_preagreement_evidence(mandate(), over, refused)

    assert gated.outcome is PolicyOutcome.ESCALATE
    assert gated.reason is ReasonCode.OUTSIDE_MANDATE


# ---------------------------------------------------------------------------------- selection


def test_the_cheapest_eligible_quote_wins() -> None:
    cheap = proposal(
        proposal_id="p-cheap",
        components=(CostComponent(name="all-in", amount=Decimal("8100"), currency="USD"),),
    )
    dear = proposal(
        proposal_id="p-dear",
        components=(CostComponent(name="all-in", amount=Decimal("8900"), currency="USD"),),
    )
    pairs = [
        (dear, evaluate_quote(mandate(), dear, {}, now=NOW)),
        (cheap, evaluate_quote(mandate(), cheap, {}, now=NOW)),
    ]

    winner = select_best(pairs)

    assert winner is not None
    assert winner.proposal_id == "p-cheap"


def test_a_refused_quote_can_never_win() -> None:
    """Even when it is the only one on the table. No eligible candidate is an answer."""
    over = proposal(
        components=(CostComponent(name="all-in", amount=Decimal("10500"), currency="USD"),)
    )

    assert select_best([(over, evaluate_quote(mandate(), over, {}, now=NOW))]) is None


def test_select_best_on_an_empty_market_is_none() -> None:
    assert select_best([]) is None


def test_a_tie_on_price_breaks_on_the_earlier_pickup() -> None:
    """Deterministic because a human will ask why this one and not that one."""
    later = proposal(proposal_id="p-later", pickup_at=PICKUP + timedelta(days=1))
    earlier = proposal(proposal_id="p-earlier", pickup_at=PICKUP)
    pairs = [
        (later, evaluate_quote(mandate(), later, {}, now=NOW)),
        (earlier, evaluate_quote(mandate(), earlier, {}, now=NOW)),
    ]

    winner = select_best(pairs)

    assert winner is not None
    assert winner.proposal_id == "p-earlier"


def test_a_total_tie_breaks_on_the_proposal_id() -> None:
    """Arbitrary but total. No two proposals may ever compare equal."""
    first = proposal(proposal_id="p-aaa")
    second = proposal(proposal_id="p-bbb")
    pairs = [
        (second, evaluate_quote(mandate(), second, {}, now=NOW)),
        (first, evaluate_quote(mandate(), first, {}, now=NOW)),
    ]

    winner = select_best(pairs)

    assert winner is not None
    assert winner.proposal_id == "p-aaa"


def test_selection_is_stable_across_input_order() -> None:
    """The same market ranked twice must produce the same winner, however it is shuffled."""
    quotes = [
        proposal(
            proposal_id=f"p-{n}",
            components=(CostComponent(name="all-in", amount=Decimal(n), currency="USD"),),
        )
        for n in (8100, 8400, 8900)
    ]
    pairs = [(q, evaluate_quote(mandate(), q, {}, now=NOW)) for q in quotes]

    forward = select_best(pairs)
    backward = select_best(list(reversed(pairs)))

    assert forward is not None and backward is not None
    assert forward.proposal_id == backward.proposal_id == "p-8100"
