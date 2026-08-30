"""Idempotent call-lifecycle writes, and the anchor every piece of evidence hangs from.

Vapi redelivers webhooks and they arrive out of order -- a status-update can land after the
end-of-call-report. Every write here keys on ``vapi_call_id`` and on
``events.idempotency_key``, so a second delivery is a no-op rather than a second row.

``anchor_ms`` is measured server-side, at the instant a tool fires, as
``now - call.started_at``. It is deliberately not read from the vendor transcript:
``artifact.messages[].secondsFromStart`` is not documented and has a reported epoch-value
bug, and evidence resting on an unverified field is not evidence. When an end-of-call report
does carry offsets they are reconciled against ours, and a mismatch is recorded as an event
rather than silently overwriting what we measured.

OWNER: Track E.
"""

from collections.abc import Callable
from datetime import datetime

import structlog

from app.domain import CallRecord, CallStatus, EventRow, Store

log = structlog.get_logger(__name__)

__all__ = ["CallLedger"]

#: Fields an out-of-order delivery must never blank out. A ``status-update`` that arrives
#: after the ``end-of-call-report`` carries no recording and no transcript, and applying it
#: naively would erase both. Merge instead: a later delivery may add, never remove.
_PRESERVED = (
    "recording_url",
    "ended_at",
    "ended_reason",
    "cost_cents",
    "order_id",
    "carrier_id",
    "started_at",
    "context",
)


class CallLedger:
    def __init__(self, store: Store, *, now: Callable[[], datetime]) -> None:
        self._store = store
        self._now = now

    async def upsert_from_webhook(self, call: CallRecord, event_key: str) -> str | None:
        """Apply a status-update. Returns None when this delivery was already applied."""
        return await self._apply(call, event_key, "call.status_update")

    async def finalize(self, call: CallRecord, event_key: str) -> str | None:
        """Apply an end-of-call-report: recording, transcript, ended_reason, cost."""
        return await self._apply(call, event_key, "call.ended")

    async def _apply(self, call: CallRecord, event_key: str, event_type: str) -> str | None:
        existing = await self._store.call_by_vapi_id(call.vapi_call_id)
        accepted = await self._store.append_event(
            EventRow(
                order_id=call.order_id or (existing.order_id if existing else None),
                call_id=existing.id if existing else None,
                type=event_type,
                payload={"vapi_call_id": call.vapi_call_id, "status": call.status.value},
                idempotency_key=event_key,
            )
        )
        if not accepted:
            # A redelivery. Normal, not an error -- Vapi retries on any non-2xx and on
            # timeouts, so the same report can arrive several times for one call.
            return None
        return await self._store.upsert_call(self._merge(existing, call))

    @staticmethod
    def _merge(existing: CallRecord | None, incoming: CallRecord) -> CallRecord:
        """Later deliveries add facts; they never take one away.

        Out-of-order delivery is the reason this is not a plain overwrite. Vapi does not
        guarantee ordering, so the ``ended`` row can be followed by a ``ringing`` row for
        the same call, and last-write-wins would walk a finished call backwards.
        """
        if existing is None:
            return incoming
        update: dict[str, object] = {"id": existing.id}
        for field in _PRESERVED:
            if getattr(incoming, field, None) in (None, {}, "") and getattr(existing, field, None):
                update[field] = getattr(existing, field)
        if not incoming.transcript and existing.transcript:
            update["transcript"] = existing.transcript
        # Identity is monotonic: it may only ever demand more, never concede more. A late
        # webhook carrying the default 0 must not un-verify a caller who already passed.
        if existing.identity_level > incoming.identity_level:
            update["identity_level"] = existing.identity_level
            update["identity_verified"] = existing.identity_verified
        if existing.status is CallStatus.ENDED and incoming.status is not CallStatus.ENDED:
            update["status"] = existing.status
        return incoming.model_copy(update=update)

    async def anchor_ms(self, call_id: str) -> int:
        """Milliseconds since this call started. The evidence offset, measured by us.

        Returns 0 when the call has no ``started_at`` -- a tool firing on a call that never
        reported a start. That is deliberately soft here and hard one layer up: a quote may
        carry a zero anchor, because a quote is a record of what somebody said, but
        ``tools/model.py::confirm_preagreement`` refuses on the same condition, so nothing
        ever *binds* on an offset we did not measure.
        """
        call = await self._store.call(call_id)
        if call is None or call.started_at is None:
            await self._store.append_event(
                EventRow(
                    call_id=call_id,
                    type="call.anchor_unmeasurable",
                    payload={"reason": "call has no started_at"},
                    idempotency_key=f"anchor-unmeasurable:{call_id}",
                )
            )
            return 0
        elapsed = (self._now() - call.started_at).total_seconds() * 1000
        return max(0, int(elapsed))
