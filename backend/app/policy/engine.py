"""Pure reference monitor. No model, network, persistence, or ambient clock access.

Every function here is total: it always returns a decision, and the default is deny. A
technical failure must never degrade into permission, so there is no code path that raises
past a caller and leaves "allowed" as the effective outcome.

``now`` is a parameter, never ``datetime.now()``. A decision that depends on an ambient
clock cannot be reproduced from its stored inputs, and a decision that cannot be reproduced
cannot be defended.

Ported from ``nextwave/backend/app/policy/engine.py``. OWNER: Track A.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from app.domain import (
    CostEvidence,
    FxSnapshot,
    Mandate,
    PolicyDecision,
    PolicyOutcome,
    QuoteProposal,
    ReasonCode,
)

__all__ = ["evaluate_quote", "require_preagreement_evidence", "select_best"]

_CENT = Decimal("0.01")


def _decision(
    mandate: Mandate,
    proposal: QuoteProposal,
    outcome: PolicyOutcome,
    reason: ReasonCode,
    cost: CostEvidence | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        outcome=outcome,
        reason=reason,
        mandate_id=mandate.mandate_id,
        mandate_version=mandate.version,
        proposal_id=proposal.proposal_id,
        cost=cost,
    )


def _pickup_is_inside(mandate: Mandate, proposal: QuoteProposal) -> bool:
    """Is this pickup within the granted window?

    Two comparisons, because a carrier can offer two different kinds of thing. "The fourth,
    at two in the afternoon" is a moment, and a moment is judged to the minute. "The fourth"
    is a *day*, and judging a day to the minute means judging an hour nobody said: a bare
    date resolves to midnight, so a carrier offering the first day of the window was refused
    for being eight hours early to a window their own utterance never addressed.

    The alternative -- resolving a bare date to some plausible hour -- is worse. It invents a
    term, and that invented hour then travels into the recap the carrier is asked to confirm.
    Better to keep the date exactly as spoken and compare it as what it is.

    Both sides are read in UTC. That holds while the window is business hours somewhere in
    the Americas, where 08:00-18:00 local stays inside one UTC day; a window whose local day
    straddles midnight UTC would need the mandate to carry its timezone, and policy takes no
    configuration, so this is stated rather than silently assumed.
    """
    if proposal.pickup_is_date_only:
        return (
            mandate.pickup_not_before.date()
            <= proposal.pickup_at.date()
            <= mandate.pickup_not_after.date()
        )
    return mandate.pickup_not_before <= proposal.pickup_at <= mandate.pickup_not_after


def evaluate_quote(
    mandate: Mandate,
    proposal: QuoteProposal,
    fx: Mapping[str, FxSnapshot],
    *,
    now: datetime,
    max_fx_age: timedelta = timedelta(hours=2),
) -> PolicyDecision:
    """Evaluate one proposal against an immutable mandate and evidence snapshots.

    Order of checks matters and is part of the contract: mandate match, cost finality,
    validity, pickup window, equipment, then the all-in conversion against the ceiling.

    The order is not cosmetic. A proposal for the wrong operation must be rejected as a
    mismatch and not as an over-cap quote, because the reason code is what a human reads
    when they ask why -- and "outside_mandate" would send them looking at the ceiling of an
    order that was never involved.
    """
    if proposal.operation_id != mandate.operation_id:
        return _decision(mandate, proposal, PolicyOutcome.DENY, ReasonCode.MANDATE_MISMATCH)
    if not proposal.cost_is_final:
        return _decision(mandate, proposal, PolicyOutcome.ESCALATE, ReasonCode.INCOMPLETE_COST)
    if proposal.valid_until < now:
        return _decision(mandate, proposal, PolicyOutcome.DENY, ReasonCode.STALE_EVIDENCE)
    if not _pickup_is_inside(mandate, proposal):
        return _decision(mandate, proposal, PolicyOutcome.DENY, ReasonCode.INVALID_WINDOW)
    if proposal.equipment not in mandate.allowed_equipment:
        return _decision(mandate, proposal, PolicyOutcome.DENY, ReasonCode.OUTSIDE_MANDATE)

    totals: dict[str, Decimal] = {}
    usd = Decimal(0)
    snapshot_ids: list[str] = []
    for component in proposal.components:
        totals[component.currency] = totals.get(component.currency, Decimal(0)) + component.amount
        if component.currency == "USD":
            usd += component.amount
            continue
        snapshot = fx.get(component.currency)
        if snapshot is None or snapshot.quote_currency != component.currency:
            return _decision(
                mandate, proposal, PolicyOutcome.ESCALATE, ReasonCode.FX_EVIDENCE_MISSING
            )
        if snapshot.observed_at > now or now - snapshot.observed_at > max_fx_age:
            return _decision(mandate, proposal, PolicyOutcome.DENY, ReasonCode.STALE_EVIDENCE)
        if mandate.fx_margin_bps is None:
            return _decision(
                mandate, proposal, PolicyOutcome.ESCALATE, ReasonCode.FX_EVIDENCE_MISSING
            )
        usd += component.amount * snapshot.usd_per_unit
        snapshot_ids.append(snapshot.snapshot_id)

    margin_bps = mandate.fx_margin_bps or 0
    unbuffered = usd.quantize(_CENT, rounding=ROUND_CEILING)
    buffered = (usd * (Decimal(1) + Decimal(margin_bps) / Decimal(10_000))).quantize(
        _CENT, rounding=ROUND_CEILING
    )
    evidence = CostEvidence(
        original_totals=totals,
        unbuffered_usd=unbuffered,
        margin_bps=margin_bps,
        buffered_usd=buffered,
        fx_snapshot_ids=tuple(sorted(set(snapshot_ids))),
    )
    if buffered > mandate.max_all_in_usd:
        return _decision(
            mandate, proposal, PolicyOutcome.ESCALATE, ReasonCode.OUTSIDE_MANDATE, evidence
        )
    return _decision(mandate, proposal, PolicyOutcome.ALLOW, ReasonCode.ALLOWED, evidence)


def require_preagreement_evidence(
    mandate: Mandate, proposal: QuoteProposal, decision: PolicyDecision
) -> PolicyDecision:
    """A model-interpreted yes is not enough without exact, anchored recap evidence.

    Layered on top of ``evaluate_quote`` rather than folded into it because they answer
    different questions. One asks whether these terms are permitted; this asks whether we
    can prove the counterparty actually assented to them. A quote can pass the first and
    fail the second, and the reason code has to say which.
    """
    if decision.outcome is not PolicyOutcome.ALLOW:
        return decision
    if (
        not proposal.carrier_confirmed_exact_recap
        or proposal.confirmed_at is None
        or proposal.transcript_anchor_ms is None
    ):
        return _decision(mandate, proposal, PolicyOutcome.DENY, ReasonCode.EVIDENCE_MISSING)
    return decision


def select_best(decisions: list[tuple[QuoteProposal, PolicyDecision]]) -> QuoteProposal | None:
    """Lowest eligible all-in cost, with deterministic tie-breaks.

    Deterministic because the comparison has to be reproducible in front of a human who
    asks why this carrier and not that one. Cost, then the earlier pickup, then the earlier
    confirmation, then the proposal id -- the last is arbitrary but total, which is the
    point: no pair of proposals may ever compare equal.
    """
    eligible = [
        (proposal, decision)
        for proposal, decision in decisions
        if decision.outcome is PolicyOutcome.ALLOW and decision.cost is not None
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (
            item[1].cost.buffered_usd,  # type: ignore[union-attr]
            item[0].pickup_at,
            item[0].confirmed_at or datetime.max.replace(tzinfo=item[0].pickup_at.tzinfo),
            item[0].proposal_id,
        ),
    )[0]
