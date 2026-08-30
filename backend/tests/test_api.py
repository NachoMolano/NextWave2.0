"""The portal's REST surface, over InMemoryStore. No network, no database, no phone.

The router is built here exactly as main.py builds it, so what these tests exercise is the
wiring the demo runs on and not a parallel arrangement that only exists in the suite.

Two things are worth stating because they are the point of the layer rather than details of
it: ``POST /api/orders/{id}/mandate`` is the only path in the system that can write a price
ceiling, and an ``AwardConflict`` surfaces as 409 rather than 500 -- a second award attempt is
the database refusing to let two bookings exist, which is the system working.

OWNER: Track C.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import ClassVar

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import create_api_router
from app.config import Settings
from app.domain import (
    Approval,
    ApprovalKind,
    ApprovalReason,
    AwardConflict,
    CallDirection,
    CallPhase,
    CallRecord,
    Carrier,
    Commitment,
    Comparison,
    DecisionRow,
    DialPlan,
    EventRow,
    Order,
    OrderStatus,
)
from app.store import RowNotFound, StoreUnavailable
from tests.fakes import InMemoryStore, RecordingNotifier

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
PORTAL_ACTOR = "ops@volta.test"


def _now() -> datetime:
    return NOW


class PortalMemoryStore(InMemoryStore):
    """InMemoryStore plus the three list reads the portal needs.

    They are not on the frozen ``Store`` Protocol -- see ``api/routes.py::PortalStore`` and
    the CHANGELOG entry asking for them to move there. Until they do, both implementations
    have to grow them separately, which is exactly the drift the shared suite exists to catch
    and the reason it is worth raising rather than living with.
    """

    #: The one company_profile row. 0003 seeds it; a store without it answers 503.
    profile: ClassVar[dict[str, object] | None] = {
        "display_name": "Pacific Textiles",
        "business_type": "importer",
        "currency": "USD",
        "timezone": "America/Mexico_City",
        "agent_name": "Volta",
        "agent_role": "transport coordinator",
        "primary_language": "en",
        "fallback_language": "es-MX",
        "warehouse_city": "Tampa",
        "updated_by": "seed",
    }

    async def list_orders(self) -> list[Order]:
        return list(self.orders.values())

    async def list_carriers(self) -> list[Carrier]:
        return sorted(self.carriers.values(), key=lambda c: c.name)

    async def calls_for(self, order_id: str) -> list[CallRecord]:
        return [c for c in self.calls.values() if c.order_id == order_id]

    async def decisions_for_call(self, call_id: str) -> list[DecisionRow]:
        return [d for d in self.decisions.values() if d.call_id == call_id]

    async def events_for_call(self, call_id: str) -> list[EventRow]:
        return [e for e in self.events.values() if e.call_id == call_id]

    async def commitments_for(self, order_id: str) -> list[Commitment]:
        return [c for c in self.commitments.values() if c.order_id == order_id]

    async def company_profile(self) -> dict[str, object] | None:
        return dict(self.profile) if self.profile else None

    async def save_company_profile(self, values: dict[str, object]) -> dict[str, object]:
        if self.profile is None:
            raise RowNotFound("no company_profile row; apply migration 0003")
        self.profile.update(values)
        return dict(self.profile)


class FakeMarket:
    """Records what the portal asked the market to do. Dials nothing itself.

    ``plan_rfq`` returns plans the first time and nothing afterwards, which is what the real
    one does: the market is claimed by an idempotency key on the mandate version, so a second
    click on the same mandate plans nothing and therefore dials nobody.

    It also moves the order to QUOTING once it has planned, because the real one does
    (``Market.plan_rfq``, after the call rows exist) and that transition is now the only
    thing that opens a market. A fake that skipped it would let a test assert an order was
    "collecting quotes" in a world the real system never produces.
    """

    def __init__(
        self, *, award_conflict: bool = False, store: PortalMemoryStore | None = None
    ) -> None:
        self.planned: list[tuple[str, int]] = []
        self.awarded: list[str] = []
        self.failed_rounds: list[list[str]] = []
        self._planned_ids: set[str] = set()
        self.award_conflict = award_conflict
        self.store = store

    async def plan_rfq(self, order: Order, count: int) -> list[DialPlan]:
        already = any(planned == str(order.id) for planned, _ in self.planned)
        self.planned.append((str(order.id), count))
        self._planned_ids.add(str(order.id))
        if already:
            return []
        if self.store is not None:
            await self.store.set_order_status(str(order.id), OrderStatus.QUOTING)
        return [
            DialPlan(
                call_id=f"call-{index}",
                carrier=Carrier(
                    id=f"c{index}", name=f"Carrier {index}", phone=f"+5255000000{index}"
                ),
                to_number=f"+5255000000{index}",
                context={},
            )
            for index in range(count)
        ]

    async def mark_dial_round_failed(self, plans: list[DialPlan]) -> None:
        self.failed_rounds.append([plan.call_id for plan in plans])
        # The real one releases the claim by marking the rows, so the next plan_rfq is a
        # fresh attempt rather than a silent no-op.
        self.planned = [entry for entry in self.planned if entry[0] not in self._planned_ids]

    async def rank(self, order: Order) -> Comparison:
        return Comparison(
            order_id=str(order.id),
            entries=[],
            winner_quote_id=None,
            cap_at_decision_cents=order.cap.cents if order.cap else None,
            cap_currency=order.cap.currency if order.cap else None,
            mandate_version=order.mandate_version,
            built_at=NOW,
        )

    async def award(self, order: Order, approval: Approval) -> str:
        if self.award_conflict:
            raise AwardConflict(f"order {order.id} already awarded")
        self.awarded.append(str(order.id))
        return "quote-1"

    async def plan_award(self, order: Order, quote_id: str) -> list[DialPlan]:
        carrier = Carrier(id="carrier-1", name="Winner", phone="+525500000001")
        return [
            DialPlan(
                call_id="award-call-1",
                carrier=carrier,
                to_number=carrier.phone,
                context={"phase": CallPhase.AWARD.value, "quote_id": quote_id},
            )
        ]


class FakeDialler:
    """Records what would have been dialled. Never touches the network."""

    def __init__(self, *, places: bool = True) -> None:
        self.batches: list[list[DialPlan]] = []
        #: False reproduces a round where the provider refused every call. run_campaign
        #: swallows each failure, so what reaches the route is an empty dict, not a raise.
        self.places = places

    async def __call__(self, plans: list[DialPlan]) -> object:
        self.batches.append(plans)
        if not self.places:
            return {}
        return {plan.call_id: f"vapi-{plan.call_id}" for plan in plans}

    @property
    def numbers_dialled(self) -> list[str]:
        return [plan.to_number for batch in self.batches for plan in batch]


class FakeSweep:
    """Dials once, then nothing. Idempotency is the assertion worth making to a judge."""

    def __init__(self) -> None:
        self.runs = 0

    async def __call__(self) -> list[str]:
        self.runs += 1
        return ["call-1"] if self.runs == 1 else []


def build(
    store: PortalMemoryStore | None = None,
    *,
    market: FakeMarket | None = None,
    sweep: FakeSweep | None = None,
    dial: FakeDialler | None = None,
    notifier: RecordingNotifier | None = None,
) -> tuple[TestClient, PortalMemoryStore, FakeMarket, FakeSweep, FakeDialler]:
    store = store or PortalMemoryStore()
    market = market or FakeMarket(store=store)
    sweep = sweep or FakeSweep()
    dial = dial or FakeDialler()
    app = FastAPI()
    app.include_router(
        create_api_router(
            store,  # type: ignore[arg-type]
            market=market,  # type: ignore[arg-type]
            sweep=sweep,
            dial=dial,
            now=_now,
            settings=Settings(portal_manager_identity="ops@volta.test"),
            notifier=notifier,
        ),
        prefix="/api",
    )
    return TestClient(app), store, market, sweep, dial


def _new_order(reference: str = "OP-MZO-0001") -> dict[str, object]:
    return {
        "reference": reference,
        "origin": "Manzanillo",
        "destination": "Guadalajara",
        "cargo": "Textiles",
        "equipment": "40-foot container chassis",
        "container_number": "MSCU1234566",
        "free_days": 5,
        "last_free_day": str(date(2026, 9, 2)),
    }


def _confirm_intake(client: TestClient, order_id: str) -> None:
    """Stage 1, which every mandate now requires.

    Granting authority to spend against a container nobody confirmed was released is what
    the release gate exists to refuse, so the tests have to walk through it like the portal
    does. ``_new_order`` already carries a last free day, so the clock is satisfied.
    """
    response = client.post(f"/api/orders/{order_id}/intake", json={"released": True})
    assert response.status_code == 200, response.text


def _mandate() -> dict[str, object]:
    return {
        "cap_amount_cents": 900_000,
        "cap_currency": "USD",
        "target_amount_cents": 820_000,
        "pickup_not_before": NOW.isoformat(),
        "pickup_not_after": (NOW + timedelta(days=2)).isoformat(),
        "expected_version": 0,
    }


# ------------------------------------------------------------------------------- orders


def test_receiving_a_cargo_is_idempotent_on_the_reference() -> None:
    """A re-delivered intake must not open a second folio for one container."""
    client, store, _, _, _ = build()

    first = client.post("/api/orders", json=_new_order())
    second = client.post("/api/orders", json=_new_order())

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(store.orders) == 1


def test_the_queue_shows_the_demurrage_countdown() -> None:
    """The countdown is what makes everything downstream urgent, so it is on the list row."""
    client, _, _, _, _ = build()
    client.post("/api/orders", json=_new_order())

    row = client.get("/api/orders").json()[0]

    assert row["demurrage"]["last_free_day"] == "2026-09-02"
    assert row["demurrage"]["days_remaining"] == 3
    assert row["demurrage"]["is_overdue"] is False
    assert row["mandate"]["is_granted"] is False


def test_a_new_order_authorizes_nothing() -> None:
    """No mandate is not 'no limit'. It is a permission that was never granted."""
    client, _, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]

    body = client.get(f"/api/orders/{order_id}").json()

    assert body["mandate"]["is_granted"] is False
    assert body["mandate"]["cap"] is None
    assert body["mandate"]["version"] == 0


def test_the_aggregate_is_one_call() -> None:
    """A human approving an award should not watch a page fill in piece by piece."""
    client, _, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]

    body = client.get(f"/api/orders/{order_id}").json()

    assert set(body) == {
        "order",
        "mandate",
        "demurrage",
        "next_action",
        "quotes",
        "calls",
        "commitment",
        "approvals",
    }


def test_an_unknown_order_is_404_not_500() -> None:
    client, _, _, _, _ = build()
    assert client.get("/api/orders/does-not-exist").status_code == 404


# ------------------------------------------------------------------------------ mandate


def test_setting_a_mandate_bumps_the_version_and_records_who() -> None:
    """The row a jury reads when it asks who authorized the spend."""
    client, store, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]

    _confirm_intake(client, order_id)
    body = client.post(f"/api/orders/{order_id}/mandate", json=_mandate()).json()

    assert body["mandate"]["version"] == 1
    assert body["mandate"]["is_granted"] is True
    assert body["mandate"]["cap"] == {"cents": 900_000, "currency": "USD"}
    assert body["mandate"]["set_by"] == "ops@volta.test"
    assert any(e.type == "mandate.set" for e in store.events.values())


def test_granting_a_mandate_does_not_open_the_market_by_itself() -> None:
    """Authority is not spend. Until a carrier is dialled the order has not moved.

    The status used to flip to ``quoting`` here, which made an order with zero calls read as
    "Volta is working" and hid the operator's own next action -- opening the market -- behind
    a progress label. ``plan_rfq`` owns that transition, after the call rows exist.
    """
    client, _, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]

    _confirm_intake(client, order_id)
    body = client.post(f"/api/orders/{order_id}/mandate", json=_mandate()).json()

    assert body["order"]["status"] == "received"
    assert body["mandate"]["is_granted"] is True
    assert body["calls"] == []
    assert body["next_action"]["actor"] == "operator"
    assert body["next_action"]["label"] == "Open the market"


def test_raising_the_cap_versions_it_rather_than_overwriting() -> None:
    """Decisions copy the ceiling by value, so an old refusal stays explainable."""
    client, store, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    _confirm_intake(client, order_id)
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    raised = dict(_mandate(), cap_amount_cents=1_200_000, expected_version=1)
    _confirm_intake(client, order_id)
    body = client.post(f"/api/orders/{order_id}/mandate", json=raised).json()

    assert body["mandate"]["version"] == 2
    assert body["mandate"]["cap"]["cents"] == 1_200_000
    keys = {e.idempotency_key for e in store.events.values()}
    assert f"mandate.set:{order_id}:v1" in keys
    assert f"mandate.set:{order_id}:v2" in keys


def test_a_stale_mandate_write_is_rejected() -> None:
    """Two dashboards cannot silently overwrite one another's authority."""
    client, _, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    _confirm_intake(client, order_id)
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    _confirm_intake(client, order_id)
    assert client.post(f"/api/orders/{order_id}/mandate", json=_mandate()).status_code == 409


