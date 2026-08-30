"""The three things Volta writes.

  * the official commitment email -- the confirmation brief plus the commitment register:
    every agreed term with the audio timestamp at which it was agreed, and a recording link;
  * the award approval request -- the whole ranked comparison, losers and reason codes
    included;
  * the incident report -- an inbound call or a missed deadline, with a short WhatsApp
    variant.

"""

from app.domain import (
    CallReport,
    Commitment,
    Comparison,
    NotificationChannel,
    OutboundMessage,
)

__all__ = [
    "render_award_request",
    "render_commitment_email",
    "render_incident_report",
    "render_not_selected_email",
]


def render_not_selected_email(
    *, order_id: str, reference: str, carrier_name: str, to_address: str
) -> OutboundMessage:
    return OutboundMessage(
        channel=NotificationChannel.EMAIL,
        to_address=to_address,
        subject=f"Quotation update — {reference}",
        body=(
            f"Hello {carrier_name},\n\nThank you for quoting transport for {reference}. "
            "Your company was not selected for this movement. We appreciate the time and "
            "expect to work with you on future opportunities.\n\nVolta transport coordination"
        ),
        order_id=order_id,
    )


def render_commitment_email(
    commitment: Commitment, report: CallReport, to_address: str, recording_url: str | None
) -> OutboundMessage:
    anchor = _timestamp(commitment.evidence_anchor_ms)
    terms = "\n".join(
        f"- {label.replace('_', ' ').title()}: {value} (audio {anchor})"
        for label, value in sorted(commitment.terms.items())
    )
    recording = recording_url or "Recording link unavailable"
    body = f"""Official transport commitment

Confirmation brief
{report.summary}

Commitment register
{terms or f"- Terms recorded at audio {anchor}"}

Evidence
Call: {commitment.evidence_call_id}
Recording: {recording}

Please reply immediately if any written term differs from what was confirmed."""
    return OutboundMessage(
        channel=NotificationChannel.EMAIL,
        to_address=to_address,
        subject=f"Official transport commitment — order {commitment.order_id}",
        body=body,
        order_id=commitment.order_id,
        call_id=commitment.evidence_call_id,
        commitment_id=commitment.id,
    )


def render_award_request(comparison: Comparison, to_address: str) -> OutboundMessage:
    rows = []
    for position, entry in enumerate(comparison.entries, start=1):
        marker = " — selected candidate" if entry.is_winner else ""
        rows.append(
            f"{position}. {entry.carrier_name}: {entry.amount.spoken()}, "
            f"pickup {entry.pickup_at.isoformat()}, {entry.outcome}/{entry.reason_code}{marker}"
        )
    winner = comparison.winner_quote_id or "No eligible candidate"
    body = f"""Award approval required for order {comparison.order_id}

Ranked comparison
{chr(10).join(rows) or "No quotes available"}

Proposed winning quote: {winner}
Mandate version evaluated: {comparison.mandate_version}
Comparison built: {comparison.built_at.isoformat()}

Review and approve or reject this exact option in the portal."""
    return OutboundMessage(
        channel=NotificationChannel.EMAIL,
        to_address=to_address,
        subject=f"Award approval required — order {comparison.order_id}",
        body=body,
        order_id=comparison.order_id,
    )


def render_incident_report(
    report: CallReport, to_address: str, *, whatsapp: bool = False
) -> OutboundMessage:
    channel = NotificationChannel.WHATSAPP if whatsapp else NotificationChannel.EMAIL
    if whatsapp:
        body = (
            f"Volta alert [{report.severity.value.upper()}] {report.subject.value}: "
            f"{report.summary} Call {report.call_id}. Review the incident in the portal."
        )
        subject = None
    else:
        actions = "\n".join(f"- {_evidence_text(item)}" for item in report.actions)
        objections = "\n".join(f"- {item}" for item in report.objections)
        conditions = "\n".join(f"- {item}" for item in report.conditions)
        body = f"""Incident report

Subject: {report.subject.value}
Severity: {report.severity.value}
Call: {report.call_id}

Summary
{report.summary}

Actions
{actions or "- None reported"}

Objections
{objections or "- None reported"}

Conditions
{conditions or "- None reported"}"""
        subject = f"{report.severity.value.title()} incident — {report.subject.value}"
    return OutboundMessage(
        channel=channel,
        to_address=to_address,
        subject=subject,
        body=body,
        call_id=report.call_id,
    )


def _timestamp(offset_ms: int) -> str:
    minutes, remainder = divmod(offset_ms, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _evidence_text(item: dict[str, object]) -> str:
    text = str(item.get("text", item))
    offset = item.get("offset_ms")
    return f"{text} (audio {_timestamp(offset)})" if isinstance(offset, int) else text
