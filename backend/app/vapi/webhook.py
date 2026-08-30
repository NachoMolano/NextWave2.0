"""POST /vapi/events -- every Vapi server message, on one URL.

Verify X-Vapi-Secret before parsing the body, then switch on message.type:

  assistant-request           inbound. Look up the carrier by message.call.from.phoneNumber
                              and return a transient assistant. HARD BUDGET: 7.5 seconds,
                              fixed and not configurable. Put a ~2s timeout on the lookup and
                              fall back to the unverified-caller assistant rather than
                              blowing the deadline.
  status-update               -> tools/calls.py
  end-of-call-report          store recording + transcript, then agent/report.py
  transfer-destination-request  write the approval, then return the manager's number -- or
                              refuse. Escalation is decided here, server-side, not by the
                              model choosing a destination.

STATUS: Phase 0 stub. OWNER: Track B.
"""

from fastapi import APIRouter

__all__ = ["create_webhook_router"]


def create_webhook_router() -> APIRouter:
    raise NotImplementedError("Track B: implement app/vapi/webhook.py")
