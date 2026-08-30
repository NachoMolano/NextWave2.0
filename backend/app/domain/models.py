"""The operational vocabulary: the shipment, the carrier, the call, and what came out of it.

MAY IMPORT:  stdlib, pydantic. Nothing from app except domain.security.
IMPORTED BY: every package. This is the leaf.

Money is always a whole number of cents plus an explicit ISO 4217 code. Never a float, and
never a bare amount: an amount without a currency is an error waiting for a phone call.
Rounding has to be a decision somebody made, not a consequence of binary fractions.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.security import CommitmentMode, Mandate

__all__ = [
    "COMMITMENT_DEAD_STATES",
    "DELIVERY_UNDERWAY",
    "Approval",
    "ApprovalKind",
    "ApprovalReason",
    "ApprovalStatus",
    "AwardConflict",
    "CallDirection",
    "CallRecord",
    "CallReport",
    "CallStatus",
    "Carrier",
    "Commitment",
    "CommitmentState",
    "Comparison",
    "ComparisonEntry",
    "DecisionRow",
    "DeliveryResult",
    "DeliveryStatus",
    "DialPlan",
    "EventRow",
    "IncidentSubject",
    "Money",
    "NotificationChannel",
    "Order",
    "OrderStatus",
    "OutboundMessage",
    "QuoteRow",
    "QuoteStatus",
    "Severity",
    "Turn",
]


class AwardConflict(Exception):
    """Raised when a second award is attempted on an order that already has one.

    The database is the enforcement -- a partial unique index on
    ``quotes (order_id) where status = 'accepted'``. This exception exists so callers see a
    typed failure instead of a raw Postgres error, and so ``store/`` stays the only package
    that knows Postgres exists.
    """


# --------------------------------------------------------------------------------- money


class Money(BaseModel):
    """A whole number of cents and the currency it is denominated in. Never separated."""

    model_config = ConfigDict(frozen=True)

    cents: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def canonical_currency(cls, value: str) -> str:
        if not value.isalpha():
            raise ValueError("currency must be an ISO 4217 alphabetic code")
        return value.upper()

    @property
    def amount(self) -> Decimal:
        """The value in major units. Exact, because cents are integers."""
        return Decimal(self.cents) / Decimal(100)

    def spoken(self) -> str:
        """How a person says it out loud. The agent always says the currency."""
        return f"{self.amount:,.2f} {self.currency}"


# ----------------------------------------------------------------------------- the world


class OrderStatus(StrEnum):
    RECEIVED = "received"
    QUOTING = "quoting"
    AWAITING_APPROVAL = "awaiting_approval"
    AWARDING = "awarding"
    BOOKED = "booked"
    IN_TRANSIT = "in_transit"
    AT_RISK = "at_risk"
    DELIVERED = "delivered"
    CLOSED = "closed"
    CANCELLED = "cancelled"


#: Statuses meaning the cargo is moving or has arrived, so the deadline sweep leaves it
#: alone. OUTBOUND 2 fires on everything else once ``delivery_deadline`` has passed.
DELIVERY_UNDERWAY: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.IN_TRANSIT,
        OrderStatus.DELIVERED,
        OrderStatus.CLOSED,
        OrderStatus.CANCELLED,
    }
)


class Carrier(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str = Field(min_length=1, description="What the agent pronounces on the phone.")
    phone: str = Field(description="E.164. Its unique index is how an inbound call is matched.")
    contact_name: str | None = None
    email: str | None = None
    whatsapp: str | None = None
    is_on_file: bool = Field(
        default=True,
        description="False means the agent declines to quote. Volta onboards nobody by phone.",
    )
    is_active: bool = True
    persona: str | None = Field(
        default=None,
        description="Seed-only colour: 'cheap and slow', 'never answers'. Never spoken.",
    )


class Order(BaseModel):
    """The shipment, its mandate and its clocks, in one record.

    The mandate is columns rather than its own table because there is exactly one per order.
    What makes that safe is ``DecisionRow.cap_at_decision_cents``: every evaluation copies
    the ceiling by value, so raising the cap later cannot rewrite an earlier explanation.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    reference: str = Field(
        min_length=1,
        description="The folio. Also identity proof level 1: only a real counterparty knows it.",
    )
    status: OrderStatus = OrderStatus.RECEIVED

    # The load, in the words a dispatcher would use. Rendered into the prompt verbatim.
    origin: str | None = None
    destination: str | None = None
    cargo: str | None = None
    equipment: str | None = None
    weight: str | None = None
    container_number: str | None = None

    # --- intake: is this real, and may it move? ---
    #: When a *person* confirmed the container is released and available to move. Until it is
    #: set, no mandate may be granted and no carrier may be dialled. Never inferred from
    #: discharge and never taken from a carrier's claim: the terminal releasing a box and
    #: somebody checking that it did are different facts, and only the second is evidence.
    released_at: datetime | None = None
    released_by: str | None = None
    release_note: str | None = None

    # Demurrage. Nobody decides when it starts; discharge starts it.
    discharged_at: datetime | None = None
    free_days: int | None = None
    last_free_day: date | None = None

    #: The OUTBOUND 2 trigger. Past this, with nothing underway, the agent calls to ask why.
    delivery_deadline: datetime | None = None

    # --- the mandate ---
    cap: Money | None = None
    target: Money | None = None
    pickup_not_before: datetime | None = None
    pickup_not_after: datetime | None = None
    commitment_mode: CommitmentMode = CommitmentMode.HUMAN_ESCALATION
    mandate_version: int = Field(default=0, ge=0, description="0 means no mandate has been set.")
    mandate_set_by: str | None = None
    mandate_set_at: datetime | None = None

    assigned_carrier_id: str | None = None
    awarded_quote_id: str | None = None

    # What a legitimate inbound caller can tell us. Checked against, never read out.
    expected_driver: str | None = None
    expected_plate: str | None = None

    payload: dict[str, object] = Field(default_factory=dict)

    @property
    def has_mandate(self) -> bool:
        return self.mandate_version > 0 and self.cap is not None

    @property
    def is_released(self) -> bool:
        """Has a person confirmed this container may move?

        The gate the whole intake stage exists to hold. Kept as a property rather than read
        as a column at each call site so there is one definition of released, and so a future
        release that needs two conditions changes here and nowhere else.
        """
        return self.released_at is not None

    @property
    def has_clock(self) -> bool:
        """Is there a deadline that makes urgency real?

        ``last_free_day`` is the import clock (demurrage starts at discharge and nobody
        chooses when); ``delivery_deadline`` carries the export one (the cargo cutoff at the
        port). One or the other must exist before a mandate is granted: a ceiling with no
        clock is an authority to negotiate with no reason to hurry, and the agent's strongest
        honest lever -- "I can move the pickup if you can move the rate" -- is exactly the
        days of free time it does not have.
        """
        return self.last_free_day is not None or self.delivery_deadline is not None

    def mandate(self) -> Mandate:
        """Project the mandate columns into the value ``policy/`` evaluates against.

        Raises rather than substituting a default. A missing ceiling is not "no limit"; it
        is a mandate that was never granted, and nothing may be authorized under it.
        """
        if self.cap is None or self.pickup_not_before is None or self.pickup_not_after is None:
            raise ValueError(f"order {self.reference} has no mandate: nothing is authorized")
        if not self.equipment:
            raise ValueError(f"order {self.reference} has no equipment: nothing is authorized")
        return Mandate(
            mandate_id=f"{self.id}:v{self.mandate_version}",
            version=max(self.mandate_version, 1),
            owner_id=self.mandate_set_by or "unknown",
            operation_id=self.id,
            max_all_in_usd=self.cap.amount,
            pickup_not_before=self.pickup_not_before,
            pickup_not_after=self.pickup_not_after,
            allowed_equipment=frozenset({self.equipment}),
            commitment_mode=self.commitment_mode,
        )


