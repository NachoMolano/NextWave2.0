"""The three things Volta writes.

  * the official commitment email -- the confirmation brief plus the commitment register:
    every agreed term with the audio timestamp at which it was agreed, and a recording link;
  * the award approval request -- the whole ranked comparison, losers and reason codes
    included;
  * the incident report -- an inbound call or a missed deadline, with a short WhatsApp
    variant.

STATUS: Phase 0 stub. OWNER: Track D.
"""

from app.domain import CallReport, Commitment, Comparison, OutboundMessage

__all__ = ["render_award_request", "render_commitment_email", "render_incident_report"]


def render_commitment_email(
    commitment: Commitment, report: CallReport, to_address: str, recording_url: str | None
) -> OutboundMessage:
    raise NotImplementedError("Track D: implement app/notify/render.py")


def render_award_request(comparison: Comparison, to_address: str) -> OutboundMessage:
    raise NotImplementedError("Track D: implement app/notify/render.py")


def render_incident_report(
    report: CallReport, to_address: str, *, whatsapp: bool = False
) -> OutboundMessage:
    raise NotImplementedError("Track D: implement app/notify/render.py")