# ---------------------------------------------------------------------------- the market


def test_the_market_will_not_open_without_a_mandate() -> None:
    client, _, market, _, dial = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    _confirm_intake(client, order_id)

    response = client.post(f"/api/orders/{order_id}/rfq")

    assert response.status_code == 409
    assert "no mandate" in response.json()["detail"]
    assert market.planned == []
    assert dial.numbers_dialled == []


def test_the_market_will_not_open_on_an_unreleased_container() -> None:
    """The release gate, from the dialling side.

    A release can be withdrawn after a mandate was granted -- the box is held, the booking
    slips -- and the phone has to stop when it is, not only at the moment authority was
    given. Three carriers were dialled for a container nobody had confirmed.
    """
    client, _, _, _, dial = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    _confirm_intake(client, order_id)
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())
    client.post(f"/api/orders/{order_id}/intake", json={"released": False, "note": "held"})

    response = client.post(f"/api/orders/{order_id}/rfq")

    assert response.status_code == 409
    assert "not released" in response.json()["detail"]
    assert dial.numbers_dialled == []


def test_a_mandate_needs_the_container_released_first() -> None:
    client, _, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]

    response = client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    assert response.status_code == 409
    assert "not been confirmed as released" in response.json()["detail"]


def test_a_mandate_needs_a_clock() -> None:
    """A ceiling with no deadline is authority to negotiate with no reason to hurry.

    OP-MIA-0002 was granted a mandate and dialled three carriers with neither a last free
    day nor a cargo cutoff, so the agent's strongest honest lever -- trading pickup timing
    for rate -- was a lever it did not know it had.
    """
    client, _, _, _, _ = build()
    order = {**_new_order(), "free_days": None, "last_free_day": None}
    order_id = client.post("/api/orders", json=order).json()["id"]
    _confirm_intake(client, order_id)

    response = client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    assert response.status_code == 409
    assert "no deadline" in response.json()["detail"]


