"""Test doubles for the four Protocols. This is what lets five tracks build at once.

``InMemoryStore`` is not a stub -- it is a working implementation, because the behaviours the
other tracks depend on are behaviours, not signatures:

  * ``append_event`` returns False on a repeated idempotency key;
  * ``accept_quote`` raises ``AwardConflict`` when the slot is taken;
  * ``supersede_quote`` links the old row instead of editing it.

Track C's ``tests/test_store.py`` runs the same suite against this and against Supabase, so
the fake and the real thing cannot quietly drift apart. A fake that is more permissive than
the database is worse than no fake: it makes a green suite mean nothing.

Nothing here is importable from ``app`` -- these live in tests/ because a test double in the
production tree eventually gets wired into production.
"""

from datetime import UTC, datetime

from app.domain import (
    Approval,
    ApprovalStatus,
    AwardConflict,
    CallContext,
    CallRecord,
    CallReport,
    CallStatus,
    Carrier,
    Commitment,
    CommitmentState,
    DecisionRow,
    DeliveryResult,
    DeliveryStatus,
    EventRow,
    Order,
    OrderStatus,
    OutboundMessage,
    QuoteRow,
    QuoteStatus,
    Turn,
)
from app.domain.models import COMMITMENT_DEAD_STATES, DELIVERY_UNDERWAY

__all__ = ["FakeCallPlacer", "InMemoryStore", "RecordingNotifier", "ScriptedReportModel"]


def _next_id(prefix: str, existing: dict[str, object]) -> str:
    return f"{prefix}-{len(existing) + 1}"


