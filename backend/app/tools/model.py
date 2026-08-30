"""The five tools exposed to the model. The complete mutation surface a stranger can reach.

MAY IMPORT:  domain, policy, store, notify.
IMPORTED BY: vapi/toolserver.py.

Every handler returns a **single-line string** that Vapi reads back to the model. Three
things that string may never contain, because a counterparty can ask for all three:

  * the price ceiling, the target, or any figure that came from our instructions rather than
    from them;
  * a number, date or name the counterparty did not actually say;
  * the words "approved", "booked" or "confirmed" -- a call creates a pre-agreement, and the
    model must not be handed language that lets it claim otherwise.

Those three rules are enforced, not just documented: every string the module can return is
declared in ``RESPONSES`` below, and ``tests/test_ugly_cases.py`` asserts the property over
the whole table. A handler that returns an undeclared string fails the suite.

The argument models below are frozen in Phase 0 because two tracks depend on them: Track A
implements the handlers, Track B renders these same shapes as the JSON schemas Vapi
validates against. Changing one is a CHANGELOG event.

OWNER: Track A.
"""

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.domain import (
    Approval,
    ApprovalKind,
    ApprovalReason,
    AwardConflict,
    CallPhase,
    CallRecord,
    CallReport,
    Commitment,
    CostComponent,
    DecisionRow,
    EventRow,
    IncidentSubject,
    Money,
    Order,
    OrderStatus,
    PolicyOutcome,
    QuoteProposal,
    QuoteRow,
    QuoteStatus,
    ReasonCode,
    Severity,
    Store,
)
from app.policy import evaluate_quote, require_preagreement_evidence
from app.tools.calls import CallLedger
from app.tools.commitments import CommitmentCoordinator
from app.tools.parse import Ambiguous, parse_amount, parse_date

__all__ = [
    "RESPONSES",
    "ConfirmPreagreementArgs",
    "LookupOrderArgs",
    "ModelTools",
    "ProposeQuoteArgs",
    "QuotedComponent",
    "ReportIncidentArgs",
    "VerifyCallerArgs",
]