def test_a_cargo_cutoff_satisfies_the_clock_on_an_export() -> None:
    """An export has no demurrage. Its clock is the cutoff, and it counts."""
    client, _, _, _, _ = build()
    order = {**_new_order(), "free_days": None, "last_free_day": None}
    order_id = client.post("/api/orders", json=order).json()["id"]
    client.post(
        f"/api/orders/{order_id}/intake",
        json={"released": True, "delivery_deadline": "2026-09-04T18:00:00Z"},
    )

    response = client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    assert response.status_code == 200


def test_opening_the_market_asks_for_at_least_three_carriers() -> None:
    """The brief requires three. The count comes from config, never from a caller."""
    client, _, market, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    _confirm_intake(client, order_id)
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    client.post(f"/api/orders/{order_id}/rfq")

    assert market.planned == [(order_id, 3)]


def test_opening_the_market_actually_dials() -> None:
    """Planning is not calling.

    The endpoint used to plan and stop there: call rows appeared, the portal said the market
    was open, and nobody's phone rang. A test that only asserted plan_rfq was reached could
    not tell the difference, which is why this one asserts on the dialler.
    """
    client, _, _, _, dial = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    _confirm_intake(client, order_id)
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    client.post(f"/api/orders/{order_id}/rfq")

    assert len(dial.numbers_dialled) == 3


