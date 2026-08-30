"""One suite, two implementations, so the fake and the database cannot drift apart.

Every test here runs twice: against ``InMemoryStore``, which four other tracks develop
against, and against the real Supabase client. A fake that is more permissive than the
database is worse than no fake -- it makes a green suite mean nothing -- and the only way to
know they agree is to ask them the same questions.

By default only the in-memory half runs, so ``uv run pytest`` needs no network, no database
and no phone. The live half is opt-in:

    VOLTA_LIVE_STORE_TESTS=1 SUPABASE_URL=... SUPABASE_SECRET_KEY=... uv run pytest

It is opt-in rather than automatic on ``SUPABASE_URL`` because the configured project is also
the demo database. These tests write real rows; ``decisions`` and ``events`` are append-only
by grant, so what they append cannot be deleted afterwards. Every row is namespaced under a
random reference and cleaned up in foreign-key order where the grants allow it.

OWNER: Track C.
"""

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Protocol

import pytest

from app.config import Settings
from app.domain import (
    Approval,
    ApprovalKind,
    ApprovalReason,
    ApprovalStatus,
    AwardConflict,
    CallDirection,
    CallRecord,
    CallStatus,
    Carrier,
    Commitment,
    CommitmentState,
    EventRow,
    Money,
    Order,
    OrderStatus,
    QuoteRow,
    QuoteStatus,
    Store,
)
from app.store import RowNotFound, SupabaseStore
from tests.fakes import InMemoryStore

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

_LIVE = os.environ.get("VOLTA_LIVE_STORE_TESTS") == "1" and bool(os.environ.get("SUPABASE_URL"))
_LIVE_REASON = (
    "live store tests are opt-in: set VOLTA_LIVE_STORE_TESTS=1 with SUPABASE_URL and "
    "SUPABASE_SECRET_KEY. They write to the configured project, and decisions/events are "
    "append-only so their rows cannot be removed afterwards."
)


class World(Protocol):
    """The rows a test needs before it can say anything interesting."""

    store: Store
    carrier: Carrier
    other_carrier: Carrier
    order: Order
    call_id: str


class _World:
    def __init__(
        self,
        store: Store,
        carrier: Carrier,
        other_carrier: Carrier,
        order: Order,
        call_id: str,
    ) -> None:
        self.store = store
        self.carrier = carrier
        self.other_carrier = other_carrier
        self.order = order
        self.call_id = call_id


def _carrier(tag: str, *, on_file: bool = True, active: bool = True) -> Carrier:
    return Carrier(
        id="",
        name=f"Fletes {tag}",
        phone=f"+5213141{tag[:5]}",
        contact_name="Luis Ramirez",
        is_on_file=on_file,
        is_active=active,
        persona="cheap and slow",
    )


def _order(reference: str) -> Order:
    return Order(
        id="",
        reference=reference,
        status=OrderStatus.RECEIVED,
        origin="Manzanillo",
        destination="Guadalajara",
        cargo="Textiles",
        equipment="40-foot container chassis",
        container_number="MSCU1234566",
        cap=Money(cents=900_000, currency="USD"),
        target=Money(cents=820_000, currency="USD"),
        pickup_not_before=NOW,
        pickup_not_after=NOW + timedelta(days=2),
        mandate_version=1,
        mandate_set_by="ops@volta.test",
        mandate_set_at=NOW,
    )


def _quote(world: World, cents: int, *, carrier_id: str | None = None) -> QuoteRow:
    return QuoteRow(
        order_id=str(world.order.id),
        carrier_id=carrier_id or world.carrier.id,
        call_id=world.call_id,
        anchor_ms=42_000,
        amount=Money(cents=cents, currency="USD"),
        pickup_at=NOW + timedelta(days=1),
        equipment="40-foot container chassis",
        valid_until=NOW + timedelta(days=1),
    )


async def _build_memory_world() -> AsyncIterator[World]:
    store = InMemoryStore()
    one = store.add_carrier(_carrier("aaaaa").model_copy(update={"id": "carrier-1"}))
    two = store.add_carrier(_carrier("bbbbb").model_copy(update={"id": "carrier-2"}))
    order = store.add_order(_order("OP-TEST-0001").model_copy(update={"id": "order-1"}))
    call_id = await store.upsert_call(
        CallRecord(
            vapi_call_id="vapi-1",
            direction=CallDirection.OUTBOUND,
            phase="rfq",
            order_id=order.id,
            carrier_id=one.id,
        )
    )
    yield _World(store, one, two, order, call_id)