# --------------------------------------------------------------------------- during a call


class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(StrEnum):
    QUEUED = "queued"
    RINGING = "ringing"
    ACTIVE = "active"
    ENDED = "ended"
    FAILED = "failed"


class Turn(BaseModel):
    """One line of transcript, anchored to the recording."""

    model_config = ConfigDict(frozen=True)

    speaker: str = Field(description="'caller' or 'agent'.")
    text: str
    offset_ms: int | None = Field(
        default=None,
        ge=0,
        description="Milliseconds from the start of the recording. None when the vendor did "
        "not supply one -- which is why evidence never rests on this field alone.",
    )


class CallRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str | None = None
    vapi_call_id: str = Field(
        min_length=1,
        description="Unique. A redelivered webhook must not create a second call.",
    )
    direction: CallDirection
    phase: str = Field(description="A CallPhase value: rfq, award, renegotiation, inbound, ...")
    status: CallStatus = CallStatus.QUEUED
    order_id: str | None = Field(
        default=None, description="Null until an inbound call has been correlated."
    )
    carrier_id: str | None = Field(
        default=None,
        description="Null when the number is not on file -- which is already information.",
    )
    from_number: str | None = None
    to_number: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    ended_reason: str | None = None
    recording_url: str | None = None
    transcript: list[Turn] = Field(default_factory=list)
    context: dict[str, object] = Field(
        default_factory=dict,
        description="The exact CallContext the prompt was built from. Without it a call "
        "cannot be replayed, and an unreplayable call is not evidence.",
    )
    identity_verified: bool = False
    identity_level: int = Field(default=0, ge=0, le=3)
    cost_cents: int | None = None


