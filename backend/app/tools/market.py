"""Multi-carrier strategy: who to call, how they compare, and who wins.

RFQ and AWARD are separate phases on purpose. Several carriers may hold live offers at once;
only one call may close. Two open bookings is the worst failure the brief describes, and the
enforcement is the partial unique index on ``quotes`` -- not a check in this file, which two
concurrent confirmations could both pass on their way to writing.

The comparison keeps the losers and their reason codes. A comparison listing only the winner
cannot be audited, and "why not that one" is the question a human actually asks.

STATUS: Phase 0 stub. OWNER: Track E.
"""

from collections.abc import Callable
from datetime import datetime

from app.domain import Approval, Comparison, DialPlan, Order, Store

__all__ = ["Market"]


class Market:
    def __init__(self, store: Store, *, now: Callable[[], datetime]) -> None:
        self._store = store
        self._now = now

    async def plan_rfq(self, order: Order, count: int) -> list[DialPlan]:
        """Pick carriers, create a call row each, and freeze one CallContext per call.

        Each context carries the market state as of dial time -- how many quotes are in hand
        and the best so far -- so the third call negotiates with two numbers behind it and
        the first with none.
        """
        raise NotImplementedError("Track E: implement app/tools/market.py")

    async def rank(self, order: Order) -> Comparison:
        """Re-evaluate every quote against the current mandate, then select the best."""
        raise NotImplementedError("Track E: implement app/tools/market.py")

    async def request_award_approval(self, order: Order, comparison: Comparison) -> Approval:
        """Hand the ranked comparison to a human; move the order to awaiting_approval."""
        raise NotImplementedError("Track E: implement app/tools/market.py")

    async def award(self, order: Order, approval: Approval) -> str:
        """Accept the winning quote. Raises AwardConflict if the slot is already taken."""
        raise NotImplementedError("Track E: implement app/tools/market.py")
