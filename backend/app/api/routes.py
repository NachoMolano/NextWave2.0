"""The thirteen endpoints the dashboard consumes.

MAY IMPORT:  domain, config, tools, store.
IMPORTED BY: main.

Reads go straight to store/. Every mutation that acts on something a counterparty said goes
through tools/, which is where policy lives -- and that is why this package may not import
policy: there must be no way to write state here that skips the boundary.

POST /api/orders/{id}/mandate is the exception, and deliberately so. It writes straight to
store/, because tools/ exists to gate *the model*: it evaluates a proposal that arrived in a
conversation against an authority granted elsewhere. A mandate is that authority. There is no
proposal to evaluate and no counterparty involved -- an authenticated human is the source of
the permission -- so routing it through the proposal boundary would not add a check, it would
only blur where authority comes from. Nothing reachable from a phone call can reach this path.

The router is a factory rather than a module of globals so that tests build it over
InMemoryStore with no network, and so main.py stays a composition root that names its
dependencies instead of a module that happens to import them.

``sweep`` arrives as a callable because ``api`` may not import ``jobs`` under the layering
contract. That is not an obstacle to work around -- the clock is a different trust level from
the portal, and main.py is the one place allowed to know both.

STATUS: reads and the mandate path are implemented. Endpoints that delegate to a tools/
function still stubbed by its owner answer 501 and name the owner. OWNER: Track C.
"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Protocol

from fastapi import APIRouter, HTTPException, Query

from app.api.schemas import (
    ApprovalDecisionRequest,
    CallDetail,
    DemurrageView,
    MandateView,
    NewOrderRequest,
    OrderAggregate,
    OrderSummary,
    SetMandateRequest,
    SweepResult,
)
from app.config import Settings
from app.domain import (
    Approval,
    CallRecord,
    Carrier,
    DialPlan,
    Money,
    Order,
    OrderStatus,
    Store,
)
from app.domain.models import EventRow
from app.store import AwardConflict, RowNotFound, StoreUnavailable
from app.tools.market import Market

__all__ = ["PortalStore", "create_api_router"]


class PortalStore(Store, Protocol):
    """``Store`` plus the three list reads the portal's screens need.

    The frozen Protocol in ``domain/ports.py`` has no list reads -- it was shaped around one
    call and one order at a time, which is what the phone needs. A queue screen needs the
    other shape. Declared here instead of widening a contract four tracks build against;
    raised in CHANGELOG so it can move into ports.py deliberately rather than by side effect.
    """

    async def list_orders(self) -> list[Order]: ...

    async def list_carriers(self) -> list[Carrier]: ...

    async def calls_for(self, order_id: str) -> list[CallRecord]: ...


#: A sweep the router can trigger without importing jobs. Returns the call ids placed.
Sweep = Callable[[], Awaitable[list[str]]]
#: Places the calls an RFQ planned. A callable rather than the campaign itself because
#: ``api`` may not import ``vapi`` -- main.py has already chosen the placer and the profile.
RfqDialler = Callable[[list[DialPlan]], Awaitable[dict[str, str]]]


@contextmanager
def _guard() -> Iterator[None]:
    """Turn the store's typed failures into the status codes the portal expects.

    AwardConflict is a 409 and never a 500: a second award attempt is the database refusing
    to let two bookings exist, which is the system working, not the system breaking.
    """
    try:
        yield
    except AwardConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except NotImplementedError as exc:
        # A track has not landed its half yet. 501 names it instead of a 500 that reads like
        # a crash; the portal can show the button as not-yet-wired rather than broken.
        raise HTTPException(status_code=501, detail=str(exc)) from exc


def create_api_router(
    store: PortalStore,
    *,
    market: Market,
    sweep: Sweep,
    dial: RfqDialler,
    now: Callable[[], datetime],
    settings: Settings,
) -> APIRouter:
    router = APIRouter(tags=["portal"])

    async def _load(order_id: str) -> Order:
        with _guard():
            order = await store.order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail=f"order {order_id} not found")
        return order

    # ------------------------------------------------------------------------ orders

    @router.post("/orders", response_model=OrderSummary, status_code=201)
    async def create_order(body: NewOrderRequest) -> OrderSummary:
        """A cargo was received at port. Idempotent on the reference.

        The event is written after the row so that a redelivered intake is a no-op rather
        than a second folio: save_order upserts, and the event key is the reference itself.
        """
        with _guard():
            order_id = await store.save_order(Order(id="", **body.model_dump(exclude_unset=False)))
            await store.append_event(
                EventRow(
                    order_id=order_id,
                    type="order.received",
                    payload={"reference": body.reference},
                    idempotency_key=f"order.received:{body.reference}",
                )
            )
            order = await store.order(order_id)
        if order is None:
            raise HTTPException(status_code=500, detail="order vanished after being written")
        return OrderSummary.of(order, now().date(), 0)

    @router.get("/orders", response_model=list[OrderSummary])
    async def list_orders() -> list[OrderSummary]:
        with _guard():
            orders = await store.list_orders()
            open_now = await store.open_approvals()
        today = now().date()
        counts: dict[str, int] = {}
        for approval in open_now:
            if approval.order_id:
                counts[approval.order_id] = counts.get(approval.order_id, 0) + 1
        return [OrderSummary.of(o, today, counts.get(str(o.id), 0)) for o in orders]

    @router.get("/orders/{order_id}", response_model=OrderAggregate)
    async def get_order(order_id: str) -> OrderAggregate:
        """Everything the portal needs about one order, in one call."""
        order = await _load(order_id)
        with _guard():
            quotes = await store.quotes_for(order_id)
            calls = await store.calls_for(order_id)
            commitment = await store.live_commitment(order_id)
            approvals = await store.open_approvals(order_id)
        return OrderAggregate(
            order=order,
            mandate=MandateView.of(order),
            demurrage=DemurrageView.of(order, now().date()),
            quotes=quotes,
            calls=calls,
            commitment=commitment,
            approvals=approvals,
        )

    @router.post("/orders/{order_id}/mandate", response_model=OrderAggregate)
    async def set_mandate(order_id: str, body: SetMandateRequest) -> OrderAggregate:
        """The only price-cap writer in the system.

        Bumps the version rather than overwriting in place. Decisions copy the ceiling by
        value, so raising the cap here can never rewrite the explanation of a refusal that
        was made under the old one.
        """
        order = await _load(order_id)
        updated = order.model_copy(
            update={
                "cap": Money(cents=body.cap_amount_cents, currency=body.cap_currency),
                "target": (
                    Money(cents=body.target_amount_cents, currency=body.cap_currency)
                    if body.target_amount_cents is not None
                    else None
                ),
                "pickup_not_before": body.pickup_not_before,
                "pickup_not_after": body.pickup_not_after,
                "delivery_deadline": body.delivery_deadline or order.delivery_deadline,
                "commitment_mode": body.commitment_mode,
                "mandate_version": order.mandate_version + 1,
                "mandate_set_by": body.set_by,
                "mandate_set_at": now(),
                "status": OrderStatus.QUOTING,
            }
        )
        with _guard():
            await store.save_order(updated)
            await store.append_event(
                EventRow(
                    order_id=order_id,
                    type="mandate.set",
                    payload={
                        "version": updated.mandate_version,
                        "cap_cents": body.cap_amount_cents,
                        "cap_currency": body.cap_currency,
                        "set_by": body.set_by,
                    },
                    idempotency_key=f"mandate.set:{order_id}:v{updated.mandate_version}",
                )
            )
        return await get_order(order_id)

    @router.post("/orders/{order_id}/rfq", response_model=OrderAggregate)
    async def start_rfq(order_id: str) -> OrderAggregate:
        """Open the market. Refuses without a mandate: nothing is authorized yet."""
        order = await _load(order_id)
        if not order.has_mandate:
            raise HTTPException(
                status_code=409,
                detail=f"order {order.reference} has no mandate: nothing is authorized",
            )
        with _guard():
            plans = await market.plan_rfq(order, settings.rfq_carrier_count)
        # Outside the guard: a dial failure is not a store failure, and run_campaign already
        # absorbs a single carrier's failure so the other two still ring.
        if plans:
            await dial(plans)
        return await get_order(order_id)

    @router.get("/orders/{order_id}/comparison")
    async def get_comparison(order_id: str) -> object:
        """The ranked comparison, losers and reason codes included."""
        order = await _load(order_id)
        with _guard():
            return await market.rank(order)

    @router.post("/orders/{order_id}/renegotiate", response_model=OrderAggregate)
    async def renegotiate(order_id: str) -> OrderAggregate:
        """Move something already agreed. Same path as the RFQ, at phase=renegotiation.

        There is no entry point for this in tools/ yet -- see CHANGELOG, where it is raised
        with Track E. It answers 501 rather than pretending, and its test is skipped.
        """
        await _load(order_id)
        raise HTTPException(
            status_code=501,
            detail="Track E: tools/market.py has no renegotiate entry point yet",
        )

    # --------------------------------------------------------------------- approvals

    @router.get("/approvals", response_model=list[Approval])
    async def list_approvals(
        status: str = Query(default="open", pattern="^open$"),
        order_id: str | None = Query(default=None),
    ) -> list[Approval]:
        """The one human inbox: awards, escalations and incidents on one screen."""
        with _guard():
            return await store.open_approvals(order_id)

    @router.post("/approvals/{approval_id}/decision", response_model=Approval)
    async def decide_approval(approval_id: str, body: ApprovalDecisionRequest) -> Approval:
        """Steps 9 and 10. Approving an award is what engages the single-award lock."""
        with _guard():
            approval = await store.approval(approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail=f"approval {approval_id} not found")
        if approval.status is not None and str(approval.status) != "open":
            raise HTTPException(
                status_code=409, detail=f"approval {approval_id} is already {approval.status}"
            )

        with _guard():
            await store.resolve_approval(
                approval_id,
                status=body.status,
                decided_by=body.decided_by,
                note=body.note,
            )
            if body.status == "approved" and approval.order_id:
                order = await store.order(approval.order_id)
                if order is not None and str(approval.kind) == "award_approval":
                    await market.award(order, approval)
            decided = await store.approval(approval_id)
        if decided is None:
            raise HTTPException(status_code=500, detail="approval vanished after being decided")
        return decided

    # ------------------------------------------------------------------------- calls

    @router.get("/calls", response_model=list[CallDetail])
    async def list_calls(order_id: str = Query(...)) -> list[CallDetail]:
        with _guard():
            calls = await store.calls_for(order_id)
            return [CallDetail(call=c, report=None, carrier=None) for c in calls]

    @router.get("/calls/{call_id}", response_model=CallDetail)
    async def get_call(call_id: str) -> CallDetail:
        """The brief, the transcript, the recording and the anchors."""
        with _guard():
            call = await store.call(call_id)
            if call is None:
                raise HTTPException(status_code=404, detail=f"call {call_id} not found")
            report = await store.report_for(call_id)
            carrier = await store.carrier(call.carrier_id) if call.carrier_id else None
        return CallDetail(call=call, report=report, carrier=carrier)

    # ---------------------------------------------------------------------- carriers

    @router.get("/carriers", response_model=list[Carrier])
    async def list_carriers() -> list[Carrier]:
        with _guard():
            return await store.list_carriers()

    # -------------------------------------------------------------------------- jobs

    @router.post("/jobs/sweep", response_model=SweepResult)
    async def run_sweep() -> SweepResult:
        """The demo button. Idempotent by events.idempotency_key, so a second press dials
        nothing -- which is the assertion worth making in front of a judge."""
        with _guard():
            return SweepResult(call_ids=await sweep())

    return router
