"""The clock. Two things happen because time passed rather than because someone called.

MAY IMPORT:  domain, config, tools, vapi.
IMPORTED BY: main.

  * the deadline sweep -- OUTBOUND 2. An order whose delivery_deadline has passed with
    nothing underway gets one call asking what happened.
  * the RFQ timeout -- a market whose last call never ended still has to be ranked, or the
    human waits forever for an approval that is never requested.

Both are idempotent through ``events.idempotency_key``, so a restart mid-sweep cannot
double-dial a carrier. The key is derived from the fact that triggered it
(``chase:{order_id}:{deadline}``), not from the moment the loop happened to run -- a key
containing a timestamp would make every tick unique and defeat the point.

OWNER: Track E.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Protocol, cast

import structlog

from app.config import Settings
from app.domain import (
    Approval,
    CallContext,
    CallDirection,
    CallPhase,
    CallPlacer,
    CallRecord,
    CallStatus,
    Comparison,
    DialPlan,
    EventRow,
    Order,
    OrderStatus,
    Store,
)
from app.tools.market import Market
from app.vapi.assistant import profile_from_settings
from app.vapi.campaign import run_campaign

__all__ = ["run_forever", "sweep_deadlines", "timeout_open_markets"]

log = structlog.get_logger(__name__)

#: How a sweep turns plans into calls. Injected so the suite can run the whole clock with no
#: Vapi and no credit spend; ``main.py`` passes nothing and gets the real fan-out.
Dialler = Callable[[list[DialPlan], CallPlacer, Settings], Awaitable[dict[str, str]]]
AwardAlert = Callable[[Comparison, Approval], Awaitable[None]]


class CallListingStore(Store, Protocol):
    async def calls_for(self, order_id: str) -> list[CallRecord]: ...


async def sweep_deadlines(
    store: Store,
    placer: CallPlacer,
    settings: Settings,
    *,
    now: Callable[[], datetime],
    dial: Dialler | None = None,
) -> list[str]:
    """Call every carrier whose delivery is overdue. Returns the call ids placed."""
    moment = now()
    plans: list[DialPlan] = []

    for order in await store.due_for_chase(moment):
        if order.delivery_deadline is None or order.assigned_carrier_id is None:
            continue
        # Keyed on the deadline that triggered it, not on this tick. A key carrying the
        # current time would be unique on every pass and the sweep would dial once a minute
        # forever.
        accepted = await store.append_event(
            EventRow(
                order_id=order.id,
                type="chase.started",
                payload={"deadline": order.delivery_deadline.isoformat()},
                idempotency_key=f"chase:{order.id}:{order.delivery_deadline.isoformat()}",
            )
        )
        if not accepted:
            continue

        carrier = await store.carrier(order.assigned_carrier_id)
        if carrier is None or not carrier.phone:
            continue

        context = CallContext(
            phase=CallPhase.STATUS_CHECK,
            today=moment.strftime("%A, %d %B %Y"),
            reference=order.reference,
            origin=order.origin,
            destination=order.destination,
            cargo=order.cargo,
            equipment=order.equipment,
            counterparty_name=carrier.name,
            counterparty_contact=carrier.contact_name,
            missed_deadline=order.delivery_deadline.strftime("%A, %d %B %Y at %H:%M"),
        )
        call_id = await store.upsert_call(
            CallRecord(
                vapi_call_id=f"pending:chase:{order.id}",
                direction=CallDirection.OUTBOUND,
                phase=CallPhase.STATUS_CHECK.value,
                order_id=order.id,
                carrier_id=carrier.id,
                to_number=carrier.phone,
                started_at=moment,
                context=context.model_dump(mode="json"),
            )
        )
        plans.append(
            DialPlan(
                call_id=call_id,
                carrier=carrier,
                to_number=carrier.phone,
                context=context.model_dump(mode="json"),
            )
        )

    if plans:
        if dial is None:
            # Same re-keying as the injected dialler does; see Store.attach_vapi_call_id.
            placed = await run_campaign(
                plans, placer, settings, profile=profile_from_settings(settings)
            )
            for our_id, provider_id in placed.items():
                await store.attach_vapi_call_id(our_id, provider_id)
        else:
            await dial(plans, placer, settings)
    return [plan.call_id for plan in plans]


async def timeout_open_markets(
    store: Store,
    settings: Settings,
    *,
    now: Callable[[], datetime],
    placer: CallPlacer | None = None,
    dial: Dialler | None = None,
    alert: AwardAlert | None = None,
) -> list[str]:
    """Close RFQ, run one bounded renegotiation round, then request approval."""
    moment = now()
    cutoff = moment - timedelta(minutes=settings.rfq_timeout_minutes)
    market = Market(store, now=now)
    ranked: list[str] = []

    for order in await store.orders_in_status(OrderStatus.QUOTING):
        calls = await _calls_for(store, order.id)
        rfq_calls = [call for call in calls if call.phase == CallPhase.RFQ.value]
        renegotiation_calls = [
            call for call in calls if call.phase == CallPhase.RENEGOTIATION.value
        ]
        phase = CallPhase.RENEGOTIATION if renegotiation_calls else CallPhase.RFQ
        phase_calls = renegotiation_calls or rfq_calls
        if not _calls_are_closed(phase_calls, cutoff):
            continue
        accepted = await store.append_event(
            EventRow(
                order_id=order.id,
                type=f"{phase.value}.closed",
                payload={"cutoff": cutoff.isoformat(), "call_count": len(phase_calls)},
                idempotency_key=f"{phase.value}-closed:{order.id}:{order.mandate_version}",
            )
        )
        if not accepted:
            continue
        await _close_stalled_calls(store, phase_calls, moment)
        comparison = await market.rank(order)
        if phase is CallPhase.RFQ:
            plans = await market.plan_renegotiation(order, comparison)
            if plans and placer is not None:
                if dial is None:
                    await run_campaign(
                        plans, placer, settings, profile=profile_from_settings(settings)
                    )
                else:
                    await dial(plans, placer, settings)
                continue
        approval = await market.request_award_approval(order, comparison)
        if alert is not None:
            await alert(comparison, approval)
        ranked.append(order.id)
    return ranked


async def _close_stalled_calls(
    store: Store, calls: list[CallRecord], moment: datetime
) -> None:
    """Mark calls that never reported an end as failed, once the market has closed.

    A call can die before it ever starts -- Vapi ended one of the 30 Aug calls with
    ``call.start.error-get-transport`` and sent no webhook at all, so its row sat at QUEUED
    with no ``ended_at`` while the portal showed it beside two that had finished. The market
    was never blocked (``_calls_are_closed`` falls back to the cutoff for exactly this), but
    a queued row that will never move is a lie on the screen.

    Here rather than in a sweep of its own because this is the moment we have already
    decided those calls are over. Writing FAILED at any earlier point would be a guess.
    """
    for call in calls:
        if call.status in (CallStatus.ENDED, CallStatus.FAILED):
            continue
        await store.upsert_call(
            call.model_copy(update={"status": CallStatus.FAILED, "ended_at": moment})
        )


async def _market_is_closed(store: Store, order: Order, cutoff: datetime) -> bool:
    """A market closes when every call has ended, or when the oldest one is past the cutoff.

    The second clause is the one that matters. A call that never reports an end -- the
    carrier hung up during a vendor hiccup, the webhook never arrived -- would otherwise
    hold the market open forever and the human would wait for an approval nobody requested.
    """
    calls = [
        call for call in await _calls_for(store, order.id) if call.phase == CallPhase.RFQ.value
    ]
    return _calls_are_closed(calls, cutoff)


def _calls_are_closed(calls: list[CallRecord], cutoff: datetime) -> bool:
    if not calls:
        return False
    if all(call.status in (CallStatus.ENDED, CallStatus.FAILED) for call in calls):
        return True
    started = [call.started_at for call in calls if call.started_at is not None]
    return bool(started) and min(started) < cutoff


async def _calls_for(store: Store, order_id: str) -> list[CallRecord]:
    """Every call on one order.

    ``Store`` has no list-calls-by-order read, and adding one is a second cross-track ask on
    top of the two this branch already makes. The RFQ timeout is the only caller, the row
    count per order is three, and Track C can collapse this into one indexed query later --
    ``calls_order_idx`` already exists in 0001_init.sql for exactly that.
    """
    return await cast(CallListingStore, store).calls_for(order_id)


async def run_forever(
    store: Store,
    placer: CallPlacer,
    settings: Settings,
    *,
    now: Callable[[], datetime],
    dial: Dialler | None = None,
    alert: AwardAlert | None = None,
) -> None:
    """The loop main.py starts at boot.

    An exception in one tick is logged and swallowed. A sweep that dies takes OUTBOUND 2
    with it silently, which is worse than a tick that failed and will run again in a minute.
    """
    while True:
        await asyncio.sleep(settings.sweep_interval_seconds)
        try:
            dialled = await sweep_deadlines(store, placer, settings, now=now, dial=dial)
            ranked = await timeout_open_markets(
                store, settings, now=now, placer=placer, dial=dial, alert=alert
            )
            if dialled or ranked:
                log.info("jobs.tick", dialled=len(dialled), ranked=len(ranked))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Swallowed on purpose. The alternative is a loop that dies on one bad row and
            # takes the deadline sweep with it, silently, until somebody notices at 4am.
            log.exception("jobs.tick_failed")