async def _build_live_world() -> AsyncIterator[World]:
    store = SupabaseStore(Settings())
    tag = uuid.uuid4().hex[:10]
    one_id = await store.save_carrier(_carrier(tag))
    two_id = await store.save_carrier(_carrier(uuid.uuid4().hex[:10]))
    one = await store.carrier(one_id)
    two = await store.carrier(two_id)
    assert one is not None and two is not None
    order_id = await store.save_order(_order(f"OP-TEST-{tag}"))
    order = await store.order(order_id)
    assert order is not None
    call_id = await store.upsert_call(
        CallRecord(
            vapi_call_id=f"vapi-{tag}",
            direction=CallDirection.OUTBOUND,
            phase="rfq",
            order_id=order_id,
            carrier_id=one_id,
        )
    )
    try:
        yield _World(store, one, two, order, call_id)
    finally:
        # Foreign-key order, and only what the grants allow. decisions and events stay:
        # revoking DELETE is the point of them, so a test cannot tidy away its own audit
        # trail any more than the backend can.
        db = store._db
        for table, column, value in (
            ("commitments", "order_id", order_id),
            ("quotes", "order_id", order_id),
            ("notifications", "order_id", order_id),
            ("call_reports", "call_id", call_id),
            ("approvals", "order_id", order_id),
            ("calls", "id", call_id),
            ("orders", "id", order_id),
            ("carriers", "id", one_id),
            ("carriers", "id", two_id),
        ):
            await db.table(table).delete().eq(column, value).execute()


@pytest.fixture(
    params=[
        pytest.param("memory", id="InMemoryStore"),
        pytest.param(
            "live",
            id="SupabaseStore",
            marks=pytest.mark.skipif(not _LIVE, reason=_LIVE_REASON),
        ),
    ]
)
async def world(request: pytest.FixtureRequest) -> AsyncIterator[World]:
    builder = _build_memory_world if request.param == "memory" else _build_live_world
    async for built in builder():
        yield built


# ------------------------------------------------------------------ the three invariants


async def test_a_repeated_idempotency_key_is_refused(world: World) -> None:
    """The whole idempotency mechanism. Every mutating path stops on False."""
    key = f"chase:{world.order.id}:{uuid.uuid4().hex}"
    event = EventRow(order_id=world.order.id, type="order.chased", idempotency_key=key)

    assert await world.store.append_event(event) is True
    assert await world.store.append_event(event) is False


async def test_a_second_award_is_refused_by_the_database(world: World) -> None:
    """Two open bookings is the worst outcome in the brief. One index prevents it."""
    first = await world.store.add_quote(_quote(world, 850_000))
    second = await world.store.add_quote(_quote(world, 870_000, carrier_id=world.other_carrier.id))

    await world.store.accept_quote(str(world.order.id), first)

    with pytest.raises(AwardConflict):
        await world.store.accept_quote(str(world.order.id), second)

    quotes = {q.id: q for q in await world.store.quotes_for(str(world.order.id))}
    assert quotes[first].status is QuoteStatus.ACCEPTED
    assert quotes[second].status is QuoteStatus.PROPOSED


async def test_superseding_a_quote_keeps_both_rows(world: World) -> None:
    """They said 8,500 and then they said 9,200. Both were said."""
    old = await world.store.add_quote(_quote(world, 850_000))
    new = await world.store.add_quote(_quote(world, 920_000))

    await world.store.supersede_quote(old, new)

    quotes = {q.id: q for q in await world.store.quotes_for(str(world.order.id))}
    assert len(quotes) == 2
    assert quotes[old].amount == Money(cents=850_000, currency="USD")
    assert quotes[old].status is QuoteStatus.SUPERSEDED
    assert quotes[old].superseded_by == new
    assert quotes[new].amount == Money(cents=920_000, currency="USD")


async def test_supersede_is_repeatable(world: World) -> None:
    """It is not atomic with the insert, so it has to be safe to run again. See CHANGELOG."""
    old = await world.store.add_quote(_quote(world, 850_000))
    new = await world.store.add_quote(_quote(world, 920_000))

    await world.store.supersede_quote(old, new)
    await world.store.supersede_quote(old, new)

    quotes = {q.id: q for q in await world.store.quotes_for(str(world.order.id))}
    assert quotes[old].superseded_by == new
    assert len(quotes) == 2


async def _commitment(world: World, state: CommitmentState = CommitmentState.VERBAL) -> Commitment:
    quote_id = await world.store.add_quote(_quote(world, 850_000))
    return Commitment(
        order_id=str(world.order.id),
        quote_id=quote_id,
        state=state,
        evidence_call_id=world.call_id,
        evidence_anchor_ms=61_500,
    )


async def test_only_one_commitment_can_be_live_at_a_time(world: World) -> None:
    await world.store.save_commitment(await _commitment(world))

    with pytest.raises(AwardConflict):
        await world.store.save_commitment(await _commitment(world))


async def test_a_dead_commitment_frees_the_slot(world: World) -> None:
    """A renegotiation inserts its replacement; the old row steps aside, never edited away."""
    superseded = await _commitment(world, CommitmentState.SUPERSEDED)
    await world.store.save_commitment(superseded)

    live_id = await world.store.save_commitment(await _commitment(world))

    live = await world.store.live_commitment(str(world.order.id))
    assert live is not None
    assert live.id == live_id
    assert live.state is CommitmentState.VERBAL


# ----------------------------------------------------------------------- round trips


async def test_an_order_survives_the_round_trip_with_its_mandate(world: World) -> None:
    """Money is two columns and one value type. The seam is a good place to lose a currency."""
    stored = await world.store.order(str(world.order.id))

    assert stored is not None
    assert stored.reference == world.order.reference
    assert stored.cap == Money(cents=900_000, currency="USD")
    assert stored.target == Money(cents=820_000, currency="USD")
    assert stored.has_mandate is True
    assert stored.mandate().max_all_in_usd == 9000


