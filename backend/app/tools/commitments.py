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

OWNER: Track A.
"""

from collections.abc import Callable
from datetime import datetime

from app.domain import (
    Approval,
    ApprovalKind,
    ApprovalReason,
    Commitment,
    CommitmentState,
    DeliveryStatus,
    EventRow,
    Notifier,
    OrderStatus,
    OutboundMessage,
    Store,
)

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
        """Record a pre-agreement. Requires an anchor, which the type already enforces.

        Raises ``AwardConflict`` when the order already holds a live commitment. That check
        belongs to the store -- in Postgres it is the partial unique index on
        ``commitments (order_id) where state not in (...)`` -- because two carriers
        confirming at the same instant must not both read an empty slot on the way in.
        """
        commitment_id = await self._store.save_commitment(
            commitment.model_copy(update={"state": CommitmentState.VERBAL})
        )
        await self._store.append_event(
            EventRow(
                order_id=commitment.order_id,
                call_id=commitment.evidence_call_id,
                type="commitment.verbal",
                payload={
                    "commitment_id": commitment_id,
                    "quote_id": commitment.quote_id,
                    "anchor_ms": commitment.evidence_anchor_ms,
                },
                idempotency_key=f"commitment-verbal:{commitment_id}",
            )
        )
        return commitment_id

    async def send_recap_and_promote(
        self, commitment_id: str, message: OutboundMessage
    ) -> Commitment:
        """Send the official written recap; promote to COMMITTED only if it actually left.

        ``message`` is rendered by ``notify/render.py`` (Track D) and passed in. The wording
        of a recap and the authority to promote on one are different concerns, and Track A
        must not import a renderer to get at the first.

        The state moves to RECAP_SENT *before* the send, not after. If the process dies
        mid-send the stored state then says "this may have gone out", which is the honest
        reading and the one that stops anybody re-sending: a duplicate recap is a second
        booking.
        """
        commitment = self._require(await self._store.commitment(commitment_id))

        # The event insert is the atomic outbox claim. Only the caller that inserts it may
        # contact the provider; a replay or concurrent worker sees False and sends nothing.
        claimed = await self._store.append_event(
            EventRow(
                order_id=commitment.order_id,
                call_id=commitment.evidence_call_id,
                type="commitment.recap_claimed",
                payload={"commitment_id": commitment_id, "channel": str(message.channel)},
                idempotency_key=f"commitment-recap-claimed:{commitment_id}",
            )
        )
        if not claimed:
            return self._require(await self._store.commitment(commitment_id))

        await self._store.update_commitment(
            commitment.model_copy(update={"state": CommitmentState.RECAP_SENT})
        )
        result = await self._notifier.send(message)
        await self._store.record_delivery(message, result)

        if result.status is not DeliveryStatus.SENT:
            # A failed recap does not mean a defective commitment. It means there was no
            # commitment: nothing was ever put to the carrier in writing.
            await self._store.raise_approval(
                Approval(
                    order_id=commitment.order_id,
                    call_id=commitment.evidence_call_id,
                    kind=ApprovalKind.ESCALATION,
                    reason=ApprovalReason.POLICY_FAILURE,
                    context={
                        "commitment_id": commitment_id,
                        "detail": (
                            "the written recap outcome is unresolved; the commitment is not "
                            "promoted and will not be re-sent automatically"
                            if result.status is DeliveryStatus.UNKNOWN
                            else "the written recap did not leave; the commitment is not "
                            "promoted and will not be re-sent automatically"
                        ),
                        "error": result.error or "unknown",
                    },
                    raised_at=self._now(),
                )
            )
            await self._store.append_event(
                EventRow(
                    order_id=commitment.order_id,
                    type=(
                        "commitment.recap_unknown"
                        if result.status is DeliveryStatus.UNKNOWN
                        else "commitment.recap_failed"
                    ),
                    payload={
                        "commitment_id": commitment_id,
                        "status": str(result.status),
                        "error": result.error or "unknown",
                    },
                    idempotency_key=f"commitment-recap-{result.status}:{commitment_id}",
                )
            )
            return commitment.model_copy(update={"state": CommitmentState.RECAP_SENT})

        promoted = commitment.model_copy(update={"state": CommitmentState.COMMITTED})
        await self._store.update_commitment(promoted)
        await self._store.set_order_status(commitment.order_id, OrderStatus.BOOKED)
        await self._store.append_event(
            EventRow(
                order_id=commitment.order_id,
                type="commitment.committed",
                payload={
                    "commitment_id": commitment_id,
                    "provider_message_id": result.provider_message_id or "",
                },
                idempotency_key=f"commitment-committed:{commitment_id}",
            )
        )
        return promoted

    async def supersede(self, commitment_id: str, replacement: Commitment) -> str:
        """Renegotiation. The old row stays; the new one takes the live slot.

        The old commitment is retired first so the replacement has a slot to occupy -- the
        one-live-per-order rule is enforced by the store, and doing this in the other order
        would trip it. What the old row records about what was agreed at the time is never
        touched.
        """
        old = self._require(await self._store.commitment(commitment_id))
        await self._store.update_commitment(
            old.model_copy(update={"state": CommitmentState.SUPERSEDED})
        )
        new_id = await self._store.save_commitment(
            replacement.model_copy(update={"state": CommitmentState.VERBAL})
        )
        await self._store.update_commitment(
            old.model_copy(update={"state": CommitmentState.SUPERSEDED, "superseded_by": new_id})
        )
        await self._store.append_event(
            EventRow(
                order_id=replacement.order_id,
                call_id=replacement.evidence_call_id,
                type="commitment.superseded",
                payload={"old_commitment_id": commitment_id, "new_commitment_id": new_id},
                idempotency_key=f"commitment-superseded:{commitment_id}:{new_id}",
            )
        )
        return new_id

    @staticmethod
    def _require(commitment: Commitment | None) -> Commitment:
        if commitment is None:
            raise ValueError("no such commitment")
        return commitment