class QuoteStatus(StrEnum):
    PROPOSED = "proposed"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    SELECTED = "selected"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class QuoteRow(BaseModel):
    """What a carrier said it would do, and for how much.

    A quote that changes is a **new row**. Overwriting deletes exactly the fact a judge will
    probe: they said 8,500, then they said 9,200, and both were said.
    """

    model_config = ConfigDict(frozen=True)

    id: str | None = None
    order_id: str
    carrier_id: str
    call_id: str
    anchor_ms: int = Field(
        ge=0, description="The moment of the recording this was said. Never null."
    )
    amount: Money
    components: list[dict[str, object]] = Field(default_factory=list)
    cost_is_final: bool = Field(
        default=False,
        description="False by default so silence blocks. 'Plus tolls' means the total is not "
        "final, and a total that is not final cannot be authorized.",
    )
    pickup_at: datetime
    pickup_window_end: datetime | None = None
    equipment: str
    valid_until: datetime
    all_in_usd_cents: int | None = None
    status: QuoteStatus = QuoteStatus.PROPOSED
    superseded_by: str | None = None
    carrier_confirmed_exact_recap: bool = False
    confirmed_at: datetime | None = None
    claimed_identity: str | None = Field(
        default=None, description="Who they *said* they were. Never trusted; always kept."
    )
    identity_level: int = Field(default=0, ge=0, le=3)


class DecisionRow(BaseModel):
    """One policy evaluation, including the refusals. Append-only.

    This is literally what you show a jury when the agent says no, which is why the ceiling
    is copied here by value: the decision has to stay explainable after the mandate changes.
    """

    model_config = ConfigDict(frozen=True)

    id: str | None = None
    order_id: str
    call_id: str | None = None
    quote_id: str | None = None
    proposal: dict[str, object] = Field(
        default_factory=dict, description="A copy of the input. Without it, not reproducible."
    )
    outcome: str
    reason_code: str
    cap_at_decision_cents: int | None = None
    cap_currency: str | None = None
    mandate_version: int = 0
    decided_at: datetime


class EventRow(BaseModel):
    """The append-only log every mutating path writes to. Idempotency lives here.

    ``insert ... on conflict (idempotency_key) do nothing`` is atomic, so a redelivered
    webhook has no window in which to slip a second write through.
    """

    model_config = ConfigDict(frozen=True)

    id: str | None = None
    order_id: str | None = None
    call_id: str | None = None
    type: str
    payload: dict[str, object] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1)
    created_at: datetime | None = None


# ---------------------------------------------------------------------------- after a call


class IncidentSubject(StrEnum):
    QUOTE = "quote"
    ACCIDENT = "accident"
    DELAY = "delay"
    REQUEST = "request"
    DELIVERED = "delivered"
    OTHER = "other"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CallReport(BaseModel):
    """What a model understood about a finished call. Evidence, never authorization.

    Separate from ``CallRecord`` because a different writer fills it at a different
    confidence level: the call row holds what the vendor reported, this holds what a model
    inferred.
    """

    model_config = ConfigDict(frozen=True)

    call_id: str
    summary: str
    subject: IncidentSubject = IncidentSubject.OTHER
    severity: Severity = Severity.LOW
    actions: list[dict[str, object]] = Field(default_factory=list)
    mentions: list[dict[str, object]] = Field(default_factory=list)
    quoted_prices: list[dict[str, object]] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    agreement_candidates: list[dict[str, object]] = Field(
        default_factory=list,
        description="Candidates only. The model proposes; policy decides whether one binds.",
    )
    model: str | None = None
    generated_at: datetime | None = None