async def test_a_carrier_is_found_by_the_number_that_called(world: World) -> None:
    found = await world.store.carrier_by_phone(world.carrier.phone)

    assert found is not None
    assert found.id == world.carrier.id


async def test_an_unknown_number_is_not_an_error(world: World) -> None:
    """None is an answer: the agent gives nothing away and records claims as unverified."""
    assert await world.store.carrier_by_phone("+15550000000") is None


async def test_a_call_is_found_by_its_vapi_id(world: World) -> None:
    call = await world.store.call(world.call_id)
    assert call is not None

    again = await world.store.call_by_vapi_id(call.vapi_call_id)
    assert again is not None
    assert again.id == world.call_id


async def test_a_redelivered_status_update_does_not_erase_the_transcript(
    world: World, request: pytest.FixtureRequest
) -> None:
    """ports.py: a status-update can land after the end-of-call-report. It must not clobber.

    ``InMemoryStore.upsert_call`` replaces the stored record wholesale, so the empty
    transcript a status-update carries erases the real one written moments earlier by the
    end-of-call-report. That contradicts the reason ports.py gives for the method being an
    upsert at all. ``fakes.py`` belongs to Phase 0, so this is reported in CHANGELOG rather
    than fixed here, and pinned strict for the same reason Phase 0 pinned STATUS_CHECK: the
    day the fake is fixed this test XPASSes and fails, forcing whoever fixed it to delete the
    marker instead of leaving a stale exemption behind.
    """
    if isinstance(world.store, InMemoryStore):
        request.applymarker(
            pytest.mark.xfail(
                strict=True,
                reason="InMemoryStore.upsert_call replaces instead of merging; see CHANGELOG",
            )
        )
    call = await world.store.call(world.call_id)
    assert call is not None

    await world.store.upsert_call(
        call.model_copy(
            update={
                "status": CallStatus.ENDED,
                "recording_url": "https://recording.test/1.wav",
                "transcript": [{"speaker": "caller", "text": "Sí, tengo un minuto."}],
            }
        )
    )
    await world.store.upsert_call(
        call.model_copy(update={"status": CallStatus.ACTIVE, "transcript": []})
    )

    after = await world.store.call(world.call_id)
    assert after is not None
    assert after.recording_url == "https://recording.test/1.wav"
    assert [t.text for t in after.transcript] == ["Sí, tengo un minuto."]


async def test_an_approval_closes_with_who_decided_it(world: World) -> None:
    approval_id = await world.store.raise_approval(
        Approval(
            order_id=str(world.order.id),
            kind=ApprovalKind.AWARD_APPROVAL,
            reason=ApprovalReason.AWARD_SELECTED,
            context={"comparison": []},
        )
    )
    assert [a.id for a in await world.store.open_approvals(str(world.order.id))] == [approval_id]

    await world.store.resolve_approval(
        approval_id, status="approved", decided_by="ops@volta.test", note="cheapest all-in"
    )

    resolved = await world.store.approval(approval_id)
    assert resolved is not None
    assert resolved.status is ApprovalStatus.APPROVED
    assert resolved.decided_by == "ops@volta.test"
    assert await world.store.open_approvals(str(world.order.id)) == []


async def test_rfq_selection_skips_carriers_not_on_file(world: World) -> None:
    """False means the agent declines to quote. Volta onboards nobody by phone."""
    eligible = await world.store.carriers_for_rfq(50)
    assert world.carrier.id in {c.id for c in eligible}
    assert all(c.is_on_file and c.is_active for c in eligible)


async def test_the_sweep_leaves_alone_what_is_already_moving(world: World) -> None:
    overdue = NOW - timedelta(hours=1)
    await world.store.save_order(
        world.order.model_copy(update={"delivery_deadline": overdue, "status": OrderStatus.BOOKED})
    )

    due = await world.store.due_for_chase(NOW)
    assert str(world.order.id) in {o.id for o in due}

    await world.store.set_order_status(str(world.order.id), OrderStatus.IN_TRANSIT)

    due_again = await world.store.due_for_chase(NOW)
    assert str(world.order.id) not in {o.id for o in due_again}


async def test_writing_to_a_row_that_is_not_there_is_refused(world: World) -> None:
    """PostgREST answers a zero-row update with success, so this has to be checked."""
    missing = str(uuid.uuid4())

    with pytest.raises((KeyError, RowNotFound)):
        await world.store.set_order_status(missing, OrderStatus.BOOKED)


# ------------------------------------------------------------- configuration, not data


async def test_an_unconfigured_store_reports_it_instead_of_failing_to_boot() -> None:
    """main.py builds this at import time. /health is the endpoint you need most when the
    database is the thing that is broken, so construction must not reach for a client."""
    from app.store import StoreUnavailable

    store = SupabaseStore(Settings(supabase_url="", supabase_secret_key=""))

    with pytest.raises(StoreUnavailable):
        await store.order("whatever")
