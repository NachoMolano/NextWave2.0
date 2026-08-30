"""The commitment state machine. The only writer of the commitments table.

    VERBAL -> RECAP_SENT -> COMMITTED -> EXECUTED
      |                        |
      +-> NOT_COMMITTED        +-> SUPERSEDED

A call can only ever produce VERBAL. What promotes a commitment is the written recap
actually leaving: ``notifications.status = 'sent'`` is the gate. A failed send leaves the
commitment unpromoted and raises an approval. It does not retry -- a send whose outcome is
unknown may already have reached the carrier, and a second one would be a second booking.

Renegotiation creates a new commitment pointing at the old one through ``superseded_by``. It
never edits the old row: what was agreed at the time is a fact, and the later change is a
different fact.

STATUS: Phase 0 stub. OWNER: Track A.
"""

from collections.abc import Callable
from datetime import datetime

from app.domain import Commitment, Notifier, Store

__all__ = ["CommitmentCoordinator"]


class CommitmentCoordinator:
    """A server-side capability. It deliberately exposes no method the model can reach.

    There is no ``commit()`` here for a tool to call: promotion happens as a consequence of a
    delivery result, never as an instruction that arrived in a conversation.
    """

    def __init__(self, store: Store, notifier: Notifier, *, now: Callable[[], datetime]) -> None:
        self._store = store
        self._notifier = notifier
        self._now = now

    async def open_verbal(self, commitment: Commitment) -> str:
        """Record a pre-agreement. Requires an anchor, which the type already enforces."""
        raise NotImplementedError("Track A: implement app/tools/commitments.py")

    async def send_recap_and_promote(self, commitment_id: str) -> Commitment:
        """Send the official written recap; promote to COMMITTED only if it actually left."""
        raise NotImplementedError("Track A: implement app/tools/commitments.py")

    async def supersede(self, commitment_id: str, replacement: Commitment) -> str:
        """Renegotiation. The old row stays; the new one takes the live slot."""
        raise NotImplementedError("Track A: implement app/tools/commitments.py")
