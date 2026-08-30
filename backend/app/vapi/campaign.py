"""Parallel dial fan-out. Three carriers at once is the point of the whole exercise.

asyncio.gather over a plan from tools/market.py, bounded by a semaphore below Vapi's
default of 10 concurrent slots so a retry has room. A call that comes back queued with
concurrencyBlocked is retried, not failed.

STATUS: Phase 0 stub. OWNER: Track B.
"""

from app.config import Settings
from app.domain import CallPlacer, DialPlan

__all__ = ["run_campaign"]


async def run_campaign(
    plans: list[DialPlan], placer: CallPlacer, settings: Settings
) -> dict[str, str]:
    """Dial every plan concurrently. Returns {call_id: vapi_call_id} for the ones placed."""
    raise NotImplementedError("Track B: implement app/vapi/campaign.py")
