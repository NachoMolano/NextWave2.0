"""The Supabase implementation of domain.ports.Store.

This is the ONLY module in the codebase permitted to import a Supabase client. Everything
else depends on the Protocol, which is what lets four other tracks run against
InMemoryStore with no database and no network.

The Supabase Python client is synchronous. Every method here must hand it to a worker
thread (``asyncio.to_thread``) so a slow query cannot block the event loop while a webhook
is waiting -- Vapi's assistant-request deadline is 7.5 seconds and is not configurable.

STATUS: Phase 0 stub. Track C implements. Three behaviours the other tracks depend on:
  * ``append_event`` returns False on idempotency-key conflict (insert ... on conflict
    (idempotency_key) do nothing, then check the affected row count). Callers stop on False.
  * ``add_quote`` + ``supersede_quote`` are one transaction.
  * ``accept_quote`` surfaces the partial-unique-index violation as ``AwardConflict``,
    never as a raw Postgres error.

OWNER: Track C.
"""

from datetime import datetime

from app.config import Settings
from app.domain import (
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
)

__all__ = ["SupabaseStore"]

_UNIMPLEMENTED = "Track C: implement app/store/supabase.py against domain.ports.Store"


class SupabaseStore:
    """Implements domain.ports.Store.

    Construction must not touch the network. main.py builds this at import time, and a
    server that cannot boot without a reachable database cannot serve /health -- which is
    the endpoint you need most when the database is unreachable.
    """

    def __init__(self, settings: Settings) -> None:
        self._url = settings.supabase_url
        self._key = settings.supabase_secret_key

    async def order(self, order_id: str) -> Order | None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def order_by_reference(self, reference: str) -> Order | None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def carrier(self, carrier_id: str) -> Carrier | None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def carrier_by_phone(self, phone: str) -> Carrier | None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def carriers_for_rfq(self, limit: int) -> list[Carrier]:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def call(self, call_id: str) -> CallRecord | None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def call_by_vapi_id(self, vapi_call_id: str) -> CallRecord | None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def quotes_for(self, order_id: str) -> list[QuoteRow]:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def quote(self, quote_id: str) -> QuoteRow | None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def live_commitment(self, order_id: str) -> Commitment | None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def commitment(self, commitment_id: str) -> Commitment | None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def due_for_chase(self, now: datetime) -> list[Order]:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def orders_in_status(self, status: OrderStatus) -> list[Order]:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def upsert_call(self, call: CallRecord) -> str:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def add_quote(self, quote: QuoteRow) -> str:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def supersede_quote(self, old_quote_id: str, new_quote_id: str) -> None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def accept_quote(self, order_id: str, quote_id: str) -> None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def record_decision(self, decision: DecisionRow) -> str:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def append_event(self, event: EventRow) -> bool:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def raise_approval(self, approval: Approval) -> str:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def approval(self, approval_id: str) -> Approval | None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def resolve_approval(
        self, approval_id: str, *, status: str, decided_by: str, note: str | None = None
    ) -> None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def open_approvals(self, order_id: str | None = None) -> list[Approval]:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def save_report(self, report: CallReport) -> None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def report_for(self, call_id: str) -> CallReport | None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def save_commitment(self, commitment: Commitment) -> str:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def update_commitment(self, commitment: Commitment) -> None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def set_order_status(self, order_id: str, status: OrderStatus) -> None:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def save_order(self, order: Order) -> str:
        raise NotImplementedError(_UNIMPLEMENTED)

    async def record_delivery(self, message: OutboundMessage, result: DeliveryResult) -> str:
        raise NotImplementedError(_UNIMPLEMENTED)