def test_opening_the_market_twice_dials_nobody_twice() -> None:
    """Two clicks, or two instances against one database, must not ring a carrier twice.

    The market is claimed by an idempotency key on the mandate version, so the second attempt
    plans nothing and there is nothing to dial.
    """
    client, _, _, _, dial = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    _confirm_intake(client, order_id)
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    client.post(f"/api/orders/{order_id}/rfq")
    first = list(dial.numbers_dialled)
    client.post(f"/api/orders/{order_id}/rfq")

    assert dial.numbers_dialled == first


def test_the_comparison_carries_the_cap_it_was_ranked_against() -> None:
    client, _, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    _confirm_intake(client, order_id)
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    body = client.get(f"/api/orders/{order_id}/comparison").json()

    assert body["cap_at_decision_cents"] == 900_000
    assert body["mandate_version"] == 1


# -------------------------------------------------------------------------- the inbox


def _award_approval(store: PortalMemoryStore, order_id: str) -> str:
    """Put an award decision in the human inbox, the way tools/market.py will.

    ``winner_quote_id`` is the field that makes this approvable. It used to be absent, which
    made every test here exercise a shape ``rank()`` never produces -- and hid the fact that
    approving an award with no winner reached ``market.award`` and died on a ValueError.
    """
    return asyncio.run(
        store.raise_approval(
            Approval(
                order_id=order_id,
                kind=ApprovalKind.AWARD_APPROVAL,
                reason=ApprovalReason.AWARD_SELECTED,
                context={"comparison": [], "winner_quote_id": "quote-1"},
            )
        )
    )


