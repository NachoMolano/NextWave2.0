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

STATUS: Phase 0 stub. OWNER: Track E.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime

from app.config import Settings
from app.domain import CallPlacer, Store

__all__ = ["run_forever", "sweep_deadlines", "timeout_open_markets"]


async def sweep_deadlines(
    store: Store, placer: CallPlacer, settings: Settings, *, now: Callable[[], datetime]
) -> list[str]:
    """Call every carrier whose delivery is overdue. Returns the call ids placed."""
    raise NotImplementedError("Track E: implement app/jobs.py")


async def timeout_open_markets(
    store: Store, settings: Settings, *, now: Callable[[], datetime]
) -> list[str]:
    """Rank any RFQ that has been open past the timeout. Returns the order ids ranked."""
    raise NotImplementedError("Track E: implement app/jobs.py")


async def run_forever(
    store: Store, placer: CallPlacer, settings: Settings, *, now: Callable[[], datetime]
) -> None:
    """The loop main.py starts at boot.

    An exception in one tick is logged and swallowed. A sweep that dies takes OUTBOUND 2
    with it silently, which is worse than a tick that failed and will run again in a minute.
    """
    while True:
        await asyncio.sleep(settings.sweep_interval_seconds)
        raise NotImplementedError("Track E: implement app/jobs.py")
