"""The thirteen endpoints the dashboard consumes.

MAY IMPORT:  domain, config, tools, store.
IMPORTED BY: main.

Reads go straight to store/. Every mutation that acts on something a counterparty said goes
through tools/, which is where policy lives -- and that is why this package may not import
policy: there must be no way to write state here that skips the boundary.

POST /api/orders/{id}/mandate is the exception, and deliberately so. It writes straight to
store/, because tools/ exists to gate *the model*: it evaluates a proposal that arrived in a
conversation against an authority granted elsewhere. A mandate is that authority. There is no
proposal to evaluate and no counterparty involved -- the portal is the source of the
permission -- so routing it through the proposal boundary would not add a check, it would only
blur where authority comes from. Nothing reachable from a phone call can reach this path.

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
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query

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
from app.api.trace import TraceRow, build_trace
from app.config import Settings
from app.domain import (
    Approval,
    ApprovalStatus,
    CallRecord,
    Carrier,
    Commitment,
    DecisionRow,
    DialPlan,
    EventRow,
    Money,
    Order,
    OrderStatus,
    Store,
)
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

    async def decisions_for_call(self, call_id: str) -> list[DecisionRow]: ...

    async def events_for_call(self, call_id: str) -> list[EventRow]: ...

    async def commitments_for(self, order_id: str) -> list[Commitment]: ...

    async def save_order_if_mandate_version(self, order: Order, expected_version: int) -> bool: ...


#: A sweep the router can trigger without importing jobs. Returns the call ids placed.
Sweep = Callable[[], Awaitable[list[str]]]

#: Placing the calls a plan describes. Injected for the same reason as ``sweep``: ``api`` may
#: not import ``vapi`` under the layering contract, and it should not -- the portal is the
#: user-operated surface and the phone is a stranger's. main.py is the one place allowed to
#: know both, so it hands this down with the placer already bound.
Dialler = Callable[[list[DialPlan]], Awaitable[object]]


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
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotImplementedError as exc:
        # A track has not landed its half yet. 501 names it instead of a 500 that reads like
        # a crash; the portal can show the button as not-yet-wired rather than broken.
        raise HTTPException(status_code=501, detail=str(exc)) from exc


def create_api_router(
    store: PortalStore,
    *,
    market: Market,
    sweep: Sweep,
    dial: Dialler,
    now: Callable[[], datetime],
    settings: Settings,
) -> APIRouter:
    async def portal_actor() -> str:
        """The unauthenticated demo portal still records a stable audit actor."""
        return settings.portal_manager_identity.strip() or "portal-operator"

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
    async def set_mandate(
        order_id: str, body: SetMandateRequest, actor: Annotated[str, Depends(portal_actor)]
    ) -> OrderAggregate:
        """The only price-cap writer in the system.

        Bumps the version rather than overwriting in place. Decisions copy the ceiling by
        value, so raising the cap here can never rewrite the explanation of a refusal that
        was made under the old one.
        """
        order = await _load(order_id)
        if order.mandate_version != body.expected_version:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"stale mandate version {body.expected_version}; "
                    f"current version is {order.mandate_version}"
                ),
            )
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
                "mandate_set_by": actor,
                "mandate_set_at": now(),
                "status": OrderStatus.QUOTING,
            }
        )
        with _guard():
            saved = await store.save_order_if_mandate_version(updated, body.expected_version)
            if not saved:
                raise HTTPException(status_code=409, detail="mandate changed concurrently; reload")
            await store.append_event(
                EventRow(
                    order_id=order_id,
                    type="mandate.set",
                    payload={
                        "version": updated.mandate_version,
                        "cap_cents": body.cap_amount_cents,
                        "cap_currency": body.cap_currency,
                        "set_by": actor,
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
            # plan_rfq returns nothing when the market for this mandate version is already
            # claimed, so a second click -- or a second instance pointed at the same database
            # -- reaches this line with an empty list and dials nobody.
            if plans:
                await dial(plans)
        return await get_order(order_id)

    @router.get("/orders/{order_id}/comparison")
    async def get_comparison(order_id: str) -> object:
        """The ranked comparison, losers and reason codes included."""
        order = await _load(order_id)
        with _guard():
            return await market.rank(order)

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
    async def decide_approval(
        approval_id: str,
        body: ApprovalDecisionRequest,
        actor: Annotated[str, Depends(portal_actor)],
    ) -> Approval:
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
            if body.status == "approved" and approval.order_id:
                order = await store.order(approval.order_id)
                if order is not None and str(approval.kind) == "award_approval":
                    await market.award(
                        order,
                        approval.model_copy(
                            update={
                                "status": ApprovalStatus.APPROVED,
                                "decided_by": actor,
                                "note": body.note,
                            }
                        ),
                    )
            await store.resolve_approval(
                approval_id,
                status=body.status,
                decided_by=actor,
                note=body.note,
            )
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

    @router.get("/calls/{call_id}/trace", response_model=list[TraceRow])
    async def get_trace(call_id: str) -> list[TraceRow]:
        """The Decision Trace: what the counterparty did, what Volta did, what happened next.

        A projection over the append-only record and nothing else. The conversational rows
        come from the vendor's transcript, which is untrusted; every row carrying an outcome
        comes from a table we wrote at the moment we wrote it. That is what lets this screen
        show a refusal without asking the model to describe its own behaviour.
        """
        with _guard():
            call = await store.call(call_id)
            if call is None:
                raise HTTPException(status_code=404, detail=f"call {call_id} not found")
            order_id = call.order_id
            quotes = await store.quotes_for(order_id) if order_id else []
            approvals = await store.open_approvals(order_id) if order_id else []
            commitments = await store.commitments_for(order_id) if order_id else []
            decisions = await store.decisions_for_call(call_id)
            events = await store.events_for_call(call_id)
        return build_trace(
            call,
            quotes=quotes,
            decisions=decisions,
            events=events,
            approvals=approvals,
            commitments=commitments,
        )

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
