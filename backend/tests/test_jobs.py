"""The clock: the deadline sweep and the RFQ timeout.

The property both share is idempotency. A restart mid-sweep, or a tick that overlaps the
previous one, must not dial a carrier twice -- and the key that guarantees it is derived
from the fact that triggered the call, never from the moment the loop happened to run.

Everything runs against InMemoryStore and FakeCallPlacer. Nothing here dials.

OWNER: Track E.
"""

from datetime import UTC, datetime, timedelta

from app import jobs
from app.config import Settings
from app.domain import (
    ApprovalKind,
    CallDirection,
    CallPhase,
    CallPlacer,
    CallRecord,
    CallStatus,
    Carrier,
    DialPlan,
    Money,
    Order,
    OrderStatus,
    QuoteRow,
)
from app.tools.calls import CallLedger
from tests.fakes import FakeCallPlacer, InMemoryStore

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
EQUIPMENT = "40-foot container chassis"
SETTINGS = Settings(rfq_timeout_minutes=15)


def now() -> datetime:
    return NOW


class SpyDialler:
    """Stands in for vapi/campaign.run_campaign. Records plans; never touches the network."""

    def __init__(self) -> None:
        self.batches: list[list[DialPlan]] = []

    async def __call__(
        self, plans: list[DialPlan], placer: CallPlacer, settings: Settings
    ) -> dict[str, str]:
        self.batches.append(plans)
        return {plan.call_id: f"vapi-{n}" for n, plan in enumerate(plans)}

    @property
    def dialled(self) -> list[DialPlan]:
        return [plan for batch in self.batches for plan in batch]


def overdue_world() -> tuple[InMemoryStore, SpyDialler]:
    store = InMemoryStore()
    store.add_carrier(Carrier(id="carrier-1", name="Carrier One", phone="+52330000001"))
    store.add_order(
        Order(
            id="order-1",
            reference="OP-1042",
            status=OrderStatus.BOOKED,
            equipment=EQUIPMENT,
            assigned_carrier_id="carrier-1",
            delivery_deadline=NOW - timedelta(hours=2),
        )
    )
    return store, SpyDialler()


# ------------------------------------------------------------------------- the deadline sweep


async def test_the_sweep_dials_an_overdue_order() -> None:
    store, dial = overdue_world()

    placed = await jobs.sweep_deadlines(store, FakeCallPlacer(), SETTINGS, now=now, dial=dial)

    assert len(placed) == 1
    assert len(dial.dialled) == 1
    assert dial.dialled[0].to_number == "+52330000001"
    call = store.calls[placed[0]]
    assert call.phase == CallPhase.STATUS_CHECK.value
    assert call.direction is CallDirection.OUTBOUND


async def test_a_second_sweep_dials_nothing() -> None:
    """The heart of it. A restart mid-sweep must not call a carrier twice."""
    store, dial = overdue_world()
    placer = FakeCallPlacer()

    first = await jobs.sweep_deadlines(store, placer, SETTINGS, now=now, dial=dial)
    second = await jobs.sweep_deadlines(store, placer, SETTINGS, now=now, dial=dial)

    assert len(first) == 1
    assert second == []
    assert len(dial.dialled) == 1


async def test_the_idempotency_key_is_the_deadline_not_the_tick() -> None:
    """A key carrying the current time would be unique every pass and dial once a minute."""
    store, dial = overdue_world()
    await jobs.sweep_deadlines(store, FakeCallPlacer(), SETTINGS, now=now, dial=dial)

    keys = [e.idempotency_key for e in store.events.values() if e.type == "chase.started"]

    assert keys == ["chase:order-1:2026-08-30T10:00:00+00:00"]


async def test_a_load_already_moving_is_left_alone() -> None:
    """OUTBOUND 2 fires on a passed deadline, never on cargo that is already in transit."""
    store, dial = overdue_world()
    await store.set_order_status("order-1", OrderStatus.IN_TRANSIT)

    placed = await jobs.sweep_deadlines(store, FakeCallPlacer(), SETTINGS, now=now, dial=dial)

    assert placed == []
    assert dial.dialled == []


async def test_an_order_with_no_assigned_carrier_is_skipped() -> None:
    """There is nobody to ask. It is a question for a human, not a dial with no number."""
    store, dial = overdue_world()
    order = await store.order("order-1")
    assert order is not None
    store.orders["order-1"] = order.model_copy(update={"assigned_carrier_id": None})

    assert await jobs.sweep_deadlines(store, FakeCallPlacer(), SETTINGS, now=now, dial=dial) == []


async def test_the_sweep_never_constructs_a_real_placer() -> None:
    """A test that dials costs money and can ring a real phone."""
    store, dial = overdue_world()
    placer = FakeCallPlacer()

    await jobs.sweep_deadlines(store, placer, SETTINGS, now=now, dial=dial)

    assert placer.dialled == [], "the dialler is injected; the placer is never called directly"


# ---------------------------------------------------------------------------- the RFQ timeout


def quoting_world(*, call_status: CallStatus, started_at: datetime) -> InMemoryStore:
    store = InMemoryStore()
    store.add_carrier(Carrier(id="carrier-1", name="Carrier One", phone="+52330000001"))
    store.add_order(
        Order(
            id="order-1",
            reference="OP-1042",
            status=OrderStatus.QUOTING,
            equipment=EQUIPMENT,
            cap=Money(cents=900_000, currency="USD"),
            pickup_not_before=datetime(2026, 9, 2, tzinfo=UTC),
            pickup_not_after=datetime(2026, 9, 4, 23, 59, tzinfo=UTC),
            mandate_version=1,
            mandate_set_by="ops@pacifictextiles.mx",
        )
    )
    return _with_call(store, call_status, started_at)


