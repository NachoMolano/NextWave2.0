"""The Decision Trace: the ledger, read back as what happened.

MAY IMPORT:  domain.
IMPORTED BY: api.

A projection, and nothing more. It writes nothing, decides nothing, and adds no fact that is
not already in the append-only record. If a row appears here, a row exists in ``decisions``,
``events``, ``quotes``, ``approvals``, ``commitments`` or the stored transcript, and clicking
its provenance is meant to land on that row.

**Where each row comes from matters, because it is the whole claim.** The conversational rows
come from the vendor -- Vapi's transcript, which is a model's rendering of speech and is
untrusted. Everything with an outcome attached comes from tables we wrote ourselves at the
moment we wrote them: a POLICY row is a `decisions` row, cap copied by value; a TOOL row is an
`events` row keyed for idempotency. The trace can therefore show a refusal without asking the
model what it did, which is the difference between an audit trail and a chat log.

Not every utterance earns a row. Routine dialogue is provenance, not evidence -- the rows are
the moments where terms, decisions or state changed, and a screen that lists every "sí, claro"
buries exactly the row a judge came to find.

OWNER: Track C.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain import (
    Approval,
    CallRecord,
    Commitment,
    DecisionRow,
    EventRow,
    Money,
    QuoteRow,
)

__all__ = ["Category", "Result", "TraceRow", "build_trace"]

Category = Literal["conversation", "quote", "policy", "decision", "tool", "action"]

Result = Literal[
    "continue",
    "proposed",
    "allowed",
    "denied",
    "clarify",
    "escalate",
    "authorized",
    "in_progress",
    "completed",
    "failed",
    "not_executed",
    "unknown",
]

#: policy outcome -> what the row shows. `escalate` is not a failure: it is the system
#: choosing a person, which is the behaviour the mandate is there to produce.
_OUTCOME: dict[str, Result] = {"allow": "allowed", "deny": "denied", "escalate": "escalate"}

#: Ledger event type -> (category, result, what Volta did). The counterparty half is filled
#: from the payload where the event carries one, because most events record our own action.
_EVENTS: dict[str, tuple[Category, Result, str]] = {
    "preagreement.confirmed": ("tool", "authorized", "Confirmed the exact recap on the line."),
    "identity.attempt": ("tool", "unknown", "Checked the caller's claim server-side."),
    "commitment.verbal": ("action", "in_progress", "Opened a verbal commitment with its anchor."),
    "commitment.committed": ("action", "completed", "Promoted the commitment; the recap landed."),
    "commitment.recap_failed": ("action", "failed", "Left the commitment unpromoted."),
    "commitment.superseded": (
        "action",
        "completed",
        "Replaced the commitment; the old row stands.",
    ),
    "award.approval_requested": ("decision", "escalate", "Sent the comparison to a person."),
    "award.accepted": ("action", "completed", "Accepted the winning quote."),
    "award.conflict": ("action", "failed", "Refused a second award on this order."),
    "chase.started": ("action", "in_progress", "Called the carrier about the missed deadline."),
    "rfq.planned": ("action", "in_progress", "Opened the market and dialled carriers."),
    "rfq.timed_out": ("decision", "escalate", "Closed the market on timeout and ranked it."),
    "call.anchor_unmeasurable": ("action", "unknown", "Could not measure an audio offset."),
    "mandate.set": ("action", "authorized", "Recorded a mandate a person granted."),
    "order.received": ("action", "completed", "Registered the cargo."),
}


class TraceRow(BaseModel):
    """One line of the story. One short sentence per side, at most."""

    model_config = ConfigDict(frozen=True)

    at: datetime = Field(description="Absolute time. Shown on hover; the row displays offset.")
    offset_ms: int | None = Field(
        default=None,
        description="Call-relative. None when the vendor gave no offset for this moment, "
        "which is itself worth seeing rather than papering over with a zero.",
    )
    category: Category
    counterparty: str = Field(description="What the caller or called party said or did.")
    volta: str = Field(description="What the agent or the server decided or did.")
    result: Result
    reason_code: str | None = Field(
        default=None, description="OUTSIDE_MANDATE and friends. Shown as a secondary link."
    )
    provenance: str | None = Field(
        default=None, description="Where this row came from: a transcript offset, a tool, a table."
    )


def _clip(text: str, limit: int = 80) -> str:
    """One short sentence. Long enough to be a fact, short enough to scan a column of them."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip(" ,.;:") + "…"


def _offset(at: datetime, started_at: datetime | None) -> int | None:
    if started_at is None:
        return None
    delta = int((at - started_at).total_seconds() * 1000)
    return delta if delta >= 0 else None


def _clock(offset_ms: int | None) -> str:
    if offset_ms is None:
        return "--:--"
    total = offset_ms // 1000
    return f"{total // 60:02d}:{total % 60:02d}"


def _money(cents: int | None, currency: str | None) -> str:
    if cents is None or not currency:
        return "an amount"
    return Money(cents=cents, currency=currency).spoken()