class CommitmentState(StrEnum):
    VERBAL = "verbal"
    RECAP_SENT = "recap_sent"
    COMMITTED = "committed"
    SUPERSEDED = "superseded"
    NOT_COMMITTED = "not_committed"
    EXECUTED = "executed"


#: States that do not occupy the "one live commitment per order" slot.
COMMITMENT_DEAD_STATES: frozenset[CommitmentState] = frozenset(
    {CommitmentState.SUPERSEDED, CommitmentState.NOT_COMMITTED}
)


class Commitment(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str | None = None
    order_id: str
    quote_id: str
    state: CommitmentState = CommitmentState.VERBAL
    evidence_call_id: str
    evidence_anchor_ms: int = Field(
        ge=0,
        description="Not nullable, on purpose. If a commitment cannot exist without an audio "
        "offset, nothing has to police the absence of one.",
    )
    terms: dict[str, object] = Field(default_factory=dict)
    canonical_sha256: str | None = None
    claimed_identity: str | None = None
    identity_level: int = Field(default=0, ge=0, le=3)
    superseded_by: str | None = None
    approval_id: str | None = None
    created_at: datetime | None = None


class ApprovalKind(StrEnum):
    AWARD_APPROVAL = "award_approval"
    ESCALATION = "escalation"
    INCIDENT = "incident"


class ApprovalReason(StrEnum):
    AWARD_SELECTED = "award_selected"
    OUTSIDE_MANDATE = "outside_mandate"
    DIRECT_REQUEST = "direct_request"
    IDENTITY_UNVERIFIED = "identity_unverified"
    CONFLICTING_INFORMATION = "conflicting_information"
    POLICY_FAILURE = "policy_failure"
    DEADLINE_BREACH = "deadline_breach"
    CARRIER_REPORTED_INCIDENT = "carrier_reported_incident"
    NO_ELIGIBLE_CANDIDATE = "no_eligible_candidate"


class ApprovalStatus(StrEnum):
    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"
    HANDLED = "handled"
    EXPIRED = "expired"


class Approval(BaseModel):
    """One human inbox for the three things that all mean "a person must look at this".

    An award decision, a mid-call escalation and a deadline breach are the same request from
    the portal's point of view. Keeping them one table is what makes the dashboard one screen.
    """

    model_config = ConfigDict(frozen=True)

    id: str | None = None
    order_id: str | None = None
    call_id: str | None = None
    kind: ApprovalKind
    reason: ApprovalReason
    context: dict[str, object] = Field(
        default_factory=dict,
        description="For an award, the whole ranked comparison. For an escalation, enough for "
        "a human to take a live call without reading a transcript.",
    )
    status: ApprovalStatus = ApprovalStatus.OPEN
    raised_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    note: str | None = None


# ------------------------------------------------------------------------------ the outbox


class NotificationChannel(StrEnum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


class OutboundMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel: NotificationChannel
    to_address: str
    body: str
    subject: str | None = None
    order_id: str | None = None
    call_id: str | None = None
    commitment_id: str | None = None
    approval_id: str | None = None


class DeliveryResult(BaseModel):
    """The outcome of one send attempt.

    ``FAILED`` is a definite refusal before delivery. ``UNKNOWN`` means the provider may
    have accepted the message, so automatic retry is forbidden. A sender never raises.
    """

    model_config = ConfigDict(frozen=True)

    status: DeliveryStatus
    provider_message_id: str | None = None
    error: str | None = None
    sent_at: datetime | None = None


# ------------------------------------------------------------------------------ the market


class DialPlan(BaseModel):
    """One carrier to call, with its call row already created and its context frozen."""

    model_config = ConfigDict(frozen=True)

    call_id: str
    carrier: Carrier
    to_number: str
    context: dict[str, object]


class ComparisonEntry(BaseModel):
    """One carrier's position in the comparison -- including the ones that lost, and why.

    A comparison listing only the winner is not auditable. The reason code is the point.
    """

    model_config = ConfigDict(frozen=True)

    quote_id: str
    carrier_id: str
    carrier_name: str
    amount: Money
    all_in_usd_cents: int | None
    pickup_at: datetime
    equipment: str
    outcome: str
    reason_code: str
    is_winner: bool = False


class Comparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    entries: list[ComparisonEntry]
    winner_quote_id: str | None
    cap_at_decision_cents: int | None
    cap_currency: str | None
    mandate_version: int
    built_at: datetime
