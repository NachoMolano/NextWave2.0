"""Multi-carrier strategy: who to call, how they compare, and who wins.

RFQ and AWARD are separate phases on purpose. Several carriers may hold live offers at once;
only one call may close. Two open bookings is the worst failure the brief describes, and the
enforcement is the partial unique index on ``quotes`` -- not a check in this file, which two
concurrent confirmations could both pass on their way to writing.

The comparison keeps the losers and their reason codes. A comparison listing only the winner
cannot be audited, and "why not that one" is the question a human actually asks.

OWNER: Track E.
"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from app.domain import (
    Approval,
    ApprovalKind,
    ApprovalReason,
    ApprovalStatus,
    AwardConflict,
    CallContext,
    CallDirection,
    CallPhase,
    CallRecord,
    Carrier,
    Comparison,
    ComparisonEntry,
    CostComponent,
    DecisionRow,
    DialPlan,
    EventRow,
    Order,
    OrderStatus,
    PolicyDecision,
    QuoteProposal,
    QuoteRow,
    QuoteStatus,
    Store,
)
from app.policy import evaluate_quote, select_best

__all__ = ["Market"]

#: Quote states that are still in the running. A superseded or withdrawn row stays on disk
#: as evidence but takes no part in the comparison -- it was replaced by a later utterance.
_LIVE_QUOTES = frozenset({QuoteStatus.PROPOSED, QuoteStatus.SELECTED})


class Market:
    def __init__(self, store: Store, *, now: Callable[[], datetime]) -> None:
        self._store = store
        self._now = now

    async def plan_rfq(self, order: Order, count: int) -> list[DialPlan]:
        """Pick carriers, create a call row each, and freeze one CallContext per call.

        Each context carries the market state as of dial time -- how many quotes are in hand
        and the best so far -- so the third call negotiates with two numbers behind it and
        the first with none.
        """
        # Claim the market before planning anything. Keyed on the mandate version, so raising
        # the cap legitimately reopens it and a second click on the same mandate does not.
        #
        # This is the only thing standing between "two instances are running" and "the carrier's
        # phone rings twice". The deadline sweep has had the same guard since it was written;
        # this path did not, because the event was appended at the end and its answer thrown
        # away. Two portals -- a local one and the deployed one -- pointed at one database would
        # both have planned and both have dialled.
        claimed = await self._store.append_event(
            EventRow(
                order_id=order.id,
                type="rfq.planned",
                payload={"mandate_version": order.mandate_version},
                idempotency_key=f"rfq-planned:{order.id}:{order.mandate_version}",
            )
        )
        if not claimed:
            return []

        carriers = await self._store.carriers_for_rfq(count)
        if len(carriers) < 3:
            # The brief requires at least three. Fewer is not a thin market to push through;
            # it is a market with no comparison in it, and a comparison is the deliverable.
            await self._store.raise_approval(
                Approval(
                    order_id=order.id,
                    kind=ApprovalKind.ESCALATION,
                    reason=ApprovalReason.NO_ELIGIBLE_CANDIDATE,
                    context={
                        "eligible": len(carriers),
                        "detail": "fewer than three carriers are on file, active and "
                        "reachable by phone",
                    },
                    raised_at=self._now(),
                )
            )
            return []

        quotes = await self._store.quotes_for(order.id)
        in_hand = len([q for q in quotes if q.status in _LIVE_QUOTES])
        best = min((q.amount.amount for q in quotes if q.status in _LIVE_QUOTES), default=None)

        plans: list[DialPlan] = []
        for carrier in carriers:
            context = self._context_for(order, carrier, in_hand, best)
            call_id = await self._store.upsert_call(
                CallRecord(
                    vapi_call_id=f"pending:{order.id}:{carrier.id}",
                    direction=CallDirection.OUTBOUND,
                    phase=CallPhase.RFQ.value,
                    order_id=order.id,
                    carrier_id=carrier.id,
                    to_number=carrier.phone,
                    started_at=self._now(),
                    context=context.model_dump(mode="json"),
                )
            )
            plans.append(
                DialPlan(
                    call_id=call_id,
                    carrier=carrier,
                    to_number=carrier.phone,
                    context=context.model_dump(mode="json"),
                )
            )

        await self._store.set_order_status(order.id, OrderStatus.QUOTING)
        return plans

    def _context_for(
        self, order: Order, carrier: Carrier, in_hand: int, best: Decimal | None
    ) -> CallContext:
        return CallContext(
            phase=CallPhase.RFQ,
            today=self._now().strftime("%A, %d %B %Y"),
            reference=order.reference,
            origin=order.origin,
            destination=order.destination,
            cargo=order.cargo,
            equipment=order.equipment,
            weight=order.weight,
            counterparty_name=carrier.name,
            counterparty_contact=carrier.contact_name,
            # The figures go in the prompt so the agent can negotiate with judgement instead
            # of relaying every number to policy. policy/ still decides every proposal, so a
            # figure that leaks is an embarrassment and not an authorization.
            price_ceiling=order.cap.amount if order.cap else None,
            target_price=order.target.amount if order.target else None,
            quotes_in_hand=in_hand,
            best_rate_so_far=best,
        )

    async def rank(self, order: Order) -> Comparison:
        """Re-evaluate every quote against the current mandate, then select the best.

        Re-evaluated rather than trusting the verdict stored at proposal time, because time
        has passed: a quote that was inside its validity when it was spoken can be stale by
        the time the market closes, and the comparison has to say so.
        """
        now = self._now()
        quotes = [q for q in await self._store.quotes_for(order.id) if q.status in _LIVE_QUOTES]

        try:
            mandate = order.mandate()
        except ValueError:
            return Comparison(
                order_id=order.id,
                entries=[],
                winner_quote_id=None,
                cap_at_decision_cents=None,
                cap_currency=None,
                mandate_version=order.mandate_version,
                built_at=now,
            )

        evaluated: list[tuple[QuoteRow, QuoteProposal, PolicyDecision]] = []
        pairs: list[tuple[QuoteProposal, PolicyDecision]] = []
        for quote in quotes:
            proposal = self._proposal_for(order, quote)
            decision = evaluate_quote(mandate, proposal, {}, now=now)
            evaluated.append((quote, proposal, decision))
            pairs.append((proposal, decision))

        winner = select_best(pairs)
        winner_quote_id = None
        if winner is not None:
            winner_quote_id = next(
                (q.id for q, p, _ in evaluated if p.proposal_id == winner.proposal_id), None
            )

        entries: list[ComparisonEntry] = []
        for quote, _proposal, decision in evaluated:
            entries.append(
                ComparisonEntry(
                    quote_id=quote.id or "",
                    carrier_id=quote.carrier_id,
                    carrier_name=await self._carrier_name(quote.carrier_id),
                    amount=quote.amount,
                    all_in_usd_cents=(
                        int(decision.cost.buffered_usd * 100) if decision.cost else None
                    ),
                    pickup_at=quote.pickup_at,
                    equipment=quote.equipment,
                    outcome=decision.outcome.value,
                    reason_code=decision.reason.value,
                    is_winner=quote.id == winner_quote_id and quote.id is not None,
                )
            )
            # One decisions row per quote per ranking. The refusals are the auditable half:
            # "why not that one" is the question a human actually asks.
            await self._store.record_decision(
                DecisionRow(
                    order_id=order.id,
                    call_id=quote.call_id,
                    quote_id=quote.id,
                    proposal={"source": "rank", "quote_id": quote.id or ""},
                    outcome=decision.outcome.value,
                    reason_code=decision.reason.value,
                    cap_at_decision_cents=order.cap.cents if order.cap else None,
                    cap_currency=order.cap.currency if order.cap else None,
                    mandate_version=order.mandate_version,
                    decided_at=now,
                )
            )

        # Sorted cheapest-first among the eligible, then everything else. The winner is
        # already flagged; this only makes the comparison readable at a glance.
        entries.sort(key=lambda e: (not e.is_winner, e.all_in_usd_cents or 1 << 62, e.quote_id))
        return Comparison(
            order_id=order.id,
            entries=entries,
            winner_quote_id=winner_quote_id,
            cap_at_decision_cents=order.cap.cents if order.cap else None,
            cap_currency=order.cap.currency if order.cap else None,
            mandate_version=order.mandate_version,
            built_at=now,
        )

    def _proposal_for(self, order: Order, quote: QuoteRow) -> QuoteProposal:
        components: list[CostComponent] = []
        for raw in quote.components:
            try:
                components.append(CostComponent.model_validate(raw))
            except (ValueError, TypeError):
                continue
        if not components:
            components = [
                CostComponent(
                    name="all-in",
                    amount=Decimal(quote.amount.cents) / Decimal(100),
                    currency=quote.amount.currency,
                )
            ]
        return QuoteProposal(
            proposal_id=f"rank:{quote.id}",
            operation_id=order.id,
            carrier_id=quote.carrier_id,
            carrier_contact_id=quote.carrier_id,
            components=tuple(components),
            cost_is_final=quote.cost_is_final,
            pickup_at=quote.pickup_at,
            equipment=quote.equipment,
            valid_until=quote.valid_until,
            source_call_id=quote.call_id,
            source_event_id=f"rank:{quote.id}",
            transcript_anchor_ms=quote.anchor_ms,
            carrier_confirmed_exact_recap=quote.carrier_confirmed_exact_recap,
            confirmed_at=quote.confirmed_at,
        )

    async def _carrier_name(self, carrier_id: str) -> str:
        carrier = await self._store.carrier(carrier_id)
        return carrier.name if carrier else carrier_id

    async def request_award_approval(self, order: Order, comparison: Comparison) -> Approval:
        """Hand the ranked comparison to a human; move the order to awaiting_approval."""
        approval = Approval(
            order_id=order.id,
            kind=ApprovalKind.AWARD_APPROVAL,
            reason=(
                ApprovalReason.AWARD_SELECTED
                if comparison.winner_quote_id
                else ApprovalReason.NO_ELIGIBLE_CANDIDATE
            ),
            # The whole comparison, not just the winner. What makes this auditable is that
            # the losing quotes and their reason codes travel with the request.
            context=comparison.model_dump(mode="json"),
            raised_at=self._now(),
        )
        approval_id = await self._store.raise_approval(approval)
        await self._store.set_order_status(order.id, OrderStatus.AWAITING_APPROVAL)
        await self._store.append_event(
            EventRow(
                order_id=order.id,
                type="award.approval_requested",
                payload={
                    "approval_id": approval_id,
                    "winner_quote_id": comparison.winner_quote_id or "",
                },
                idempotency_key=f"award-requested:{order.id}:{comparison.built_at.isoformat()}",
            )
        )
        return approval.model_copy(update={"id": approval_id})

    async def award(self, order: Order, approval: Approval) -> str:
        """Revalidate and accept the approved winner against current trusted state."""
        if approval.status is not ApprovalStatus.APPROVED:
            raise ValueError("an award requires an approved approval; nothing else authorizes one")

        quote_id = str(approval.context.get("winner_quote_id") or "")
        if not quote_id:
            raise ValueError("the approval carries no winning quote")

        approved_version = approval.context.get("mandate_version")
        if approved_version != order.mandate_version:
            raise ValueError("the approval was made under a stale mandate version")

        current = await self.rank(order)
        if current.winner_quote_id != quote_id:
            raise ValueError("the approved quote is no longer the current eligible winner")

        try:
            await self._store.accept_quote(order.id, quote_id)
        except AwardConflict:
            # Never retried. A second award attempt means something already closed this
            # order, and the answer to two carriers holding a booking is a person, not a
            # loop that eventually picks one.
            await self._store.raise_approval(
                Approval(
                    order_id=order.id,
                    kind=ApprovalKind.ESCALATION,
                    reason=ApprovalReason.CONFLICTING_INFORMATION,
                    context={
                        "attempted_quote_id": quote_id,
                        "detail": "this order already has an accepted quote; the second "
                        "award was refused by the database",
                    },
                    raised_at=self._now(),
                )
            )
            await self._store.append_event(
                EventRow(
                    order_id=order.id,
                    type="award.conflict",
                    payload={"attempted_quote_id": quote_id},
                    idempotency_key=f"award-conflict:{order.id}:{quote_id}",
                )
            )
            raise

        await self._store.save_order(
            order.model_copy(update={"status": OrderStatus.AWARDING, "awarded_quote_id": quote_id})
        )
        await self._store.append_event(
            EventRow(
                order_id=order.id,
                type="award.accepted",
                payload={"quote_id": quote_id, "approval_id": approval.id or ""},
                idempotency_key=f"award-accepted:{order.id}:{quote_id}",
            )
        )
        return quote_id