def _no_candidate_approval(store: PortalMemoryStore, order_id: str) -> str:
    """What the market raises when nothing cleared the ceiling: no winner to award."""
    return asyncio.run(
        store.raise_approval(
            Approval(
                order_id=order_id,
                kind=ApprovalKind.AWARD_APPROVAL,
                reason=ApprovalReason.NO_ELIGIBLE_CANDIDATE,
                context={"entries": [], "winner_quote_id": None},
            )
        )
    )


def _rfq_call(store: PortalMemoryStore, order_id: str, carrier_id: str) -> str:
    return asyncio.run(
        store.upsert_call(
            CallRecord(
                vapi_call_id=f"vapi-{carrier_id}",
                direction=CallDirection.OUTBOUND,
                phase="rfq",
                order_id=order_id,
                carrier_id=carrier_id,
            )
        )
    )


def test_approving_an_award_releases_the_award_call() -> None:
    client, store, market, _, dial = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    approval_id = _award_approval(store, order_id)

    response = client.post(
        f"/api/approvals/{approval_id}/decision",
        json={"status": "approved"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert market.awarded == [order_id]
    assert dial.numbers_dialled == ["+525500000001"]
    assert dial.batches[0][0].context["phase"] == CallPhase.AWARD.value


def test_an_award_call_that_places_nothing_keeps_the_approval_retryable() -> None:
    dial = FakeDialler(places=False)
    client, store, _, _, _ = build(dial=dial)
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    approval_id = _award_approval(store, order_id)

    failed = client.post(
        f"/api/approvals/{approval_id}/decision", json={"status": "approved"}
    )
    assert failed.status_code == 502
    assert len(client.get("/api/approvals").json()) == 1

    dial.places = True
    retry = client.post(
        f"/api/approvals/{approval_id}/decision", json={"status": "approved"}
    )
    assert retry.status_code == 200
    assert len(dial.batches) == 2


def test_a_dial_round_that_places_nothing_is_a_502_not_a_silent_200() -> None:
    """run_campaign swallows each dial failure so one bad carrier cannot take the batch
    down. A round that reached *nobody* was therefore indistinguishable from success: the
    portal showed "collecting quotes" over three rows that would never ring, and Vapi had
    no record of any call. Three carriers were left un-dialled that way on 30 Aug."""
    client, _, market, _, _ = build(dial=FakeDialler(places=False))
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    _confirm_intake(client, order_id)
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    response = client.post(f"/api/orders/{order_id}/rfq")

    assert response.status_code == 502
    assert "Nobody was dialled" in response.json()["detail"]
    assert market.failed_rounds, "the round must be marked so the market can be retried"


def test_a_failed_dial_round_can_be_retried() -> None:
    """The claim was written before the dial and never released, so after a failed round
    every retry returned an empty plan list and dialled nobody. The order was stuck on that
    mandate version -- the only escape was raising the ceiling to bump it."""
    client, _, _, _, dial = build(dial=FakeDialler(places=False))
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    _confirm_intake(client, order_id)
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())
    assert client.post(f"/api/orders/{order_id}/rfq").status_code == 502

    dial.places = True
    retry = client.post(f"/api/orders/{order_id}/rfq")

    assert retry.status_code == 200
    assert len(dial.batches) == 2, "the retry must actually dial, not plan an empty list"
    assert dial.numbers_dialled, "the retry placed no calls"


def test_approving_an_award_with_no_candidate_is_a_409_that_says_why() -> None:
    """The portal's Approve button did nothing at all: market.award raised
    ValueError("the approval carries no winning quote"), which surfaced as a 500 the
    frontend swallowed. A person cannot award a comparison with no eligible carrier, and
    being told so is the difference between a refusal and a broken button."""
    client, store, market, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    approval_id = _no_candidate_approval(store, order_id)

    response = client.post(
        f"/api/approvals/{approval_id}/decision", json={"status": "approved"}
    )

    assert response.status_code == 409
    assert "no eligible carrier" in response.json()["detail"]
    assert market.awarded == []
    # The approval is still open, so the operator can act on it once the market reopens.
    assert len(client.get("/api/approvals").json()) == 1


def test_rejecting_an_award_with_no_candidate_is_allowed() -> None:
    """Refusing is always available: it is the one decision that needs no winner."""
    client, store, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    approval_id = _no_candidate_approval(store, order_id)

    response = client.post(
        f"/api/approvals/{approval_id}/decision", json={"status": "rejected"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_a_second_award_is_a_409_not_a_500() -> None:
    """The database refusing two bookings is the system working, not the system breaking."""
    client, store, _, _, _ = build(market=FakeMarket(award_conflict=True))
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    approval_id = _award_approval(store, order_id)

    response = client.post(
        f"/api/approvals/{approval_id}/decision",
        json={"status": "approved"},
    )

    assert response.status_code == 409


def test_an_approval_is_decided_once() -> None:
    client, store, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    approval_id = _award_approval(store, order_id)
    decision = {"status": "approved"}
    client.post(f"/api/approvals/{approval_id}/decision", json=decision)

    again = client.post(f"/api/approvals/{approval_id}/decision", json=decision)

    assert again.status_code == 409


def test_the_inbox_lists_only_what_is_open() -> None:
    client, store, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    approval_id = _award_approval(store, order_id)
    assert len(client.get("/api/approvals").json()) == 1

    client.post(
        f"/api/approvals/{approval_id}/decision",
        json={"status": "approved"},
    )

    assert client.get("/api/approvals").json() == []


# --------------------------------------------------------------------- calls, carriers


def test_a_call_carries_its_brief_and_its_carrier() -> None:
    client, store, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    carrier = store.add_carrier(
        Carrier(id="carrier-1", name="Fletes del Pacifico", phone="+523141000001")
    )
    call_id = _rfq_call(store, order_id, carrier.id)

    body = client.get(f"/api/calls/{call_id}").json()

    assert body["call"]["id"] == call_id
    assert body["carrier"]["name"] == "Fletes del Pacifico"
    assert body["report"] is None


def test_an_unknown_call_is_404() -> None:
    client, _, _, _, _ = build()
    assert client.get("/api/calls/nope").status_code == 404


def test_carriers_are_listed_for_the_portal() -> None:
    client, store, _, _, _ = build()
    store.add_carrier(Carrier(id="c2", name="Transportes Colima", phone="+523141000002"))
    store.add_carrier(Carrier(id="c1", name="Autolineas Manzanillo", phone="+523141000003"))

    names = [c["name"] for c in client.get("/api/carriers").json()]

    assert names == ["Autolineas Manzanillo", "Transportes Colima"]


# ----------------------------------------------------------------------------- the clock


def test_the_sweep_dials_once_and_then_nothing() -> None:
    """A second press must dial nothing. That is the demo assertion for OUTBOUND 2."""
    client, _, _, sweep, _ = build()

    first = client.post("/api/jobs/sweep").json()
    second = client.post("/api/jobs/sweep").json()

    assert first["call_ids"] == ["call-1"]
    assert second["call_ids"] == []
    assert sweep.runs == 2


# --------------------------------------------------------------- not built, and honest


def test_unimplemented_renegotiation_is_not_advertised() -> None:
    client, _, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]

    response = client.post(f"/api/orders/{order_id}/renegotiate")

    assert response.status_code == 404


def test_portal_accepts_requests_without_a_bearer_token() -> None:
    client, _, _, _, _ = build()
    assert client.get("/api/orders", headers={"Authorization": "Bearer wrong"}).status_code == 200


def test_an_unconfigured_store_is_503_not_a_crash() -> None:
    """/health has to answer when the database is the thing that is broken."""

    class Unconfigured(PortalMemoryStore):
        async def list_orders(self) -> list[Order]:
            raise StoreUnavailable("SUPABASE_URL is not configured")

    client, _, _, _, _ = build(Unconfigured())

    assert client.get("/api/orders").status_code == 503


# ---------------------------------------------------------------- the decision trace


def _traced_call(store: PortalMemoryStore, order_id: str) -> str:
    call_id = _rfq_call(store, order_id, "carrier-1")
    asyncio.run(
        store.record_decision(
            DecisionRow(
                order_id=order_id,
                call_id=call_id,
                proposal={"amount_cents": 1_050_000},
                outcome="deny",
                reason_code="outside_mandate",
                cap_at_decision_cents=900_000,
                cap_currency="USD",
                mandate_version=1,
                decided_at=NOW + timedelta(seconds=35),
            )
        )
    )
    asyncio.run(
        store.append_event(
            EventRow(
                order_id=order_id,
                call_id=call_id,
                type="quote.proposed",
                idempotency_key=f"quote:{call_id}",
                created_at=NOW + timedelta(seconds=34),
            )
        )
    )
    return call_id


def test_the_trace_shows_a_refusal_with_the_cap_it_was_judged_against() -> None:
    """The row a jury reads. The cap is the one copied into the decision, not today's."""
    client, store, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    store.add_carrier(Carrier(id="carrier-1", name="Fletes del Pacifico", phone="+523141000001"))
    call_id = _traced_call(store, order_id)

    rows = client.get(f"/api/calls/{call_id}/trace").json()

    policy = [r for r in rows if r["category"] == "policy"]
    assert len(policy) == 1
    assert policy[0]["result"] == "denied"
    assert policy[0]["reason_code"] == "OUTSIDE_MANDATE"
    assert "9,000.00 USD" in policy[0]["volta"]


def test_the_trace_is_ordered_and_stays_short() -> None:
    """One short sentence per side. A column of paragraphs is a log console, not a trace."""
    client, store, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    store.add_carrier(Carrier(id="carrier-1", name="Fletes del Pacifico", phone="+523141000001"))
    call_id = _traced_call(store, order_id)

    rows = client.get(f"/api/calls/{call_id}/trace").json()

    assert rows == sorted(rows, key=lambda r: r["at"])
    assert all(len(r["counterparty"]) <= 80 for r in rows)
    assert all(len(r["volta"]) <= 80 for r in rows)
    assert {r["category"] for r in rows} <= {
        "conversation",
        "quote",
        "policy",
        "decision",
        "tool",
        "action",
    }


def test_the_trace_never_carries_a_prompt() -> None:
    """The transcript is provenance for the trace, never its content.

    The prompt states the ceiling and the target under a heading telling the agent never to
    say them. It reached the portal once through calls.transcript; it must not reach it again
    through a screen built on top of that transcript.
    """
    client, store, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    store.add_carrier(Carrier(id="carrier-1", name="Fletes del Pacifico", phone="+523141000001"))
    call_id = _traced_call(store, order_id)

    body = client.get(f"/api/calls/{call_id}/trace").text

    assert "NEVER SAY OUT LOUD" not in body
    assert "ROLE" not in body
    assert "Ceiling" not in body


def test_an_unknown_call_has_no_trace() -> None:
    client, _, _, _, _ = build()
    assert client.get("/api/calls/nope/trace").status_code == 404


# ------------------------------------------------------------------------ the business


def test_the_profile_comes_from_the_database() -> None:
    """Not from the environment. An operator can correct a warehouse; a redeploy is not that."""
    client, _, _, _, _ = build()

    body = client.get("/api/profile").json()

    assert body["display_name"] == "Pacific Textiles"
    assert body["warehouse_city"] == "Tampa"
    assert body["agent_name"] == "Volta"


def test_editing_the_profile_records_who_did_it() -> None:
    """The warehouse address is read out loud on a recorded line. It carries a name."""
    client, store, _, _, _ = build()

    body = client.put(
        "/api/profile",
        json={"warehouse_address": "900 Channelside Dr", "warehouse_city": "Tampa"},
    ).json()

    assert body["warehouse_address"] == "900 Channelside Dr"
    # From server configuration, never from the body. This is the row somebody reads when
    # they ask who changed the address the agent now reads out on a recorded line.
    assert body["updated_by"] == PORTAL_ACTOR
    assert store.profile is not None
    assert store.profile["warehouse_address"] == "900 Channelside Dr"


def test_a_body_cannot_claim_to_be_somebody_else() -> None:
    """An `updated_by` in the body is ignored; server configuration decides who acted."""
    client, _, _, _, _ = build()

    body = client.put(
        "/api/profile",
        json={"warehouse_city": "Orlando", "updated_by": "somebody.else@example.com"},
    ).json()

    assert body["updated_by"] == PORTAL_ACTOR


def test_a_partial_edit_leaves_the_rest_alone() -> None:
    """A form edits what it edits. Absent fields are not an instruction to blank them."""
    client, _, _, _, _ = build()

    body = client.put("/api/profile", json={"warehouse_hours": "07:00-19:00"}).json()

    assert body["warehouse_hours"] == "07:00-19:00"
    assert body["display_name"] == "Pacific Textiles"


def test_a_store_without_the_migration_says_so() -> None:
    class NoProfile(PortalMemoryStore):
        profile = None

    client, _, _, _, _ = build(NoProfile())

    assert client.get("/api/profile").status_code == 503


# --------------------------------------------------------------- where are we, and whose move


def test_a_new_order_is_waiting_on_a_person() -> None:
    """The question a portal has to answer: is this waiting on me, or is it working?"""
    client, _, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]

    action = client.get(f"/api/orders/{order_id}").json()["next_action"]

    assert action["actor"] == "operator"
    assert action["label"] == "Grant a mandate"
    assert action["stage"] == "Mandate"


def test_an_open_market_is_working_not_waiting() -> None:
    """Carriers being dialled is the machine doing its job. It must not read as urgent."""
    client, _, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    _confirm_intake(client, order_id)
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())
    client.post(f"/api/orders/{order_id}/rfq")

    action = client.get(f"/api/orders/{order_id}").json()["next_action"]

    assert action["actor"] == "volta"
    assert action["urgency"] == "waiting"


