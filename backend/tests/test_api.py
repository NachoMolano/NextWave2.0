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
    CallRecord,
    Carrier,
    Comparison,
    DialPlan,
    Order,
)
from app.store import StoreUnavailable
from tests.fakes import InMemoryStore

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
AUTH = {"Authorization": "Bearer portal-test-token"}


def _now() -> datetime:
    return NOW


class PortalMemoryStore(InMemoryStore):
    """InMemoryStore plus the three list reads the portal needs.

    They are not on the frozen ``Store`` Protocol -- see ``api/routes.py::PortalStore`` and
    the CHANGELOG entry asking for them to move there. Until they do, both implementations
    have to grow them separately, which is exactly the drift the shared suite exists to catch
    and the reason it is worth raising rather than living with.
    """

    async def list_orders(self) -> list[Order]:
        return list(self.orders.values())

    async def list_carriers(self) -> list[Carrier]:
        return sorted(self.carriers.values(), key=lambda c: c.name)

    async def calls_for(self, order_id: str) -> list[CallRecord]:
        return [c for c in self.calls.values() if c.order_id == order_id]


class FakeMarket:
    """Records what the portal asked the market to do. Dials nothing itself.

    ``plan_rfq`` returns plans the first time and nothing afterwards, which is what the real
    one does: the market is claimed by an idempotency key on the mandate version, so a second
    click on the same mandate plans nothing and therefore dials nobody.
    """

    def __init__(self, *, award_conflict: bool = False) -> None:
        self.planned: list[tuple[str, int]] = []
        self.awarded: list[str] = []
        self.award_conflict = award_conflict

    async def plan_rfq(self, order: Order, count: int) -> list[DialPlan]:
        already = any(planned == str(order.id) for planned, _ in self.planned)
        self.planned.append((str(order.id), count))
        if already:
            return []
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


class FakeDialler:
    """Records what would have been dialled. Never touches the network."""

    def __init__(self) -> None:
        self.batches: list[list[DialPlan]] = []

    async def __call__(self, plans: list[DialPlan]) -> object:
        self.batches.append(plans)
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
) -> tuple[TestClient, PortalMemoryStore, FakeMarket, FakeSweep, FakeDialler]:
    store = store or PortalMemoryStore()
    market = market or FakeMarket()
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
            settings=Settings(
                portal_api_token="portal-test-token",
                portal_manager_identity="ops@volta.test",
            ),
        ),
        prefix="/api",
    )
    return TestClient(app, headers=AUTH), store, market, sweep, dial


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

    body = client.post(f"/api/orders/{order_id}/mandate", json=_mandate()).json()

    assert body["mandate"]["version"] == 1
    assert body["mandate"]["is_granted"] is True
    assert body["mandate"]["cap"] == {"cents": 900_000, "currency": "USD"}
    assert body["mandate"]["set_by"] == "ops@volta.test"
    assert body["order"]["status"] == "quoting"
    assert any(e.type == "mandate.set" for e in store.events.values())


def test_raising_the_cap_versions_it_rather_than_overwriting() -> None:
    """Decisions copy the ceiling by value, so an old refusal stays explainable."""
    client, store, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    raised = dict(_mandate(), cap_amount_cents=1_200_000, expected_version=1)
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
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    assert client.post(f"/api/orders/{order_id}/mandate", json=_mandate()).status_code == 409


# ---------------------------------------------------------------------------- the market


def test_the_market_will_not_open_without_a_mandate() -> None:
    client, _, market, _, dial = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]

    response = client.post(f"/api/orders/{order_id}/rfq")

    assert response.status_code == 409
    assert "no mandate" in response.json()["detail"]
    assert market.planned == []
    assert dial.numbers_dialled == []


def test_opening_the_market_asks_for_at_least_three_carriers() -> None:
    """The brief requires three. The count comes from config, never from a caller."""
    client, _, market, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
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
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    client.post(f"/api/orders/{order_id}/rfq")
    first = list(dial.numbers_dialled)
    client.post(f"/api/orders/{order_id}/rfq")

    assert dial.numbers_dialled == first


def test_the_comparison_carries_the_cap_it_was_ranked_against() -> None:
    client, _, _, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    client.post(f"/api/orders/{order_id}/mandate", json=_mandate())

    body = client.get(f"/api/orders/{order_id}/comparison").json()

    assert body["cap_at_decision_cents"] == 900_000
    assert body["mandate_version"] == 1


# -------------------------------------------------------------------------- the inbox


def _award_approval(store: PortalMemoryStore, order_id: str) -> str:
    """Put an award decision in the human inbox, the way tools/market.py will."""
    return asyncio.run(
        store.raise_approval(
            Approval(
                order_id=order_id,
                kind=ApprovalKind.AWARD_APPROVAL,
                reason=ApprovalReason.AWARD_SELECTED,
                context={"comparison": []},
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
    client, store, market, _, _ = build()
    order_id = client.post("/api/orders", json=_new_order()).json()["id"]
    approval_id = _award_approval(store, order_id)

    response = client.post(
        f"/api/approvals/{approval_id}/decision",
        json={"status": "approved"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert market.awarded == [order_id]


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


def test_portal_requires_a_bearer_token() -> None:
    client, _, _, _, _ = build()
    assert client.get("/api/orders", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_an_unconfigured_store_is_503_not_a_crash() -> None:
    """/health has to answer when the database is the thing that is broken."""

    class Unconfigured(PortalMemoryStore):
        async def list_orders(self) -> list[Order]:
            raise StoreUnavailable("SUPABASE_URL is not configured")

    client, _, _, _, _ = build(Unconfigured())

    assert client.get("/api/orders").status_code == 503