def _with_call(store: InMemoryStore, status: CallStatus, started_at: datetime) -> InMemoryStore:
    store.calls["call-1"] = CallRecord(
        id="call-1",
        vapi_call_id="vapi-1",
        direction=CallDirection.OUTBOUND,
        phase=CallPhase.RFQ.value,
        status=status,
        order_id="order-1",
        carrier_id="carrier-1",
        started_at=started_at,
    )
    store.quotes["quote-1"] = QuoteRow(
        id="quote-1",
        order_id="order-1",
        carrier_id="carrier-1",
        call_id="call-1",
        anchor_ms=11_200,
        amount=Money(cents=850_000, currency="USD"),
        components=[{"name": "all-in", "amount": "8500", "currency": "USD"}],
        cost_is_final=True,
        pickup_at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
        equipment=EQUIPMENT,
        valid_until=NOW + timedelta(hours=6),
    )
    return store


async def test_a_market_whose_calls_all_ended_is_ranked() -> None:
    store = quoting_world(call_status=CallStatus.ENDED, started_at=NOW - timedelta(minutes=5))

    ranked = await jobs.timeout_open_markets(store, SETTINGS, now=now)

    assert ranked == ["order-1"]
    order = await store.order("order-1")
    assert order is not None and order.status is OrderStatus.AWAITING_APPROVAL
    assert any(a.kind is ApprovalKind.AWARD_APPROVAL for a in store.approvals.values())


async def test_a_call_that_never_ended_still_closes_the_market_after_the_timeout() -> None:
    """The clause that matters: a webhook that never arrived must not hold a human forever."""
    store = quoting_world(call_status=CallStatus.ACTIVE, started_at=NOW - timedelta(minutes=45))

    assert await jobs.timeout_open_markets(store, SETTINGS, now=now) == ["order-1"]


async def test_a_market_still_inside_its_timeout_is_left_open() -> None:
    store = quoting_world(call_status=CallStatus.ACTIVE, started_at=NOW - timedelta(minutes=2))

    assert await jobs.timeout_open_markets(store, SETTINGS, now=now) == []
    order = await store.order("order-1")
    assert order is not None and order.status is OrderStatus.QUOTING


async def test_a_market_is_ranked_only_once() -> None:
    store = quoting_world(call_status=CallStatus.ENDED, started_at=NOW - timedelta(minutes=5))

    first = await jobs.timeout_open_markets(store, SETTINGS, now=now)
    second = await jobs.timeout_open_markets(store, SETTINGS, now=now)

    assert first == ["order-1"]
    assert second == [], "a second tick must not request a second approval"
    assert len([a for a in store.approvals.values() if a.kind is ApprovalKind.AWARD_APPROVAL]) == 1


async def test_an_order_not_quoting_is_never_swept() -> None:
    store = quoting_world(call_status=CallStatus.ENDED, started_at=NOW - timedelta(minutes=5))
    await store.set_order_status("order-1", OrderStatus.BOOKED)

    assert await jobs.timeout_open_markets(store, SETTINGS, now=now) == []


# ---------------------------------------------------------------------------- the call ledger


async def test_a_late_status_update_cannot_walk_a_finished_call_backwards() -> None:
    """Vapi does not guarantee ordering. Last-write-wins would erase the recording."""
    store = InMemoryStore()
    ledger = CallLedger(store, now=now)
    base = CallRecord(
        vapi_call_id="vapi-1",
        direction=CallDirection.OUTBOUND,
        phase=CallPhase.RFQ.value,
        started_at=NOW - timedelta(minutes=3),
    )

    await ledger.finalize(
        base.model_copy(
            update={
                "status": CallStatus.ENDED,
                "recording_url": "https://storage.vapi.ai/one.wav",
                "ended_reason": "customer-ended-call",
            }
        ),
        "vapi-1:end-of-call-report",
    )
    await ledger.upsert_from_webhook(
        base.model_copy(update={"status": CallStatus.RINGING}), "vapi-1:status-update-late"
    )

    call = await store.call_by_vapi_id("vapi-1")
    assert call is not None
    assert call.status is CallStatus.ENDED
    assert call.recording_url == "https://storage.vapi.ai/one.wav"


async def test_the_anchor_is_measured_from_our_own_clock() -> None:
    store = InMemoryStore()
    ledger = CallLedger(store, now=now)
    call_id = await store.upsert_call(
        CallRecord(
            vapi_call_id="vapi-1",
            direction=CallDirection.OUTBOUND,
            phase=CallPhase.RFQ.value,
            started_at=NOW - timedelta(seconds=11, milliseconds=200),
        )
    )

    assert await ledger.anchor_ms(call_id) == 11_200


async def test_an_unmeasurable_anchor_is_recorded_as_an_event() -> None:
    """It returns 0 rather than failing, and says so. Commitments refuse on the same call."""
    store = InMemoryStore()
    ledger = CallLedger(store, now=now)
    call_id = await store.upsert_call(
        CallRecord(
            vapi_call_id="vapi-1",
            direction=CallDirection.OUTBOUND,
            phase=CallPhase.RFQ.value,
            started_at=None,
        )
    )

    assert await ledger.anchor_ms(call_id) == 0
    assert any(e.type == "call.anchor_unmeasurable" for e in store.events.values())