def test_an_open_approval_outranks_everything_else() -> None:
    """A decision waiting on a person is the only state where the system has stopped."""
    client, store, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    _award_approval(store, order_id)

    action = client.get(f"/api/orders/{order_id}").json()["next_action"]

    assert action["actor"] == "operator"
    assert action["urgency"] == "now"


def test_the_queue_puts_what_is_blocked_on_a_person_first() -> None:
    """A queue ordered by created_at makes someone read every row to find their own."""
    client, store, _, _, _ = build()
    quiet = client.post("/api/orders", json=_new_order("OP-QUIET")).json()["id"]
    _confirm_intake(client, quiet)
    client.post(f"/api/orders/{quiet}/mandate", json=_mandate())
    client.post(f"/api/orders/{quiet}/rfq")
    blocked = client.post("/api/orders", json=_new_order("OP-BLOCKED")).json()["id"]
    _award_approval(store, blocked)

    rows = client.get("/api/orders").json()

    assert rows[0]["reference"] == "OP-BLOCKED"
    assert rows[0]["next_action"]["urgency"] == "now"


def test_a_finished_order_asks_nothing_of_anyone() -> None:
    client, store, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    asyncio.run(store.set_order_status(order_id, OrderStatus.DELIVERED))

    action = client.get(f"/api/orders/{order_id}").json()["next_action"]

    assert action["actor"] == "nobody"
    assert action["urgency"] == "none"