class InMemoryStore:
    """Implements domain.ports.Store against dictionaries."""

    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.carriers: dict[str, Carrier] = {}
        self.calls: dict[str, CallRecord] = {}
        self.quotes: dict[str, QuoteRow] = {}
        self.decisions: dict[str, DecisionRow] = {}
        self.events: dict[str, EventRow] = {}
        self.approvals: dict[str, Approval] = {}
        self.reports: dict[str, CallReport] = {}
        self.commitments: dict[str, Commitment] = {}
        self.deliveries: list[tuple[OutboundMessage, DeliveryResult]] = []
        #: Every idempotency key ever accepted. The second attempt is refused.
        self._seen_keys: set[str] = set()
        #: Mirrors the Postgres sequence in migration 0005. Monotonic, never reused.
        self._reference_seq = 0

    # --- seeding helpers (tests only; not part of the Protocol) --------------------

    def add_carrier(self, carrier: Carrier) -> Carrier:
        self.carriers[carrier.id] = carrier
        return carrier

    def add_order(self, order: Order) -> Order:
        self.orders[order.id] = order
        return order

    # --- reads --------------------------------------------------------------------

    async def order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    async def order_by_reference(self, reference: str) -> Order | None:
        return next((o for o in self.orders.values() if o.reference == reference), None)

    async def carrier(self, carrier_id: str) -> Carrier | None:
        return self.carriers.get(carrier_id)

    async def carrier_by_phone(self, phone: str) -> Carrier | None:
        return next((c for c in self.carriers.values() if c.phone == phone), None)

    async def carriers_for_rfq(self, limit: int) -> list[Carrier]:
        eligible = [c for c in self.carriers.values() if c.is_on_file and c.is_active and c.phone]
        return sorted(eligible, key=lambda c: c.id)[:limit]

    async def call(self, call_id: str) -> CallRecord | None:
        return self.calls.get(call_id)

    async def call_by_vapi_id(self, vapi_call_id: str) -> CallRecord | None:
        return next((c for c in self.calls.values() if c.vapi_call_id == vapi_call_id), None)

    async def calls_for(self, order_id: str) -> list[CallRecord]:
        return [call for call in self.calls.values() if call.order_id == order_id]

    async def quotes_for(self, order_id: str) -> list[QuoteRow]:
        return [q for q in self.quotes.values() if q.order_id == order_id]

    async def quote(self, quote_id: str) -> QuoteRow | None:
        return self.quotes.get(quote_id)

    async def live_commitment(self, order_id: str) -> Commitment | None:
        return next(
            (
                c
                for c in self.commitments.values()
                if c.order_id == order_id and c.state not in COMMITMENT_DEAD_STATES
            ),
            None,
        )

    async def commitment(self, commitment_id: str) -> Commitment | None:
        return self.commitments.get(commitment_id)

    async def due_for_chase(self, now: datetime) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if o.delivery_deadline is not None
            and o.delivery_deadline < now
            and o.status not in DELIVERY_UNDERWAY
        ]

    async def orders_in_status(self, status: OrderStatus) -> list[Order]:
        return sorted((o for o in self.orders.values() if o.status is status), key=lambda o: o.id)

    # --- writes -------------------------------------------------------------------

    async def upsert_call(self, call: CallRecord) -> str:
        existing = await self.call_by_vapi_id(call.vapi_call_id)
        call_id = existing.id if existing and existing.id else _next_id("call", self.calls)
        if existing is None:
            self.calls[call_id] = call.model_copy(update={"id": call_id})
            return call_id

        update: dict[str, object] = {"id": call_id}
        for field in (
            "recording_url",
            "ended_at",
            "ended_reason",
            "cost_cents",
            "order_id",
            "carrier_id",
            "started_at",
            "context",
        ):
            if getattr(call, field) in (None, {}, "") and getattr(existing, field):
                update[field] = getattr(existing, field)
        if not call.transcript and existing.transcript:
            update["transcript"] = existing.transcript
        if existing.identity_level > call.identity_level:
            update["identity_level"] = existing.identity_level
            update["identity_verified"] = existing.identity_verified
        if existing.status is CallStatus.ENDED and call.status is not CallStatus.ENDED:
            update["status"] = existing.status
        self.calls[call_id] = call.model_copy(update=update)
        return call_id

    async def attach_vapi_call_id(self, call_id: str, vapi_call_id: str) -> None:
        record = self.calls.get(call_id)
        if record is None:
            raise KeyError(call_id)
        self.calls[call_id] = record.model_copy(update={"vapi_call_id": vapi_call_id})

    async def add_quote(self, quote: QuoteRow) -> str:
        quote_id = quote.id or _next_id("quote", self.quotes)
        self.quotes[quote_id] = quote.model_copy(update={"id": quote_id})
        return quote_id

    async def supersede_quote(self, old_quote_id: str, new_quote_id: str) -> None:
        old = self.quotes[old_quote_id]
        self.quotes[old_quote_id] = old.model_copy(
            update={"status": QuoteStatus.SUPERSEDED, "superseded_by": new_quote_id}
        )

    async def accept_quote(self, order_id: str, quote_id: str) -> None:
        already = [
            q
            for q in self.quotes.values()
            if q.order_id == order_id and q.status is QuoteStatus.ACCEPTED
        ]
        if already:
            # What the partial unique index does in Postgres. Modelled here so a race that
            # the database would reject also fails in the suite that runs without one.
            raise AwardConflict(f"order {order_id} already awarded to quote {already[0].id}")
        quote = self.quotes[quote_id]
        self.quotes[quote_id] = quote.model_copy(update={"status": QuoteStatus.ACCEPTED})

    async def record_decision(self, decision: DecisionRow) -> str:
        decision_id = decision.id or _next_id("decision", self.decisions)
        self.decisions[decision_id] = decision.model_copy(update={"id": decision_id})
        return decision_id

    async def events_for_call(self, call_id: str) -> list[EventRow]:
        return [event for event in self.events.values() if event.call_id == call_id]

    async def append_event(self, event: EventRow) -> bool:
        if event.idempotency_key in self._seen_keys:
            return False
        self._seen_keys.add(event.idempotency_key)
        event_id = _next_id("event", self.events)
        self.events[event_id] = event.model_copy(update={"id": event_id})
        return True

    async def raise_approval(self, approval: Approval) -> str:
        approval_id = approval.id or _next_id("approval", self.approvals)
        self.approvals[approval_id] = approval.model_copy(update={"id": approval_id})
        return approval_id

    async def approval(self, approval_id: str) -> Approval | None:
        return self.approvals.get(approval_id)

    async def resolve_approval(
        self, approval_id: str, *, status: str, decided_by: str, note: str | None = None
    ) -> None:
        existing = self.approvals[approval_id]
        self.approvals[approval_id] = existing.model_copy(
            update={
                "status": ApprovalStatus(status),
                "decided_by": decided_by,
                "note": note,
                "decided_at": datetime.now(UTC),
            }
        )

    async def open_approvals(self, order_id: str | None = None) -> list[Approval]:
        return [
            a
            for a in self.approvals.values()
            if a.status is ApprovalStatus.OPEN and (order_id is None or a.order_id == order_id)
        ]

    async def save_report(self, report: CallReport) -> None:
        self.reports[report.call_id] = report

    async def report_for(self, call_id: str) -> CallReport | None:
        return self.reports.get(call_id)

    async def save_commitment(self, commitment: Commitment) -> str:
        live = await self.live_commitment(commitment.order_id)
        if live is not None and commitment.state not in COMMITMENT_DEAD_STATES:
            raise AwardConflict(
                f"order {commitment.order_id} already has a live commitment {live.id}"
            )
        commitment_id = commitment.id or _next_id("commitment", self.commitments)
        self.commitments[commitment_id] = commitment.model_copy(update={"id": commitment_id})
        return commitment_id

    async def update_commitment(self, commitment: Commitment) -> None:
        if commitment.id is None:
            raise ValueError("cannot update a commitment with no id")
        self.commitments[commitment.id] = commitment

    async def set_order_status(self, order_id: str, status: OrderStatus) -> None:
        order = self.orders[order_id]
        self.orders[order_id] = order.model_copy(update={"status": status})

    async def save_order(self, order: Order) -> str:
        existing = await self.order_by_reference(order.reference)
        order_id = existing.id if existing else (order.id or _next_id("order", self.orders))
        self.orders[order_id] = order.model_copy(update={"id": order_id})
        return order_id

    async def next_reference(self, prefix: str) -> str:
        self._reference_seq += 1
        return f"{prefix.strip().upper()}-{self._reference_seq:04d}"

    async def save_order_if_mandate_version(self, order: Order, expected_version: int) -> bool:
        existing = self.orders.get(str(order.id))
        if existing is None or existing.mandate_version != expected_version:
            return False
        self.orders[str(order.id)] = order
        return True

    async def record_delivery(self, message: OutboundMessage, result: DeliveryResult) -> str:
        self.deliveries.append((message, result))
        return f"notification-{len(self.deliveries)}"


