"""The seams between the tracks. Four Protocols, and nothing else.

MAY IMPORT:  stdlib, typing, domain. Nothing else from app.
IMPORTED BY: tools, jobs, main (as parameters); implemented by store, vapi, notify, agent.

This file is why five people can build at once. Each Protocol is implemented by exactly one
package and faked in ``tests/fakes.py``, so a track can be finished, tested and merged
before the packages it talks to exist.

Changing a signature here is a cross-track event: it goes in CHANGELOG.md with an
``-> Affects:`` line before it goes in the code.

They are Protocols rather than base classes on purpose. A base class would have to be
imported by the implementer, and ``store/`` importing ``domain/`` to inherit from it is a
dependency pointing the wrong way for something whose only job is to describe a shape.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.context import CallContext
from app.domain.models import (
    Approval,
    CallRecord,
    CallReport,
    Carrier,
    Commitment,
    DecisionRow,
    DeliveryResult,
    EventRow,
    Order,
    OrderStatus,
    OutboundMessage,
    QuoteRow,
    Turn,
)

__all__ = ["CallPlacer", "Notifier", "ReportModel", "Store"]


@runtime_checkable
class Store(Protocol):
    """Persistence and evidence. Implemented by ``store/``; faked by ``InMemoryStore``.

    Every method is async because the real implementation runs a synchronous Supabase client
    in a worker thread, and a caller must not be able to tell the difference.
    """

    # --- reads ---------------------------------------------------------------------

    async def order(self, order_id: str) -> Order | None: ...

    async def order_by_reference(self, reference: str) -> Order | None: ...

    async def carrier(self, carrier_id: str) -> Carrier | None: ...

    async def carrier_by_phone(self, phone: str) -> Carrier | None:
        """Correlate an inbound call. ``None`` means the number is not on file, which is an
        answer in itself: the agent gives nothing away and records claims as unverified."""
        ...

    async def carriers_for_rfq(self, limit: int) -> list[Carrier]:
        """Carriers eligible to be called: on file, active, with a phone number."""
        ...

    async def call(self, call_id: str) -> CallRecord | None: ...

    async def call_by_vapi_id(self, vapi_call_id: str) -> CallRecord | None: ...

    async def quotes_for(self, order_id: str) -> list[QuoteRow]: ...

    async def quote(self, quote_id: str) -> QuoteRow | None: ...

    async def live_commitment(self, order_id: str) -> Commitment | None:
        """The one commitment occupying the order's slot, if any."""
        ...

    async def due_for_chase(self, now: datetime) -> list[Order]:
        """Orders whose delivery deadline has passed with nothing underway. OUTBOUND 2."""
        ...

    # --- writes --------------------------------------------------------------------

    async def upsert_call(self, call: CallRecord) -> str:
        """Create or update by ``vapi_call_id``. Returns the call id.

        Upsert rather than insert because Vapi redelivers webhooks and they arrive out of
        order: a ``status-update`` can land after the ``end-of-call-report``.
        """
        ...

    async def add_quote(self, quote: QuoteRow) -> str: ...

    async def supersede_quote(self, old_quote_id: str, new_quote_id: str) -> None:
        """Mark the earlier quote superseded and point it at the later one.

        Must be one transaction with the insert of the new quote. Two statements that can
        half-apply would leave an order with two live quotes from one carrier.
        """
        ...

    async def accept_quote(self, order_id: str, quote_id: str) -> None:
        """Award. Raises ``AwardConflict`` if this order already has an accepted quote.

        The check is the partial unique index, not a read-then-write: two calls confirming
        at the same moment must not both see an empty slot.
        """
        ...

    async def record_decision(self, decision: DecisionRow) -> str: ...

    async def append_event(self, event: EventRow) -> bool:
        """Append to the ledger. Returns ``False`` if the idempotency key was already seen.

        Every mutating path calls this first and stops on ``False``. That is the whole
        idempotency mechanism, and it is why it returns a bool instead of raising: a
        redelivered webhook is a normal event, not an error.
        """
        ...

    async def raise_approval(self, approval: Approval) -> str: ...

    async def approval(self, approval_id: str) -> Approval | None: ...

    async def resolve_approval(
        self, approval_id: str, *, status: str, decided_by: str, note: str | None = None
    ) -> None: ...

    async def open_approvals(self, order_id: str | None = None) -> list[Approval]: ...

    async def save_report(self, report: CallReport) -> None: ...

    async def report_for(self, call_id: str) -> CallReport | None: ...

    async def save_commitment(self, commitment: Commitment) -> str: ...

    async def update_commitment(self, commitment: Commitment) -> None: ...

    async def set_order_status(self, order_id: str, status: OrderStatus) -> None: ...

    async def save_order(self, order: Order) -> str:
        """Create or update by ``reference``. Returns the order id."""
        ...

    async def record_delivery(self, message: OutboundMessage, result: DeliveryResult) -> str: ...


@runtime_checkable
class CallPlacer(Protocol):
    """Places a real phone call. Implemented by ``vapi/client.py``.

    The only interface in the system that spends money, which is why it is one method: a
    test that fakes this cannot accidentally dial a real number.
    """

    async def place(self, assistant: dict[str, object], to_number: str) -> str:
        """Dial ``to_number`` with a transient assistant. Returns the provider call id."""
        ...


@runtime_checkable
class Notifier(Protocol):
    """Sends the written word. Implemented by ``notify/``.

    Never raises. A failure comes back as ``DeliveryResult(status=FAILED)`` so the caller
    can leave a commitment unpromoted -- a failed recap means there was no commitment, and
    an exception here would turn that into a crash instead of a state.
    """

    async def send(self, message: OutboundMessage) -> DeliveryResult: ...


@runtime_checkable
class ReportModel(Protocol):
    """Turns a finished transcript into a structured brief. Implemented by ``agent/``.

    Runs after the call with no latency budget. What it returns is evidence for a human and
    an input to policy -- never an authorization by itself.
    """

    async def report(self, call_id: str, turns: list[Turn], context: CallContext) -> CallReport: ...