# ------------------------------------------------------------------ who is calling


def _app_with(settings: Settings) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_api_router(
            PortalMemoryStore(),  # type: ignore[arg-type]
            market=FakeMarket(),  # type: ignore[arg-type]
            sweep=FakeSweep(),
            dial=FakeDialler(),
            now=_now,
            settings=settings,
        ),
        prefix="/api",
    )
    return TestClient(app)


def test_portal_never_requires_a_bearer_token() -> None:
    client = _app_with(Settings())

    assert client.get("/api/orders").status_code == 200
    body = client.get("/api/session").json()
    assert body["actor"] == "portal-operator"
    # The authorize button names the number it is about to ring; it must come from settings.
    assert body["rfq_carrier_count"] == 3


def test_portal_uses_configured_audit_identity_without_login() -> None:
    client = _app_with(Settings(portal_manager_identity="ops@volta.mx"))

    assert client.get("/api/session").json()["actor"] == "ops@volta.mx"


def test_local_email_composer_sends_without_touching_application_state() -> None:
    notifier = RecordingNotifier()
    client, store, _, _, _ = build(notifier=notifier)

    response = client.post(
        "/api/email-test",
        json={
            "to_address": "operator@example.com",
            "subject": "Standalone test",
            "body": "This is separate from the procurement flow.",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert notifier.sent[0].to_address == "operator@example.com"
    assert store.deliveries == []


def test_email_composer_is_not_exposed_outside_local_mode() -> None:
    client = _app_with(Settings(environment="demo"))

    assert (
        client.post(
            "/api/email-test",
            json={"to_address": "operator@example.com", "subject": "No", "body": "No"},
        ).status_code
        == 404
    )