class FakeCallPlacer:
    """Records what would have been dialled. Never touches the network.

    Every test uses this. A real call costs credits and can ring a real phone, so the
    production placer is never constructed in the suite at all.
    """

    def __init__(self) -> None:
        self.dialled: list[tuple[str, dict[str, object]]] = []

    async def place(self, assistant: dict[str, object], to_number: str) -> str:
        self.dialled.append((to_number, assistant))
        return f"vapi-call-{len(self.dialled)}"


class RecordingNotifier:
    """A notifier whose outcome the test chooses.

    ``succeed=False`` is the interesting case: it is how the suite proves that a failed
    recap leaves a commitment unpromoted instead of quietly promoting it anyway.
    """

    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        self.sent.append(message)
        if not self.succeed:
            return DeliveryResult(status=DeliveryStatus.FAILED, error="provider refused")
        return DeliveryResult(
            status=DeliveryStatus.SENT, provider_message_id=f"msg-{len(self.sent)}"
        )


class ScriptedReportModel:
    """Returns a report the test wrote. No model call, no latency, no non-determinism."""

    def __init__(self, report: CallReport | None = None) -> None:
        self._report = report
        self.calls: list[str] = []

    async def report(self, call_id: str, turns: list[Turn], context: CallContext) -> CallReport:
        self.calls.append(call_id)
        if self._report is not None:
            return self._report.model_copy(update={"call_id": call_id})
        return CallReport(
            call_id=call_id,
            summary="scripted report",
            model="scripted",
        )


#: Convenience for tests that only need the enum member and not the whole state machine.
LIVE_COMMITMENT_STATES = tuple(s for s in CommitmentState if s not in COMMITMENT_DEAD_STATES)
