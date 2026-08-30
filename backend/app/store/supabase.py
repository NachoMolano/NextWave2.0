"""The Supabase implementation of domain.ports.Store.

MAY IMPORT:  domain, config.
IMPORTED BY: main (constructed), api and tools (through the Protocol).

The ONLY module in the codebase permitted to import a Supabase client. Everything else
depends on the Protocol, which is what lets four other tracks run against InMemoryStore with
no database and no network.

Two decisions worth reading before changing anything here.

**It uses the async client, not ``asyncio.to_thread``.** Phase 0's stub and BUILD_PLAN both
said to run a synchronous client in a worker thread. That guidance predates the pinned
version: ``supabase`` 2.31 ships ``AsyncClient`` on ``httpx.AsyncClient``. The thread route
would queue every store call behind the default executor's ``min(32, cpu + 4)`` cap while
PostgREST's own default timeout is 120 seconds, and ``to_thread`` is not cancellable -- a
caller that gives up leaves the thread running to completion. ``vapi/webhook.py`` has a hard
7.5-second budget for one ``carrier_by_phone`` lookup, so that is the wrong risk to accept.

**Construction never touches the network.** ``main.py`` builds this at import time and
``tests/test_seam.py`` constructs it with empty settings. The client is made on first use, so
a server with no database still boots and still answers ``/health``.

OWNER: Track C.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, cast

from supabase import AsyncClient

from app.config import Settings
from app.domain import (
    Approval,
    ApprovalStatus,
    CallRecord,
    CallReport,
    Carrier,
    Commitment,
    DecisionRow,
    DeliveryResult,
    EventRow,
    Money,
    Order,
    OrderStatus,
    OutboundMessage,
    QuoteRow,
    QuoteStatus,
)
from app.domain.models import COMMITMENT_DEAD_STATES, DELIVERY_UNDERWAY
from app.store.errors import AwardConflict, RowNotFound, StoreError, StoreUnavailable

__all__ = ["SupabaseStore"]

#: SQLSTATE for unique_violation. The only Postgres code this module reasons about.
_UNIQUE_VIOLATION = "23505"

#: Index name -> the exception it means. These names are ours; they are created in
#: supabase/migrations/0001_init.sql and nothing else may be added here without a matching
#: index. An unrecognised unique violation raises StoreError rather than being guessed at.
#:
#: Note both partial indexes map to AwardConflict, which is what InMemoryStore raises for the
#: one-live-commitment collision too. Splitting them into two exception types would be more
#: precise and would also break the shared contract suite, so it is a change for the day
#: somebody actually needs to tell them apart.
_CONFLICTS: dict[str, type[Exception]] = {
    "quotes_one_award_per_order": AwardConflict,
    "commitments_one_live_per_order": AwardConflict,
}


def _constraint_of(message: str) -> str:
    """The index name Postgres names in a unique-violation message.

    Matching on a *name we created* against a closed dictionary is categorically different
    from matching on the prose of an error. An unknown name falls through to StoreError.
    """
    marker = 'unique constraint "'
    start = message.find(marker)
    if start == -1:
        return ""
    start += len(marker)
    end = message.find('"', start)
    return message[start:end] if end != -1 else ""


@contextmanager
def _translate() -> Iterator[None]:
    """Turn a PostgREST error into a typed one, or re-raise it unchanged."""
    try:
        yield
    except Exception as exc:
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", None)
        if code is None and message is None:
            raise
        # APIError.code degrades to an int HTTP status when the error body is incomplete,
        # so compare as text rather than assuming a string.
        if str(code) == _UNIQUE_VIOLATION:
            name = _constraint_of(str(message or ""))
            conflict = _CONFLICTS.get(name)
            if conflict is not None:
                raise conflict(str(message)) from exc
            raise StoreError(f"unmapped unique constraint {name!r}: {message}") from exc
        raise StoreError(f"{code}: {message}") from exc


def _rows(response: object) -> list[dict[str, Any]]:
    """Narrow an untyped PostgREST body to rows, or fail loudly.

    ``response.data`` is typed as JSON and built without validation, so on a non-JSON body it
    is a string. Indexing it directly is how ``Any`` leaks into the rest of a strict codebase.
    """
    data = getattr(response, "data", None)
    if data is None:
        return []
    if not isinstance(data, list):
        raise StoreError(f"expected rows, got {type(data).__name__}")
    return cast(list[dict[str, Any]], data)


def _one(response: object) -> dict[str, Any] | None:
    rows = _rows(response)
    return rows[0] if rows else None


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _money(cents: object, currency: object) -> Money | None:
    if cents is None or currency is None:
        return None
    return Money(cents=int(cast(int, cents)), currency=str(currency))


# --------------------------------------------------------------------- row -> model
# Pydantic coerces the ISO strings PostgREST returns into datetime/date, so these pass the
# values straight through and only do work where a column and a field disagree in shape.


def _to_carrier(row: dict[str, Any]) -> Carrier:
    return Carrier.model_validate(row)


def _to_order(row: dict[str, Any]) -> Order:
    data = dict(row)
    data["cap"] = _money(row.get("cap_amount"), row.get("cap_currency"))
    data["target"] = _money(row.get("target_amount"), row.get("cap_currency"))
    return Order.model_validate(data)


def _to_call(row: dict[str, Any]) -> CallRecord:
    return CallRecord.model_validate(row)


def _to_quote(row: dict[str, Any]) -> QuoteRow:
    data = dict(row)
    data["amount"] = _money(row.get("amount_cents"), row.get("currency"))
    return QuoteRow.model_validate(data)


def _to_approval(row: dict[str, Any]) -> Approval:
    return Approval.model_validate(row)


def _to_commitment(row: dict[str, Any]) -> Commitment:
    return Commitment.model_validate(row)


def _to_report(row: dict[str, Any]) -> CallReport:
    return CallReport.model_validate(row)


# --------------------------------------------------------------------- model -> row


def _order_row(order: Order) -> dict[str, Any]:
    row: dict[str, Any] = {
        "reference": order.reference,
        "status": str(order.status),
        "origin": order.origin,
        "destination": order.destination,
        "cargo": order.cargo,
        "equipment": order.equipment,
        "weight": order.weight,
        "container_number": order.container_number,
        "discharged_at": _iso(order.discharged_at),
        "free_days": order.free_days,
        "last_free_day": _iso(order.last_free_day),
        "delivery_deadline": _iso(order.delivery_deadline),
        "cap_amount": order.cap.cents if order.cap else None,
        "cap_currency": order.cap.currency if order.cap else None,
        "target_amount": order.target.cents if order.target else None,
        "pickup_not_before": _iso(order.pickup_not_before),
        "pickup_not_after": _iso(order.pickup_not_after),
        "commitment_mode": str(order.commitment_mode),
        "mandate_version": order.mandate_version,
        "mandate_set_by": order.mandate_set_by,
        "mandate_set_at": _iso(order.mandate_set_at),
        "assigned_carrier_id": order.assigned_carrier_id,
        "awarded_quote_id": order.awarded_quote_id,
        "expected_driver": order.expected_driver,
        "expected_plate": order.expected_plate,
        "payload": order.payload,
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    if order.id:
        row["id"] = order.id
    return row


def _call_row(call: CallRecord) -> dict[str, Any]:
    row: dict[str, Any] = {
        "vapi_call_id": call.vapi_call_id,
        "direction": str(call.direction),
        "phase": str(call.phase),
        "status": str(call.status),
        "order_id": call.order_id,
        "carrier_id": call.carrier_id,
        "from_number": call.from_number,
        "to_number": call.to_number,
        "started_at": _iso(call.started_at),
        "ended_at": _iso(call.ended_at),
        "ended_reason": call.ended_reason,
        "recording_url": call.recording_url,
        "transcript": [t.model_dump(mode="json") for t in call.transcript],
        "context": call.context,
        "identity_verified": call.identity_verified,
        "identity_level": call.identity_level,
        "cost_cents": call.cost_cents,
    }
    if call.id:
        row["id"] = call.id
    return row


def _quote_row(quote: QuoteRow) -> dict[str, Any]:
    row: dict[str, Any] = {
        "order_id": quote.order_id,
        "carrier_id": quote.carrier_id,
        "call_id": quote.call_id,
        "anchor_ms": quote.anchor_ms,
        "amount_cents": quote.amount.cents,
        "currency": quote.amount.currency,
        "components": quote.components,
        "cost_is_final": quote.cost_is_final,
        "pickup_at": _iso(quote.pickup_at),
        "pickup_window_end": _iso(quote.pickup_window_end),
        "equipment": quote.equipment,
        "valid_until": _iso(quote.valid_until),
        "all_in_usd_cents": quote.all_in_usd_cents,
        "status": str(quote.status),
        "superseded_by": quote.superseded_by,
        "carrier_confirmed_exact_recap": quote.carrier_confirmed_exact_recap,
        "confirmed_at": _iso(quote.confirmed_at),
        "claimed_identity": quote.claimed_identity,
        "identity_level": quote.identity_level,
    }
    if quote.id:
        row["id"] = quote.id
    return row


class SupabaseStore:
    """Implements domain.ports.Store."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.supabase_url
        self._key = settings.supabase_secret_key
        self._client: AsyncClient | None = None

    # --- the client ---------------------------------------------------------------

    @property
    def _db(self) -> AsyncClient:
        """Build on first use. A missing configuration is a 503, never a failed boot."""
        if self._client is None:
            if not self._url or not self._key:
                raise StoreUnavailable(
                    "SUPABASE_URL and SUPABASE_SECRET_KEY are not configured; "
                    "the store cannot be reached"
                )
            try:
                self._client = AsyncClient(self._url, self._key)
            except Exception as exc:
                raise StoreUnavailable(f"could not build a Supabase client: {exc}") from exc
        return self._client

    # --- reads --------------------------------------------------------------------

    async def order(self, order_id: str) -> Order | None:
        with _translate():
            res = await self._db.table("orders").select("*").eq("id", order_id).execute()
        row = _one(res)
        return _to_order(row) if row else None

    async def order_by_reference(self, reference: str) -> Order | None:
        with _translate():
            res = await self._db.table("orders").select("*").eq("reference", reference).execute()
        row = _one(res)
        return _to_order(row) if row else None

    async def list_orders(self) -> list[Order]:
        """Not on the Protocol. GET /api/orders needs a list, and reads go straight to store/.

        Named ``list_orders`` rather than ``orders`` because ``InMemoryStore.orders`` is a
        dict attribute; a method of the same name could not be satisfied by both.
        """
        with _translate():
            res = (
                await self._db.table("orders").select("*").order("created_at", desc=True).execute()
            )
        return [_to_order(r) for r in _rows(res)]

    async def carrier(self, carrier_id: str) -> Carrier | None:
        with _translate():
            res = await self._db.table("carriers").select("*").eq("id", carrier_id).execute()
        row = _one(res)
        return _to_carrier(row) if row else None

    async def carrier_by_phone(self, phone: str) -> Carrier | None:
        with _translate():
            res = await self._db.table("carriers").select("*").eq("phone", phone).execute()
        row = _one(res)
        return _to_carrier(row) if row else None

    async def list_carriers(self) -> list[Carrier]:
        """Not on the Protocol. GET /api/carriers. Named to avoid InMemoryStore.carriers."""
        with _translate():
            res = await self._db.table("carriers").select("*").order("name").execute()
        return [_to_carrier(r) for r in _rows(res)]

    async def save_carrier(self, carrier: Carrier) -> str:
        """Not on the Protocol. Create or update by phone, for scripts/seed.py.

        Upsert on ``phone`` rather than ``id`` because the phone is the natural key here: it
        is the unique index an inbound call is correlated on, and re-seeding must not create a
        second carrier reachable at the same number.
        """
        row: dict[str, Any] = {
            "name": carrier.name,
            "phone": carrier.phone,
            "contact_name": carrier.contact_name,
            "email": carrier.email,
            "whatsapp": carrier.whatsapp,
            "is_on_file": carrier.is_on_file,
            "is_active": carrier.is_active,
            "persona": carrier.persona,
        }
        if carrier.id:
            row["id"] = carrier.id
        with _translate():
            res = await self._db.table("carriers").upsert(row, on_conflict="phone").execute()
        created = _one(res)
        if created is None:
            raise StoreError("upsert into carriers returned no row")
        return str(created["id"])

    async def carriers_for_rfq(self, limit: int) -> list[Carrier]:
        with _translate():
            res = (
                await self._db.table("carriers")
                .select("*")
                .eq("is_on_file", True)
                .eq("is_active", True)
                .not_.is_("phone", "null")
                .order("id")
                .limit(limit)
                .execute()
            )
        return [_to_carrier(r) for r in _rows(res)]

    async def call(self, call_id: str) -> CallRecord | None:
        with _translate():
            res = await self._db.table("calls").select("*").eq("id", call_id).execute()
        row = _one(res)
        return _to_call(row) if row else None

    async def call_by_vapi_id(self, vapi_call_id: str) -> CallRecord | None:
        with _translate():
            res = (
                await self._db.table("calls").select("*").eq("vapi_call_id", vapi_call_id).execute()
            )
        row = _one(res)
        return _to_call(row) if row else None

    async def calls_for(self, order_id: str) -> list[CallRecord]:
        """Not on the Protocol. GET /api/calls?order_id=."""
        with _translate():
            res = (
                await self._db.table("calls")
                .select("*")
                .eq("order_id", order_id)
                .order("started_at", desc=True)
                .execute()
            )
        return [_to_call(r) for r in _rows(res)]

    async def quotes_for(self, order_id: str) -> list[QuoteRow]:
        with _translate():
            res = await self._db.table("quotes").select("*").eq("order_id", order_id).execute()
        return [_to_quote(r) for r in _rows(res)]

    async def quote(self, quote_id: str) -> QuoteRow | None:
        with _translate():
            res = await self._db.table("quotes").select("*").eq("id", quote_id).execute()
        row = _one(res)
        return _to_quote(row) if row else None

    async def live_commitment(self, order_id: str) -> Commitment | None:
        dead = [str(s) for s in COMMITMENT_DEAD_STATES]
        with _translate():
            res = (
                await self._db.table("commitments")
                .select("*")
                .eq("order_id", order_id)
                .not_.in_("state", dead)
                .execute()
            )
        row = _one(res)
        return _to_commitment(row) if row else None

    async def due_for_chase(self, now: datetime) -> list[Order]:
        underway = [str(s) for s in DELIVERY_UNDERWAY]
        with _translate():
            res = (
                await self._db.table("orders")
                .select("*")
                .lt("delivery_deadline", now.isoformat())
                .not_.in_("status", underway)
                .execute()
            )
        return [_to_order(r) for r in _rows(res)]

    # --- writes -------------------------------------------------------------------

    async def upsert_call(self, call: CallRecord) -> str:
        """Create or update by ``vapi_call_id``, without letting a late webhook erase evidence.

        ports.py is explicit that a ``status-update`` can arrive *after* the
        ``end-of-call-report``. A plain replace would then blank the transcript and the
        recording URL with the empty values a status-update carries, so a merge keeps any
        stored value the incoming record does not positively supply.
        """
        existing = await self.call_by_vapi_id(call.vapi_call_id)
        row = _call_row(call)
        if existing is None:
            with _translate():
                res = await self._db.table("calls").insert(row).execute()
            created = _one(res)
            if created is None:
                raise StoreError("insert into calls returned no row")
            return str(created["id"])

        merged = {k: v for k, v in row.items() if v not in (None, [], {})}
        merged.pop("id", None)
        with _translate():
            res = await self._db.table("calls").update(merged).eq("id", existing.id).execute()
        if not _rows(res):
            raise RowNotFound(f"call {existing.id} vanished during upsert")
        return str(existing.id)

    async def add_quote(self, quote: QuoteRow) -> str:
        with _translate():
            res = await self._db.table("quotes").insert(_quote_row(quote)).execute()
        row = _one(res)
        if row is None:
            raise StoreError("insert into quotes returned no row")
        return str(row["id"])

    async def supersede_quote(self, old_quote_id: str, new_quote_id: str) -> None:
        """Link the earlier quote to the later one. Never edits what was said.

        One statement, so it is atomic in itself. It is NOT atomic with the insert of the new
        quote, which ports.py asks for and PostgREST cannot give across two Protocol methods
        -- see CHANGELOG. Both rows always survive either way; the exposure is a one
        round-trip window in which an order shows two live quotes from one carrier, and a
        repeat of this call closes it. Written to be repeatable for exactly that reason.
        """
        with _translate():
            res = (
                await self._db.table("quotes")
                .update({"status": str(QuoteStatus.SUPERSEDED), "superseded_by": new_quote_id})
                .eq("id", old_quote_id)
                .execute()
            )
        if not _rows(res):
            raise RowNotFound(f"quote {old_quote_id} not found")

    async def accept_quote(self, order_id: str, quote_id: str) -> None:
        """Award. The partial unique index is the enforcement, not a read-then-write.

        Two confirmations landing together must not both see an empty slot, so nothing here
        checks first: the second UPDATE violates ``quotes_one_award_per_order`` and comes back
        as AwardConflict.
        """
        with _translate():
            res = (
                await self._db.table("quotes")
                .update({"status": str(QuoteStatus.ACCEPTED)})
                .eq("id", quote_id)
                .eq("order_id", order_id)
                .execute()
            )
        if not _rows(res):
            raise RowNotFound(f"quote {quote_id} not found on order {order_id}")

    async def record_decision(self, decision: DecisionRow) -> str:
        row: dict[str, Any] = {
            "order_id": decision.order_id,
            "call_id": decision.call_id,
            "quote_id": decision.quote_id,
            "proposal": decision.proposal,
            "outcome": decision.outcome,
            "reason_code": decision.reason_code,
            "cap_at_decision_cents": decision.cap_at_decision_cents,
            "cap_currency": decision.cap_currency,
            "mandate_version": decision.mandate_version,
            "decided_at": _iso(decision.decided_at),
        }
        with _translate():
            res = await self._db.table("decisions").insert(row).execute()
        created = _one(res)
        if created is None:
            raise StoreError("insert into decisions returned no row")
        return str(created["id"])

    async def append_event(self, event: EventRow) -> bool:
        """``on conflict (idempotency_key) do nothing``. False means the key was already seen.

        ``ignore_duplicates=True`` is a privilege requirement here, not a style choice: the
        default emits ``on conflict do update``, which needs UPDATE on ``events`` -- revoked
        in 0001 -- and would fail 42501. ``do nothing`` needs only INSERT.

        RETURNING on DO NOTHING emits only rows that were really inserted, so an empty body is
        an exact signal rather than a heuristic. It is exact *because* the backend holds the
        service key and bypasses RLS; under a policy-bound role a SELECT filter could hide a
        row that was in fact written.
        """
        row: dict[str, Any] = {
            "order_id": event.order_id,
            "call_id": event.call_id,
            "type": event.type,
            "payload": event.payload,
            "idempotency_key": event.idempotency_key,
        }
        with _translate():
            res = (
                await self._db.table("events")
                .upsert(row, on_conflict="idempotency_key", ignore_duplicates=True)
                .execute()
            )
        written = _rows(res)
        if len(written) > 1:
            raise StoreError("append_event inserted more than one row")
        return len(written) == 1

    async def raise_approval(self, approval: Approval) -> str:
        row: dict[str, Any] = {
            "order_id": approval.order_id,
            "call_id": approval.call_id,
            "kind": str(approval.kind),
            "reason": str(approval.reason),
            "context": approval.context,
            "status": str(approval.status),
            "decided_at": _iso(approval.decided_at),
            "decided_by": approval.decided_by,
            "note": approval.note,
        }
        if approval.raised_at:
            row["raised_at"] = _iso(approval.raised_at)
        with _translate():
            res = await self._db.table("approvals").insert(row).execute()
        created = _one(res)
        if created is None:
            raise StoreError("insert into approvals returned no row")
        return str(created["id"])

    async def approval(self, approval_id: str) -> Approval | None:
        with _translate():
            res = await self._db.table("approvals").select("*").eq("id", approval_id).execute()
        row = _one(res)
        return _to_approval(row) if row else None

    async def resolve_approval(
        self, approval_id: str, *, status: str, decided_by: str, note: str | None = None
    ) -> None:
        """Close one item in the human inbox.

        ``decided_at`` is stamped here rather than taken from the caller because the
        ``decided_has_decider`` check refuses any non-open row without one. InMemoryStore does
        not set it, which is a divergence raised in CHANGELOG -- the database is right.
        """
        update: dict[str, Any] = {
            "status": str(ApprovalStatus(status)),
            "decided_by": decided_by,
            "note": note,
            "decided_at": datetime.now().astimezone().isoformat(),
        }
        with _translate():
            res = await self._db.table("approvals").update(update).eq("id", approval_id).execute()
        if not _rows(res):
            raise RowNotFound(f"approval {approval_id} not found")

    async def open_approvals(self, order_id: str | None = None) -> list[Approval]:
        with _translate():
            query = (
                self._db.table("approvals")
                .select("*")
                .eq("status", str(ApprovalStatus.OPEN))
                .order("raised_at", desc=True)
            )
            if order_id is not None:
                query = query.eq("order_id", order_id)
            res = await query.execute()
        return [_to_approval(r) for r in _rows(res)]

    async def save_report(self, report: CallReport) -> None:
        row: dict[str, Any] = {
            "call_id": report.call_id,
            "summary": report.summary,
            "subject": str(report.subject),
            "severity": str(report.severity),
            "actions": report.actions,
            "mentions": report.mentions,
            "quoted_prices": report.quoted_prices,
            "objections": report.objections,
            "conditions": report.conditions,
            "agreement_candidates": report.agreement_candidates,
            "model": report.model,
        }
        if report.generated_at:
            row["generated_at"] = _iso(report.generated_at)
        with _translate():
            await self._db.table("call_reports").upsert(row, on_conflict="call_id").execute()

    async def report_for(self, call_id: str) -> CallReport | None:
        with _translate():
            res = await self._db.table("call_reports").select("*").eq("call_id", call_id).execute()
        row = _one(res)
        return _to_report(row) if row else None

    async def save_commitment(self, commitment: Commitment) -> str:
        """Insert. ``commitments_one_live_per_order`` is what refuses a second live one."""
        row: dict[str, Any] = {
            "order_id": commitment.order_id,
            "quote_id": commitment.quote_id,
            "state": str(commitment.state),
            "evidence_call_id": commitment.evidence_call_id,
            "evidence_anchor_ms": commitment.evidence_anchor_ms,
            "terms": commitment.terms,
            "canonical_sha256": commitment.canonical_sha256,
            "claimed_identity": commitment.claimed_identity,
            "identity_level": commitment.identity_level,
            "superseded_by": commitment.superseded_by,
            "approval_id": commitment.approval_id,
        }
        with _translate():
            res = await self._db.table("commitments").insert(row).execute()
        created = _one(res)
        if created is None:
            raise StoreError("insert into commitments returned no row")
        return str(created["id"])

    async def update_commitment(self, commitment: Commitment) -> None:
        if commitment.id is None:
            raise ValueError("cannot update a commitment with no id")
        update: dict[str, Any] = {
            "state": str(commitment.state),
            "terms": commitment.terms,
            "canonical_sha256": commitment.canonical_sha256,
            "superseded_by": commitment.superseded_by,
            "approval_id": commitment.approval_id,
        }
        with _translate():
            res = (
                await self._db.table("commitments").update(update).eq("id", commitment.id).execute()
            )
        if not _rows(res):
            raise RowNotFound(f"commitment {commitment.id} not found")

    async def set_order_status(self, order_id: str, status: OrderStatus) -> None:
        with _translate():
            res = (
                await self._db.table("orders")
                .update(
                    {
                        "status": str(status),
                        "updated_at": datetime.now().astimezone().isoformat(),
                    }
                )
                .eq("id", order_id)
                .execute()
            )
        if not _rows(res):
            raise RowNotFound(f"order {order_id} not found")

    async def save_order(self, order: Order) -> str:
        """Create or update by ``reference``. Idempotent, so a re-delivered intake is safe."""
        with _translate():
            res = (
                await self._db.table("orders")
                .upsert(_order_row(order), on_conflict="reference")
                .execute()
            )
        row = _one(res)
        if row is None:
            raise StoreError("upsert into orders returned no row")
        return str(row["id"])

    async def record_delivery(self, message: OutboundMessage, result: DeliveryResult) -> str:
        row: dict[str, Any] = {
            "order_id": message.order_id,
            "call_id": message.call_id,
            "commitment_id": message.commitment_id,
            "approval_id": message.approval_id,
            "channel": str(message.channel),
            "to_address": message.to_address,
            "subject": message.subject,
            "body": message.body,
            "status": str(result.status),
            "provider_message_id": result.provider_message_id,
            "error": result.error,
            "sent_at": _iso(result.sent_at),
        }
        with _translate():
            res = await self._db.table("notifications").insert(row).execute()
        created = _one(res)
        if created is None:
            raise StoreError("insert into notifications returned no row")
        return str(created["id"])
