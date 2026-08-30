"""Parallel dial fan-out. Three carriers at once is the point of the whole exercise.

asyncio.gather over a plan from tools/market.py, bounded by a semaphore below Vapi's
default of 10 concurrent slots so a retry has room. A call that comes back queued with
concurrencyBlocked is retried, not failed.

Two decisions worth stating:

  * **One carrier's failure never cancels the others.** ``gather`` runs with
    ``return_exceptions=True`` and the failures come back as log lines and a missing key.
    An RFQ that dialled two of three carriers is a smaller market; an RFQ that cancelled
    the other two because the first number was disconnected is no market at all.
  * **Only ``ConcurrencyBlocked`` is retried.** Every other failure may already have
    dialled, and a blind retry of a request that reached the network is a second real phone
    call to a real carrier.

STATUS: built. OWNER: Track B.
"""

import asyncio
from collections.abc import Awaitable, Callable

import structlog

from app.config import Settings
from app.domain import CallContext, CallPlacer, CompanyProfile, DialPlan
from app.vapi.assistant import build_assistant
from app.vapi.client import ConcurrencyBlocked

__all__ = ["run_campaign"]

log = structlog.get_logger(__name__)

#: Attempts per carrier when every slot is busy. Small on purpose: an RFQ that spends four
#: minutes waiting for a slot has missed the window it was placed to beat.
_MAX_CONCURRENCY_ATTEMPTS = 4

#: Seconds to wait before asking Vapi for a slot again. Doubles per attempt.
_BACKOFF_SECONDS = 2.0


async def run_campaign(
    plans: list[DialPlan],
    placer: CallPlacer,
    settings: Settings,
    *,
    profile: CompanyProfile,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, str]:
    """Dial every plan concurrently. Returns {call_id: vapi_call_id} for the ones placed.

    ``profile`` is a parameter rather than something derived here because the assistant is
    composed per call: each context carries the market state as of dial time, so the third
    carrier is negotiated with two numbers already in hand and the first with none.

    ``sleep`` is injected so the retry path is testable without spending the wall-clock time
    the retry exists to spend.
    """
    if not plans:
        return {}

    limit = max(1, settings.max_concurrent_calls)
    gate = asyncio.Semaphore(limit)

    async def dial(plan: DialPlan) -> tuple[str, str]:
        context = CallContext.model_validate(plan.context)
        assistant = build_assistant(profile, context, settings)

        delay = _BACKOFF_SECONDS
        for attempt in range(1, _MAX_CONCURRENCY_ATTEMPTS + 1):
            async with gate:
                try:
                    vapi_call_id = await placer.place(assistant, plan.to_number)
                except ConcurrencyBlocked:
                    if attempt == _MAX_CONCURRENCY_ATTEMPTS:
                        raise
                    log.info(
                        "vapi.campaign.concurrency_blocked",
                        call_id=plan.call_id,
                        carrier=plan.carrier.name,
                        attempt=attempt,
                    )
                else:
                    return plan.call_id, vapi_call_id
            # Outside the semaphore: holding a slot while waiting for a slot is how a
            # backoff turns into a deadlock.
            await sleep(delay)
            delay *= 2
        raise AssertionError("unreachable: the final attempt either returns or raises")

    outcomes = await asyncio.gather(*(dial(plan) for plan in plans), return_exceptions=True)

    placed: dict[str, str] = {}
    for plan, outcome in zip(plans, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            log.error(
                "vapi.campaign.dial_failed",
                call_id=plan.call_id,
                carrier=plan.carrier.name,
                error=str(outcome),
            )
            continue
        call_id, vapi_call_id = outcome
        placed[call_id] = vapi_call_id

    log.info("vapi.campaign.finished", requested=len(plans), placed=len(placed))
    return placed
