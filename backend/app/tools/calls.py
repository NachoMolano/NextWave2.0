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

STATUS: Phase 0 stub. OWNER: Track E.
"""

from collections.abc import Callable
from datetime import datetime

from app.domain import CallRecord, Store

__all__ = ["CallLedger"]


class CallLedger:
    def __init__(self, store: Store, *, now: Callable[[], datetime]) -> None:
        self._store = store
        self._now = now

    async def upsert_from_webhook(self, call: CallRecord, event_key: str) -> str | None:
        """Apply a status-update. Returns None when this delivery was already applied."""
        raise NotImplementedError("Track E: implement app/tools/calls.py")

    async def finalize(self, call: CallRecord, event_key: str) -> str | None:
        """Apply an end-of-call-report: recording, transcript, ended_reason, cost."""
        raise NotImplementedError("Track E: implement app/tools/calls.py")

    async def anchor_ms(self, call_id: str) -> int:
        """Milliseconds since this call started. The evidence offset, measured by us."""
        raise NotImplementedError("Track E: implement app/tools/calls.py")
