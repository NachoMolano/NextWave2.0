"""The JSON shapes the dashboard binds to.

MAY IMPORT:  domain.
IMPORTED BY: api.

Domain models are already Pydantic and already frozen, so the reads hand them back directly
rather than restating forty fields in a parallel hierarchy that can drift. What lives here is
only the shapes that do not exist in ``domain/``: the request bodies a human sends, and the
two projections the portal needs that no single table holds -- the mandate as one object, and
the demurrage countdown that makes everything downstream urgent.

OWNER: Track C.
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.api.nextaction import NextAction
from app.domain import (
    Approval,
    CallRecord,
    CallReport,
    Carrier,
    Commitment,
    CommitmentMode,
    Money,
    Order,
    OrderStatus,
    QuoteRow,
)

__all__ = [
    "ApprovalDecisionRequest",
    "BusinessProfile",
    "BusinessProfileUpdate",
    "CallDetail",
    "ConfirmIntakeRequest",
    "DemurrageView",
    "MandateView",
    "NewOrderRequest",
    "NextAction",
    "OrderAggregate",
    "OrderSummary",
    "SecurityModeRequest",
    "Session",
    "SetMandateRequest",
    "SweepResult",
]


# ------------------------------------------------------------------------- what a human sends


class NewOrderRequest(BaseModel):
    """A cargo was received at port. Idempotent on ``reference``."""

    reference: str = Field(min_length=1)
    origin: str | None = None
    destination: str | None = None
    cargo: str | None = None
    equipment: str | None = None
    weight: str | None = None
    container_number: str | None = None
    discharged_at: datetime | None = None
    free_days: int | None = Field(default=None, ge=0)
    last_free_day: date | None = None
    delivery_deadline: datetime | None = None
    expected_driver: str | None = None
    expected_plate: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ConfirmIntakeRequest(BaseModel):
    """Stage 1. A person confirms the container is real, released and ready to move.

    Everything except the confirmation itself is a gap-filler: intake is where the facts a
    person holds and the system never saw get written down. They are optional because a
    complete intake does not need them; the *gate* is ``released``, and the clock is checked
    when the mandate is granted rather than here, so an operator can record a release the
    moment they have it and come back with the cutoff.
    """

    released: bool = Field(
        description="True confirms the container may move. False records a hold and clears "
        "the release, which is what stops the market if something changed."
    )
    note: str | None = Field(
        default=None, max_length=500, description="Why it is held, or what was checked."
    )
    # The gaps intake exists to close.
    discharged_at: datetime | None = None
    free_days: int | None = Field(default=None, ge=0)
    last_free_day: date | None = None
    delivery_deadline: datetime | None = None
    weight: str | None = None
    container_number: str | None = None
    equipment: str | None = None


class SetMandateRequest(BaseModel):
    """Step 4. The only shape in the system that can raise a price ceiling.

    ``expected_version`` makes a stale dashboard write fail instead of silently replacing a
    newer mandate. The audit actor comes from server configuration.
    """

    cap_amount_cents: int = Field(gt=0)
    cap_currency: str = Field(min_length=3, max_length=3)
    target_amount_cents: int | None = Field(default=None, gt=0)
    pickup_not_before: datetime
    pickup_not_after: datetime
    delivery_deadline: datetime | None = None
    commitment_mode: CommitmentMode = CommitmentMode.HUMAN_ESCALATION
    expected_version: int = Field(ge=0)


class ApprovalDecisionRequest(BaseModel):
    """Steps 9 and 10. ``approved`` on an award is what releases the award call."""

    status: str = Field(pattern="^(approved|rejected|handled|expired)$")
    note: str | None = None
    quote_id: str | None = Field(
        default=None,
        description="Award this carrier instead of the ranked winner. The choice is still "
        "evaluated under current policy: an operator may pick among the options policy "
        "allows, never around them.",
    )


# --------------------------------------------------------------------------- what it reads back


class MandateView(BaseModel):
    """The mandate as one object, rather than eight columns the caller has to reassemble."""

    model_config = ConfigDict(frozen=True)

    version: int
    cap: Money | None
    target: Money | None
    pickup_not_before: datetime | None
    pickup_not_after: datetime | None
    commitment_mode: CommitmentMode
    set_by: str | None
    set_at: datetime | None
    is_granted: bool = Field(
        description="False means nothing is authorized. Not 'no limit' -- no mandate."
    )

    @classmethod
    def of(cls, order: Order) -> "MandateView":
        return cls(
            version=order.mandate_version,
            cap=order.cap,
            target=order.target,
            pickup_not_before=order.pickup_not_before,
            pickup_not_after=order.pickup_not_after,
            commitment_mode=order.commitment_mode,
            set_by=order.mandate_set_by,
            set_at=order.mandate_set_at,
            is_granted=order.has_mandate,
        )


class DemurrageView(BaseModel):
    """The countdown. Nobody decides it: discharge starts it and the clock runs.

    This is why the portal is urgent rather than informational, so it is computed on the way
    out instead of being stored and going stale.
    """

    model_config = ConfigDict(frozen=True)

    discharged_at: datetime | None
    free_days: int | None
    last_free_day: date | None
    days_remaining: int | None
    is_overdue: bool

    @classmethod
    def of(cls, order: Order, today: date) -> "DemurrageView":
        remaining = (order.last_free_day - today).days if order.last_free_day else None
        return cls(
            discharged_at=order.discharged_at,
            free_days=order.free_days,
            last_free_day=order.last_free_day,
            days_remaining=remaining,
            is_overdue=remaining is not None and remaining < 0,
        )


class OrderSummary(BaseModel):
    """One row of the queue. Enough to triage without opening anything."""

    model_config = ConfigDict(frozen=True)

    id: str
    reference: str
    status: OrderStatus
    origin: str | None
    destination: str | None
    container_number: str | None
    demurrage: DemurrageView
    mandate: MandateView
    open_approvals: int
    next_action: NextAction

    @classmethod
    def of(
        cls, order: Order, today: date, open_approvals: int, action: NextAction
    ) -> "OrderSummary":
        return cls(
            id=str(order.id),
            reference=order.reference,
            status=order.status,
            origin=order.origin,
            destination=order.destination,
            container_number=order.container_number,
            demurrage=DemurrageView.of(order, today),
            mandate=MandateView.of(order),
            open_approvals=open_approvals,
            next_action=action,
        )


class OrderAggregate(BaseModel):
    """Everything the portal needs about one order, in one call.

    One request rather than six because the screen is a decision surface: a human approving an
    award should not be looking at a page that is still filling in.
    """

    model_config = ConfigDict(frozen=True)

    order: Order
    mandate: MandateView
    demurrage: DemurrageView
    next_action: NextAction
    quotes: list[QuoteRow]
    calls: list[CallRecord]
    commitment: Commitment | None
    approvals: list[Approval]
    #: Every carrier named anywhere on this screen, so a quote, a call and an escalation can
    #: each say who they are about. Quotes carry a ``carrier_id`` and nothing else, and a
    #: column of prices with no names is not a comparison -- it is four numbers.
    carriers: list[Carrier]


class CallDetail(BaseModel):
    """A call with what a model made of it. The brief, the transcript, the anchors."""

    model_config = ConfigDict(frozen=True)

    call: CallRecord
    report: CallReport | None
    carrier: Carrier | None


class SweepResult(BaseModel):
    """What the demo button did. Empty on the second press, which is the point."""

    model_config = ConfigDict(frozen=True)

    call_ids: list[str]


# --------------------------------------------------------------------------- the business


class BusinessProfile(BaseModel):
    """Who Volta works for, and where the cargo goes.

    Read from the database rather than the environment. The prompt fields are configuration
    and could have stayed in `config.py`; the warehouse could not. It has a street address, a
    contact and opening hours, it changes because the business changed and not because
    somebody redeployed, and the person who knows it is an operator with a browser.
    """

    model_config = ConfigDict(frozen=True)

    display_name: str
    legal_name: str | None = None
    business_type: str = "importer"
    city: str | None = None
    country: str | None = None
    currency: str = "USD"
    timezone: str = "America/Mexico_City"
    business_hours: str | None = None

    agent_name: str = "Volta"
    agent_role: str = "transport coordinator"
    primary_language: str = "en"
    fallback_language: str = "es-MX"

    warehouse_name: str | None = None
    warehouse_address: str | None = None
    warehouse_city: str | None = None
    warehouse_state: str | None = None
    warehouse_postal_code: str | None = None
    warehouse_country: str | None = None
    warehouse_contact_name: str | None = None
    warehouse_phone: str | None = None
    warehouse_hours: str | None = None
    warehouse_notes: str | None = None

    updated_at: datetime | None = None
    updated_by: str | None = Field(
        default=None,
        description="Who last said this was true. A fact the agent reads out loud on a call "
        "should carry a name, for the same reason a mandate does.",
    )


class BusinessProfileUpdate(BaseModel):
    """What the settings form sends. Every field optional: a form edits what it edits."""

    display_name: str | None = Field(default=None, min_length=1)
    legal_name: str | None = None
    business_type: str | None = None
    city: str | None = None
    country: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    timezone: str | None = None
    business_hours: str | None = None

    agent_name: str | None = Field(default=None, min_length=1)
    agent_role: str | None = None
    primary_language: str | None = None
    fallback_language: str | None = None

    warehouse_name: str | None = None
    warehouse_address: str | None = None
    warehouse_city: str | None = None
    warehouse_state: str | None = None
    warehouse_postal_code: str | None = None
    warehouse_country: str | None = None
    warehouse_contact_name: str | None = None
    warehouse_phone: str | None = None
    warehouse_hours: str | None = None
    warehouse_notes: str | None = None

    # No `updated_by` here on purpose. It comes from server configuration, not from the form.
    # The mandate and approval paths use the same deployment-level audit actor.


class Session(BaseModel):
    """The deployment identity recorded for portal actions.

    Exposed so the portal can say *acting as X* rather than leaving an operator to guess whose
    name their next approval will carry.
    """

    model_config = ConfigDict(frozen=True)

    actor: str = Field(description="Recorded against every mandate, award and profile change.")
    #: How many carriers an RFQ dials. Exposed so the portal's authorize button can name the
    #: number it is about to ring rather than saying "some carriers" or hardcoding a 3 that
    #: drifts from `settings.rfq_carrier_count` the day somebody changes it.
    rfq_carrier_count: int = Field(
        ge=0,
        description="Carriers dialled when the market opens; zero means every eligible carrier.",
    )
    strict_conversation_security: bool = False


class SecurityModeRequest(BaseModel):
    enabled: bool


class EmailTestRequest(BaseModel):
    """One local-only, user-authored Resend smoke test."""

    to_address: str = Field(min_length=3, max_length=320)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20_000)