class QuotedComponent(BaseModel):
    """One line of what they quoted: the linehaul, the tolls, the waiting time.

    Each carries its own currency. A carrier who quotes the run in pesos and the tolls in
    dollars has said two things, and flattening them loses the one that matters.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=80, description="linehaul, tolls, waiting, ...")
    amount: str = Field(description="Exactly as said. A string so nothing is rounded in transit.")
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217. Never inferred.")


class ProposeQuoteArgs(BaseModel):
    """A rate a carrier stated on the call. A proposal, never an authorization."""

    model_config = ConfigDict(frozen=True)

    components: list[QuotedComponent] = Field(
        min_length=1, description="Every payable element they named, each with its currency."
    )
    cost_is_final: bool = Field(
        description="True only if they said the total covers everything. 'Plus tolls' is False."
    )
    pickup_date: str = Field(description="An explicit calendar date, ISO 8601. Never a weekday.")
    pickup_window_end: str | None = Field(
        default=None, description="ISO 8601, if they gave a window rather than a time."
    )
    equipment: str = Field(min_length=1)
    valid_until: str | None = Field(
        default=None, description="ISO 8601. How long they said the quote holds."
    )
    claimed_identity: str | None = Field(
        default=None, description="The name they gave. Recorded, never trusted."
    )


class ConfirmPreagreementArgs(BaseModel):
    """The carrier confirmed the exact recap, word for word, on an award call."""

    model_config = ConfigDict(frozen=True)

    quote_id: str = Field(min_length=1, description="The quote whose terms were read back.")
    carrier_confirmed_exact_recap: bool = Field(
        description="True only after they answered yes to the complete recap. "
        "A 'sure' in reply to five things is not a yes to five things."
    )
    claimed_identity: str | None = None


class VerifyCallerArgs(BaseModel):
    """One identity attempt on an inbound call.

    The fact must come from them. Never read the expected value out first: a caller who is
    told the plate can repeat the plate, which verifies nothing.
    """

    model_config = ConfigDict(frozen=True)

    claimed_name: str | None = None
    claimed_company: str | None = None
    fact_kind: str = Field(description="reference | plate | container | driver")
    fact_value: str = Field(min_length=1, description="What they said, verbatim.")


class LookupOrderArgs(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str | None = Field(
        default=None, description="Only used on an inbound call not yet correlated."
    )


class ReportIncidentArgs(BaseModel):
    """What the call turned out to be about."""

    model_config = ConfigDict(frozen=True)

    subject: IncidentSubject
    detail: str = Field(min_length=1, description="What they said happened, in their terms.")
    new_eta: str | None = Field(
        default=None,
        description="ISO 8601 with a clock time. Only if they gave an explicit date and time.",
    )
    load_at_risk: bool = False


# --------------------------------------------------------------------------- what it may say

#: Every string a handler can return, in one place so the property can be asserted over all
#: of them at once. None of these carries a figure, a date or a name -- everything the model
#: needs to say out loud, it already heard. Two of them are the *only* two words the caller
#: gets out of an identity check.
RESPONSES: dict[str, str] = {
    "quote_recorded": "Recorded. Nothing is booked.",
    "quote_escalated": "That is something a person from the team has to look at.",
    "quote_replayed": "Already recorded. Nothing is booked.",
    "amount_unclear": "Please say the amount again in full, with the currency.",
    "date_unclear": "What is the exact pickup date -- day, month and year?",
    "cost_not_final": "Is that the final all-in cost, including every payable charge?",
    "preagreement_noted": "Noted as a pre-agreement, subject to written confirmation.",
    "preagreement_refused": "I cannot record that as agreed; a person from the team has to "
    "take it from here.",
    "identity_match": "matches",
    "identity_no_match": "does not match",
    "identity_required": "I need to verify who I am speaking with first.",
    "incident_recorded": "Recorded. I cannot approve that on this call.",
    "no_order": "I do not have that operation on this call; a person from the team will follow up.",
    "internal_error": "I cannot complete that right now, so I will pass it to a person from "
    "the team.",
}

#: Words a tool result may never contain. The model reads these strings back verbatim, and a
#: result that says "approved" hands it language to close a deal we have not authorized.
FORBIDDEN_IN_RESULTS = ("approved", "booked", "confirmed", "agreed to", "we have a deal")

#: How long a rate holds when the carrier did not say. See the note in ``propose_quote``.
_UNSTATED_VALIDITY = timedelta(hours=2)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: The only order-status moves a phone call may cause. Everything absent from this table is
#: a request for a person, not a transition: a caller saying "it is delivered" is a claim,
#: and closing an order on a claim is how a load is written off while it is still on a truck.
_INCIDENT_TRANSITIONS: dict[IncidentSubject, OrderStatus | None] = {
    IncidentSubject.ACCIDENT: OrderStatus.AT_RISK,
    IncidentSubject.DELAY: OrderStatus.AT_RISK,
    IncidentSubject.DELIVERED: None,
    IncidentSubject.REQUEST: None,
    IncidentSubject.QUOTE: None,
    IncidentSubject.OTHER: None,
}

_INCIDENT_SEVERITY: dict[IncidentSubject, Severity] = {
    IncidentSubject.ACCIDENT: Severity.HIGH,
    IncidentSubject.DELAY: Severity.MEDIUM,
    IncidentSubject.DELIVERED: Severity.LOW,
    IncidentSubject.REQUEST: Severity.MEDIUM,
    IncidentSubject.QUOTE: Severity.LOW,
    IncidentSubject.OTHER: Severity.LOW,
}


def _normalize(value: str) -> str:
    """Fold a spoken fact to its comparable core.

    A plate read out as "J K L dash one two three" and one stored as "JKL-123" are the same
    plate. Case, spacing and punctuation are transcription artefacts, not differences.
    """
    return _NON_ALNUM.sub("", value.strip().casefold())


def _digest(name: str, call_id: str, args: BaseModel) -> str:
    """An idempotency key derived from the fact, not from the moment it arrived.

    Two identical tool calls are one fact and must produce one row; the same figure said
    twice is not two quotes. A changed figure hashes differently and is a new fact, which is
    exactly the behaviour ugly case 2 asks for.
    """
    canonical = json.dumps(args.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return f"{name}:{call_id}:{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"


class ModelTools:
    """The model-facing handlers. Nothing here decides; each one asks policy and records.

    ``now`` is injected rather than read from the clock so a scenario can be replayed and
    produce the same decisions.
    """

    def __init__(
        self,
        store: Store,
        *,
        now: Callable[[], datetime],
        ledger: CallLedger,
        commitments: CommitmentCoordinator,
    ) -> None:
        self._store = store
        self._now = now
        self._ledger = ledger
        self._commitments = commitments

    # ------------------------------------------------------------------------ propose_quote

    async def propose_quote(self, call_id: str, args: ProposeQuoteArgs) -> str:
        """Parse, evaluate against the mandate, write quote + decision + event.

        An ambiguous amount returns the clarification line and writes nothing: "eight five"
        is not data until somebody says which number it is.
        """
        call, order = await self._call_and_order(call_id)
        if call is None or order is None:
            return RESPONSES["no_order"]

        components: list[CostComponent] = []
        for component in args.components:
            parsed = parse_amount(f"{component.amount} {component.currency}")
            if isinstance(parsed, Ambiguous) or parsed is None:
                # Nothing is written. A figure nobody can read a single way is not a fact
                # about this order, and a quotes row would make it look like one.
                return RESPONSES["amount_unclear"]
            amount, currency = parsed
            components.append(CostComponent(name=component.name, amount=amount, currency=currency))

        pickup = parse_date(args.pickup_date, self._now().date())
        if isinstance(pickup, Ambiguous) or pickup is None:
            return RESPONSES["date_unclear"]

        window_end = parse_date(args.pickup_window_end or "", self._now().date())
        valid_until = parse_date(args.valid_until or "", self._now().date())
        if isinstance(window_end, Ambiguous):
            window_end = None
        if isinstance(valid_until, Ambiguous) or valid_until is None:
            # No stated validity is not "valid forever" -- but it was not "already expired"
            # either, which is what ``self._now()`` meant here. The row is written a fraction
            # of a second after the check runs, so every quote whose carrier did not name a
            # validity was born stale: policy denies on STALE_EVIDENCE before it looks at
            # anything else, and two real quotes on OP-MZO-0003 came back "no eligible
            # candidate" with a validity span of minus 0.19 seconds.
            #
            # A rate given with no validity is good for the round it was given in. This
            # covers the market closing and a person deciding, and expires well before a
            # mandate could move underneath it -- and ``rank()`` re-evaluates at approval
            # time anyway, so a slow decision still fails closed rather than awarding on a
            # figure nobody would honour.
            valid_until = self._now() + _UNSTATED_VALIDITY

        key = _digest("propose_quote", call_id, args)
        anchor = await self._ledger.anchor_ms(call_id)

        quote = QuoteRow(
            order_id=order.id,
            carrier_id=call.carrier_id or "unknown",
            call_id=call_id,
            anchor_ms=anchor,
            amount=Money(
                cents=int(sum(c.amount for c in components) * 100),
                currency=components[0].currency,
            ),
            components=[c.model_dump(mode="json") for c in components],
            cost_is_final=args.cost_is_final,
            pickup_at=pickup,
            pickup_window_end=window_end,
            equipment=args.equipment,
            valid_until=valid_until,
            claimed_identity=args.claimed_identity,
            identity_level=call.identity_level,
        )

        try:
            mandate = order.mandate()
        except ValueError:
            # A missing ceiling is not "no limit"; it is a mandate that was never granted.
            return await self._refuse_quote(order, call_id, quote, key, args)

        proposal = QuoteProposal(
            proposal_id=key,
            operation_id=order.id,
            carrier_id=quote.carrier_id,
            carrier_contact_id=quote.carrier_id,
            components=tuple(components),
            cost_is_final=args.cost_is_final,
            pickup_at=pickup,
            equipment=args.equipment,
            valid_until=valid_until,
            source_call_id=call_id,
            source_event_id=key,
            transcript_anchor_ms=anchor,
        )
        # fx is empty on purpose: no approved snapshot exists in this build, so a non-USD
        # component lands on FX_EVIDENCE_MISSING and escalates. Inventing a rate to get past
        # that is the failure the reason code is named after.
        decision = evaluate_quote(mandate, proposal, {}, now=self._now())

        if not await self._store.append_event(
            EventRow(
                order_id=order.id,
                call_id=call_id,
                type="quote.proposed",
                payload={"proposal_id": key, "outcome": decision.outcome.value},
                idempotency_key=key,
            )
        ):
            return RESPONSES["quote_replayed"]

        # The quotes row is written even when policy refuses. They said it; the refusal is
        # the decisions row next to it. Dropping the quote would delete the fact a judge
        # probes -- "they offered 10,500 and you said no" needs both halves.
        quote_id = await self._store.add_quote(
            quote.model_copy(
                update={
                    "all_in_usd_cents": (
                        int(decision.cost.buffered_usd * 100) if decision.cost else None
                    )
                }
            )
        )
        await self._supersede_previous(order.id, quote.carrier_id, quote_id)
        await self._store.record_decision(
            DecisionRow(
                order_id=order.id,
                call_id=call_id,
                quote_id=quote_id,
                proposal=proposal.model_dump(mode="json"),
                outcome=decision.outcome.value,
                reason_code=decision.reason.value,
                cap_at_decision_cents=order.cap.cents if order.cap else None,
                cap_currency=order.cap.currency if order.cap else None,
                mandate_version=order.mandate_version,
                decided_at=self._now(),
            )
        )

        if decision.outcome is PolicyOutcome.ALLOW:
            return RESPONSES["quote_recorded"]
        if decision.reason is ReasonCode.INCOMPLETE_COST:
            return RESPONSES["cost_not_final"]
        await self._store.raise_approval(
            Approval(
                order_id=order.id,
                call_id=call_id,
                kind=ApprovalKind.ESCALATION,
                reason=ApprovalReason.OUTSIDE_MANDATE
                if decision.reason is ReasonCode.OUTSIDE_MANDATE
                else ApprovalReason.POLICY_FAILURE,
                context={
                    "quote_id": quote_id,
                    "reason_code": decision.reason.value,
                    "all_in_usd": str(decision.cost.buffered_usd) if decision.cost else "",
                },
                raised_at=self._now(),
            )
        )
        return RESPONSES["quote_escalated"]

    async def _refuse_quote(
        self,
        order: Order,
        call_id: str,
        quote: QuoteRow,
        key: str,
        args: ProposeQuoteArgs,
    ) -> str:
        """Record a quote against an order with no mandate, and authorize nothing."""
        if not await self._store.append_event(
            EventRow(
                order_id=order.id,
                call_id=call_id,
                type="quote.proposed",
                payload={"proposal_id": key, "outcome": PolicyOutcome.ESCALATE.value},
                idempotency_key=key,
            )
        ):
            return RESPONSES["quote_replayed"]
        quote_id = await self._store.add_quote(quote)
        await self._store.record_decision(
            DecisionRow(
                order_id=order.id,
                call_id=call_id,
                quote_id=quote_id,
                proposal=args.model_dump(mode="json"),
                outcome=PolicyOutcome.ESCALATE.value,
                reason_code=ReasonCode.OUTSIDE_MANDATE.value,
                mandate_version=order.mandate_version,
                decided_at=self._now(),
            )
        )
        await self._store.raise_approval(
            Approval(
                order_id=order.id,
                call_id=call_id,
                kind=ApprovalKind.ESCALATION,
                reason=ApprovalReason.OUTSIDE_MANDATE,
                context={"quote_id": quote_id, "detail": "no mandate has been granted"},
                raised_at=self._now(),
            )
        )
        return RESPONSES["quote_escalated"]

    async def _supersede_previous(self, order_id: str, carrier_id: str, new_quote_id: str) -> None:
        """A second figure from the same carrier replaces the first in the market, not on disk.

        Both rows survive with ``superseded_by`` linking them, because "they said 8,500 and
        then they said 9,200" is two facts and a judge will ask about the first one.
        """
        for existing in await self._store.quotes_for(order_id):
            if (
                existing.id
                and existing.id != new_quote_id
                and existing.carrier_id == carrier_id
                and existing.status is QuoteStatus.PROPOSED
            ):
                await self._store.supersede_quote(existing.id, new_quote_id)

    # ----------------------------------------------------------------- confirm_preagreement

    async def confirm_preagreement(self, call_id: str, args: ConfirmPreagreementArgs) -> str:
        """Gate on anchored recap evidence, then open a commitment in state VERBAL."""
        call, order = await self._call_and_order(call_id)
        if call is None or order is None:
            return RESPONSES["no_order"]

        quote = await self._store.quote(args.quote_id)
        if (
            quote is None
            or quote.order_id != order.id
            or quote.status is not QuoteStatus.ACCEPTED
            or order.awarded_quote_id != quote.id
            or call.carrier_id != quote.carrier_id
        ):
            return RESPONSES["preagreement_refused"]

        key = _digest("confirm_preagreement", call_id, args)

        # RFQ and AWARD are separate phases (invariant 5). Several carriers may hold live
        # offers at once; only the one call we opened to close may close. A confirmation
        # arriving on an rfq call is the model talking itself into a phase it is not in.
        if call.phase != "award":
            return await self._refuse_preagreement(
                order, call_id, quote, key, ReasonCode.CONFLICTING_STATE
            )

        anchor = await self._ledger.anchor_ms(call_id)
        # An anchor we did not measure is not evidence. calls.py returns 0 rather than
        # failing so a quote can still be recorded; a commitment is where that stops.
        if call.started_at is None:
            return await self._refuse_preagreement(
                order, call_id, quote, key, ReasonCode.EVIDENCE_MISSING
            )

        try:
            mandate = order.mandate()
        except ValueError:
            return await self._refuse_preagreement(
                order, call_id, quote, key, ReasonCode.OUTSIDE_MANDATE
            )

        proposal = QuoteProposal(
            proposal_id=key,
            operation_id=order.id,
            carrier_id=quote.carrier_id,
            carrier_contact_id=quote.carrier_id,
            components=tuple(self._components_of(quote)),
            cost_is_final=quote.cost_is_final,
            pickup_at=quote.pickup_at,
            equipment=quote.equipment,
            valid_until=quote.valid_until,
            source_call_id=call_id,
            source_event_id=key,
            transcript_anchor_ms=anchor,
            carrier_confirmed_exact_recap=args.carrier_confirmed_exact_recap,
            confirmed_at=self._now() if args.carrier_confirmed_exact_recap else None,
        )
        decision = require_preagreement_evidence(
            mandate, proposal, evaluate_quote(mandate, proposal, {}, now=self._now())
        )
        if decision.outcome is not PolicyOutcome.ALLOW:
            return await self._refuse_preagreement(order, call_id, quote, key, decision.reason)

        if not await self._store.append_event(
            EventRow(
                order_id=order.id,
                call_id=call_id,
                type="preagreement.confirmed",
                payload={"quote_id": args.quote_id, "anchor_ms": anchor},
                idempotency_key=key,
            )
        ):
            return RESPONSES["preagreement_noted"]

        try:
            await self._commitments.open_verbal(
                Commitment(
                    order_id=order.id,
                    quote_id=args.quote_id,
                    evidence_call_id=call_id,
                    evidence_anchor_ms=anchor,
                    terms={
                        "amount_cents": quote.amount.cents,
                        "currency": quote.amount.currency,
                        "pickup_at": quote.pickup_at.isoformat(),
                        "equipment": quote.equipment,
                    },
                    claimed_identity=args.claimed_identity,
                    identity_level=call.identity_level,
                    created_at=self._now(),
                )
            )
        except AwardConflict:
            # Two carriers confirming at the same moment. The store refused the second, which
            # is the whole point -- two open bookings is the worst failure in the brief.
            return await self._refuse_preagreement(
                order, call_id, quote, key, ReasonCode.CONFLICTING_STATE
            )

        await self._store.record_decision(
            DecisionRow(
                order_id=order.id,
                call_id=call_id,
                quote_id=args.quote_id,
                proposal=proposal.model_dump(mode="json"),
                outcome=decision.outcome.value,
                reason_code=decision.reason.value,
                cap_at_decision_cents=order.cap.cents if order.cap else None,
                cap_currency=order.cap.currency if order.cap else None,
                mandate_version=order.mandate_version,
                decided_at=self._now(),
            )
        )
        return RESPONSES["preagreement_noted"]

    async def _refuse_preagreement(
        self, order: Order, call_id: str, quote: QuoteRow, key: str, reason: ReasonCode
    ) -> str:
        outcome = (
            PolicyOutcome.ESCALATE if reason is ReasonCode.OUTSIDE_MANDATE else PolicyOutcome.DENY
        )
        await self._store.record_decision(
            DecisionRow(
                order_id=order.id,
                call_id=call_id,
                quote_id=quote.id,
                proposal={"quote_id": quote.id or "", "source_event_id": key},
                outcome=outcome.value,
                reason_code=reason.value,
                cap_at_decision_cents=order.cap.cents if order.cap else None,
                cap_currency=order.cap.currency if order.cap else None,
                mandate_version=order.mandate_version,
                decided_at=self._now(),
            )
        )
        await self._store.raise_approval(
            Approval(
                order_id=order.id,
                call_id=call_id,
                kind=ApprovalKind.ESCALATION,
                reason=ApprovalReason.CONFLICTING_INFORMATION
                if reason is ReasonCode.CONFLICTING_STATE
                else ApprovalReason.OUTSIDE_MANDATE,
                context={"quote_id": quote.id or "", "reason_code": reason.value},
                raised_at=self._now(),
            )
        )
        return RESPONSES["preagreement_refused"]

    @staticmethod
    def _components_of(quote: QuoteRow) -> list[CostComponent]:
        """Rebuild the priced components from the stored row.

        Falls back to a single all-in line when the row carries no breakdown, so a quote
        written before a breakdown existed still re-evaluates rather than crashing the call.
        """
        rebuilt: list[CostComponent] = []
        for raw in quote.components:
            try:
                rebuilt.append(CostComponent.model_validate(raw))
            except (ValueError, TypeError):
                continue
        if rebuilt:
            return rebuilt
        return [
            CostComponent(
                name="all-in",
                amount=Decimal(quote.amount.cents) / Decimal(100),
                currency=quote.amount.currency,
            )
        ]

    # ------------------------------------------------------------------------ verify_caller

    async def verify_caller(self, call_id: str, args: VerifyCallerArgs) -> str:
        """Compare a claimed fact against the order. Returns match / no match, nothing else."""
        call = await self._store.call(call_id)
        if call is None:
            return RESPONSES["identity_no_match"]

        order = await self._store.order(call.order_id) if call.order_id else None
        if order is None and args.fact_kind == "reference":
            # The one correlation path: an inbound call reaches its order by the caller
            # producing the folio, never by us reading one out.
            order = await self._store.order_by_reference(args.fact_value.strip())

        if order is not None and (
            order.status in {OrderStatus.DELIVERED, OrderStatus.CLOSED, OrderStatus.CANCELLED}
            or (
                order.assigned_carrier_id is not None
                and order.assigned_carrier_id != call.carrier_id
            )
        ):
            order = None

        expected = self._expected_fact(order, args.fact_kind) if order else None
        matched = expected is not None and _normalize(expected) == _normalize(args.fact_value)

        first_attempt = await self._store.append_event(
            EventRow(
                order_id=order.id if order else None,
                call_id=call_id,
                type="identity.attempt",
                payload={
                    "fact_kind": args.fact_kind,
                    "matched": matched,
                    "claimed_name": args.claimed_name or "",
                    "claimed_company": args.claimed_company or "",
                },
                idempotency_key=_digest("verify_caller", call_id, args),
            )
        )
        if not matched:
            # No hint about which part was wrong, and never the expected value. A caller who
            # is told the plate can repeat the plate, which verifies nothing.
            return RESPONSES["identity_no_match"]

        if not first_attempt:
            return RESPONSES["identity_match"]

        # A reference correlates the order but does not authenticate the caller. Level 1
        # comes only from trusted directory phone matching; one independent operational
        # fact then reaches level 2. Level 3 is reserved for a future stronger mechanism.
        level = call.identity_level
        if args.fact_kind != "reference" and call.carrier_id and level >= 1:
            level = 2
        await self._store.upsert_call(
            call.model_copy(
                update={
                    "order_id": call.order_id or (order.id if order else None),
                    "identity_level": level,
                    # Identity may only ever demand more, so this flips up and nothing in
                    # a conversation flips it back down.
                    "identity_verified": call.identity_verified or level >= 2,
                }
            )
        )
        return RESPONSES["identity_match"]

    @staticmethod
    def _expected_fact(order: Order, fact_kind: str) -> str | None:
        return {
            "reference": order.reference,
            "plate": order.expected_plate,
            "container": order.container_number,
            "driver": order.expected_driver,
        }.get(fact_kind)

    # ------------------------------------------------------------------------- lookup_order

    async def lookup_order(self, call_id: str, args: LookupOrderArgs) -> str:
        """Read-only. Returns nothing operational until identity_verified is true."""
        call = await self._store.call(call_id)
        if call is None:
            return RESPONSES["no_order"]
        if not call.identity_verified:
            # The refusal is the same whether the order exists or not. A different answer for
            # a real reference would turn this into an oracle for guessing folios.
            return RESPONSES["identity_required"]

        order = await self._store.order(call.order_id) if call.order_id else None
        if order is None and args.reference:
            order = await self._store.order_by_reference(args.reference.strip())
        if order is None:
            return RESPONSES["no_order"]

        parts = [
            f"reference {order.reference}",
            f"status {order.status.value}",
            f"from {order.origin}" if order.origin else "",
            f"to {order.destination}" if order.destination else "",
            f"equipment {order.equipment}" if order.equipment else "",
            f"last free day {order.last_free_day.isoformat()}" if order.last_free_day else "",
        ]
        return "; ".join(part for part in parts if part)

    # ----------------------------------------------------------------------- report_incident

    async def report_incident(self, call_id: str, args: ReportIncidentArgs) -> str:
        """Record what happened. Moves order status only along a whitelisted transition."""
        call, order = await self._call_and_order(call_id)
        if call is None:
            return RESPONSES["no_order"]

        eta = parse_date(args.new_eta or "", self._now().date())
        if isinstance(eta, Ambiguous):
            return RESPONSES["date_unclear"]

        key = _digest("report_incident", call_id, args)
        if not await self._store.append_event(
            EventRow(
                order_id=order.id if order else None,
                call_id=call_id,
                type=f"incident.{args.subject.value}",
                payload={
                    "detail": args.detail,
                    "new_eta": eta.isoformat() if eta else "",
                    "load_at_risk": args.load_at_risk,
                    "identity_verified": call.identity_verified,
                },
                idempotency_key=key,
            )
        ):
            return RESPONSES["incident_recorded"]

        severity = _INCIDENT_SEVERITY[args.subject]
        if args.load_at_risk:
            severity = Severity.HIGH
        existing = await self._store.report_for(call_id)
        await self._store.save_report(
            (existing or CallReport(call_id=call_id, summary=args.detail)).model_copy(
                update={
                    "subject": args.subject,
                    "severity": severity,
                    "generated_at": self._now(),
                }
            )
        )

        target = _INCIDENT_TRANSITIONS[args.subject]
        if order is not None:
            if args.load_at_risk:
                target = OrderStatus.AT_RISK
            if target is not None and order.status not in (target, OrderStatus.CLOSED):
                await self._store.set_order_status(order.id, target)

        # Everything the whitelist does not move goes to a person. An uncorrelated inbound
        # call is still visible in the approval queue, but cannot mutate any order. A claimed
        # "delivered" is similarly never enough to close an order on a caller's word.
        if (
            order is None
            or call.phase == CallPhase.STATUS_CHECK.value
            or target is None
            or severity is Severity.HIGH
            or not call.identity_verified
        ):
            await self._store.raise_approval(
                Approval(
                    order_id=order.id if order else None,
                    call_id=call_id,
                    kind=ApprovalKind.INCIDENT,
                    reason=(
                        ApprovalReason.DEADLINE_BREACH
                        if call.phase == CallPhase.STATUS_CHECK.value
                        else self._incident_reason(args, call)
                    ),
                    context={
                        "subject": args.subject.value,
                        "detail": args.detail,
                        "new_eta": eta.isoformat() if eta else "",
                        "identity_verified": call.identity_verified,
                    },
                    raised_at=self._now(),
                )
            )
        return RESPONSES["incident_recorded"]

    @staticmethod
    def _incident_reason(args: ReportIncidentArgs, call: CallRecord) -> ApprovalReason:
        if not call.identity_verified:
            return ApprovalReason.IDENTITY_UNVERIFIED
        if args.subject is IncidentSubject.REQUEST:
            return ApprovalReason.DIRECT_REQUEST
        return ApprovalReason.CARRIER_REPORTED_INCIDENT

    # ------------------------------------------------------------------------------ shared

    async def _call_and_order(self, call_id: str) -> tuple[CallRecord | None, Order | None]:
        call = await self._store.call(call_id)
        if call is None:
            return None, None
        order = await self._store.order(call.order_id) if call.order_id else None
        return call, order