def build_trace(
    call: CallRecord,
    *,
    quotes: list[QuoteRow],
    decisions: list[DecisionRow],
    events: list[EventRow],
    approvals: list[Approval],
    commitments: list[Commitment],
) -> list[TraceRow]:
    """Merge the ledger into one ordered story, oldest first."""
    started = call.started_at
    rows: list[TraceRow] = []

    # --- the call itself ------------------------------------------------------------
    if started is not None:
        answered = "Answered the outbound call." if call.direction == "outbound" else "Called in."
        opened = (
            "Introduced Pacific Textiles and opened the phase."
            if call.direction == "outbound"
            else "Answered without giving anything away."
        )
        rows.append(
            TraceRow(
                at=started,
                offset_ms=0,
                category="conversation",
                counterparty=answered,
                volta=opened,
                result="continue",
                provenance=f"Call {call.vapi_call_id[:12]}",
            )
        )

    # --- what the counterparty put on the table -------------------------------------
    for quote in quotes:
        if quote.call_id != call.id:
            continue
        at = quote.confirmed_at or started
        if at is None:
            continue
        final = "all-in" if quote.cost_is_final else "not confirmed final"
        rows.append(
            TraceRow(
                at=at,
                offset_ms=quote.anchor_ms,
                category="quote",
                counterparty=_clip(f"Quoted {quote.amount.spoken()} {final}."),
                volta=_clip("Extracted amount, currency, date and equipment."),
                result="proposed",
                provenance=f"Transcript {_clock(quote.anchor_ms)}",
            )
        )

    # --- what policy made of it -----------------------------------------------------
    # The authoritative half. The cap is the one copied into the decision, not today's, so
    # a row still explains itself after somebody raises the ceiling.
    by_quote = {quote.id: quote for quote in quotes if quote.id}
    for decision in decisions:
        cap = _money(decision.cap_at_decision_cents, decision.cap_currency)
        result = _OUTCOME.get(decision.outcome, "unknown")
        # What the counterparty did comes from the quote this decision judged, or it is left
        # blank. Writing "pressed for a commitment" because it reads well would be putting
        # words in their mouth, and an audit trail that narrates is worth less than one that
        # is short: the whole value of the row is that it is checkable.
        judged = by_quote.get(decision.quote_id) if decision.quote_id else None
        said = f"Quoted {judged.amount.spoken()}." if judged else "—"
        rows.append(
            TraceRow(
                at=decision.decided_at,
                offset_ms=_offset(decision.decided_at, started),
                category="policy",
                counterparty=_clip(said),
                volta=_clip(f"Compared the proposal with the {cap} mandate cap."),
                result=result,
                reason_code=decision.reason_code.upper(),
                # The reason code is already shown above it; pointing at the decisions row
                # is the useful other half, not a repeat of the word "policy".
                provenance=f"Decision {decision.id[:8]}" if decision.id else "decisions",
            )
        )

    # --- tools and observable actions -----------------------------------------------
    for event in events:
        mapped = _EVENTS.get(event.type)
        if mapped is None:
            continue
        category, result, volta = mapped
        rows.append(
            TraceRow(
                at=event.created_at or started or datetime.now().astimezone(),
                offset_ms=_offset(event.created_at, started) if event.created_at else None,
                category=category,
                counterparty="—",
                volta=_clip(volta),
                result=result,
                provenance=f"Tool: {event.type}",
            )
        )

    # --- where a person was asked ---------------------------------------------------
    for approval in approvals:
        if approval.raised_at is None:
            continue
        rows.append(
            TraceRow(
                at=approval.raised_at,
                offset_ms=_offset(approval.raised_at, started),
                category="decision",
                counterparty="—",
                volta=_clip("Refused to commit and selected human escalation."),
                result="escalate",
                reason_code=str(approval.reason).upper(),
                provenance=f"Approval: {approval.kind}",
            )
        )

    # --- what stands at the end -----------------------------------------------------
    for commitment in commitments:
        if commitment.evidence_call_id != call.id or commitment.created_at is None:
            continue
        booked = commitment.state in ("committed", "executed")
        rows.append(
            TraceRow(
                at=commitment.created_at,
                offset_ms=commitment.evidence_anchor_ms,
                category="action",
                counterparty="—",
                volta=_clip(
                    "Recorded the commitment against its audio offset."
                    if booked
                    else "Held the commitment unpromoted until the recap lands."
                ),
                result="completed" if booked else "in_progress",
                provenance=f"Transcript {_clock(commitment.evidence_anchor_ms)}",
            )
        )

    if call.ended_at is not None:
        rows.append(
            TraceRow(
                at=call.ended_at,
                offset_ms=_offset(call.ended_at, started),
                category="action",
                counterparty=_clip("Ended the call."),
                volta=_clip(f"Closed the call: {call.ended_reason or 'no reason reported'}."),
                result="completed" if call.status == "ended" else "failed",
                provenance=f"Call {call.vapi_call_id[:12]}",
            )
        )

    def _order(row: TraceRow) -> tuple[int, int, datetime]:
        # Sort by what the row displays. Sorting by absolute time while displaying a
        # call-relative one lets the two disagree on screen, and a trace whose order argues
        # with its own timestamps is worse than no trace.
        derived = row.offset_ms if row.offset_ms is not None else _offset(row.at, started)
        return (0, derived, row.at) if derived is not None else (1, 0, row.at)

    return sorted(rows, key=_order)
