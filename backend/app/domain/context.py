"""What this one call is about. Assembled once, before the phone rings.

Nothing here is re-injected mid-conversation. The prompt is composed at call setup and
then stays fixed for the life of the call, which is what makes a transcript replayable:
the same context and the same audio produce the same reasoning.

It lives in domain/ rather than agent/ because three packages share it: tools/ builds one
per call, agent/ renders it into a prompt, and vapi/ stores it on the call row. Under the
layering contract tools/ may not import agent/, so a shared type cannot live there.

These are *rendering inputs*, not the domain model. tools/ maps from Order and Mandate into
this — deliberately, so that adding a field to the mandate does not silently change what the
agent says.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CallContext", "CallPhase", "spoken_window"]


class CallPhase(StrEnum):
    """Which conversation this is. Set by market/, never inferred by the model.

    RFQ and AWARD are separate for the reason in AGENTS.md invariant #5: several carriers
    may hold confirmed offers at once, but only one call may close. A phase the model
    could talk itself into is not a phase.
    """

    RFQ = "rfq"
    AWARD = "award"
    RENEGOTIATION = "renegotiation"
    INBOUND = "inbound"
    STATUS_CHECK = "status_check"


class CallContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    phase: CallPhase
    today: str = Field(
        description=(
            "Today's calendar date, already written out, e.g. 'Friday, 29 August 2026'. "
            "The agent needs it to resolve 'Thursday' into a date it can read back."
        )
    )

    # The shipment. Every field optional because an inbound call starts before we know
    # which operation it is about — that is exactly what the agent has to establish.
    reference: str | None = None
    # Written the way they are spoken after "from ... to ...". Avoid a leading article in
    # Spanish: "de el puerto de Manzanillo" is what a leading "el" produces, and the
    # greeting is the first thing a dispatcher hears.
    origin: str | None = None
    destination: str | None = None
    cargo: str | None = None
    equipment: str | None = None
    weight: str | None = None
    pickup_window: str | None = None

    counterparty_name: str | None = Field(default=None, description="Carrier company being called.")
    counterparty_contact: str | None = Field(
        default=None, description="Dispatcher's name, if known."
    )

    # The mandate figures. They are in the prompt so the agent can negotiate with judgement
    # instead of relaying every number to policy — a deliberate trade, logged in
    # DECISION_LOG. They are also extractable by a persistent counterparty, which is why
    # the prompt forbids saying them and why policy/ still decides, every time.
    price_ceiling: Decimal | None = None
    target_price: Decimal | None = None

    # Market state at the moment the call was placed. Shapes how hard the agent pushes:
    # the first call of an RFQ has nothing to compare against, the fifth has four numbers.
    quotes_in_hand: int = 0
    best_rate_so_far: Decimal | None = None

    # Renegotiation only: what already stands, and what we are asking to move.
    agreed_terms: str | None = None
    change_requested: str | None = None

    # Status check only: the deadline that passed, spoken as a date the agent can read back.
    missed_deadline: str | None = None

    # Inbound only: what we expect a legitimate caller to be able to tell us. The agent
    # checks answers against these and never reads them out — a caller who is told the
    # plate number can repeat it back, which verifies nothing.
    expected_driver: str | None = None
    expected_plate: str | None = None
    expected_carrier: str | None = None


def spoken_window(not_before: datetime | None, not_after: datetime | None) -> str | None:
    """The mandate's pickup window as a person says it, or None when there is not one.

    Here rather than in ``agent/`` because ``tools/`` builds the context and may not import
    ``agent/``, and both paths have to produce the same sentence: ``agent/prompts.py``
    recognises exactly this grammar when it shortens the window into a one-breath answer.

    An ISO timestamp is not a spoken date. "2026-09-02T08:00:00+00:00" read out loud is
    what a dispatcher hangs up on, and a window nobody can say is a window the agent ends
    up asking the carrier to supply -- which inverts who is buying.
    """
    if not_before is None or not_after is None:
        return None
    if not_before.date() == not_after.date():
        return f"on {not_before:%B} {not_before.day}, {not_before:%Y}"
    return (
        f"between {not_before:%B} {not_before.day} and "
        f"{not_after:%B} {not_after.day}, {not_after:%Y}"
    )
