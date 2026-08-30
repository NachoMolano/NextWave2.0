"""Where an operation has got to, and whose move it is.

MAY IMPORT:  domain.
IMPORTED BY: api.

A projection over the order, the same as ``trace.py``: it reads state and says what it means.
It decides nothing and writes nothing, and every field is derived from a column somebody else
wrote.

The question this answers is the one a person actually has when they open the portal: *is this
waiting on me, or is it working?* A status like ``quoting`` does not answer that. Six carriers
being dialled and a comparison sitting unapproved are both "in progress" to the schema and are
opposite things to a human -- one is the system doing its job and the other is the system
blocked on somebody who has not looked yet.

So every order carries an actor as well as a stage. ``volta`` means the machine is working and
nobody needs to do anything. ``operator`` means it has stopped and is waiting for a person,
which is the only state that should ever feel urgent.

OWNER: Track C.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain import Order, OrderStatus

__all__ = ["STAGES", "Actor", "NextAction", "Urgency", "next_action"]

Actor = Literal["operator", "volta", "nobody"]

#: `now` is reserved for things a person is blocking. Everything the machine is doing is
#: `waiting`, however busy it looks, because a progress bar is not a call to action.
Urgency = Literal["now", "soon", "waiting", "none"]

#: The operational story, in order. Shown as a strip so "where are we" is answered by
#: position rather than by reading a status word and knowing what it implies.
STAGES: tuple[str, ...] = (
    "Received",
    "Mandate",
    "Market",
    "Comparison",
    "Award",
    "In transit",
)

_STAGE_BY_STATUS: dict[OrderStatus, int] = {
    OrderStatus.RECEIVED: 0,
    OrderStatus.QUOTING: 2,
    OrderStatus.AWAITING_APPROVAL: 3,
    OrderStatus.AWARDING: 4,
    OrderStatus.BOOKED: 4,
    OrderStatus.IN_TRANSIT: 5,
    OrderStatus.AT_RISK: 5,
    OrderStatus.DELIVERED: 5,
    OrderStatus.CLOSED: 5,
    OrderStatus.CANCELLED: 5,
}


class NextAction(BaseModel):
    """The one thing to do next, and whether it is yours."""

    model_config = ConfigDict(frozen=True)

    stage: str
    stage_index: int = Field(ge=0, description="Position in STAGES, for the progress strip.")
    stage_count: int = len(STAGES)
    label: str = Field(description="The action, as a button. Imperative when it is ours.")
    detail: str = Field(description="One line saying why, in the operator's language.")
    actor: Actor
    urgency: Urgency
    #: Where the action happens. `null` means it is on the order's own page.
    href: str | None = None


def _urgency_floor(order: Order, today: date, current: Urgency) -> Urgency:
    """Demurrage can only raise urgency, never lower it.

    The clock runs whether or not anyone is looking, and it is the one thing on this screen
    that costs money by itself. An order with a person to chase and two days of free time left
    is still that person's problem; the same order on its last free day is a different problem.
    """
    if order.last_free_day is None or current == "now":
        return current
    days = (order.last_free_day - today).days
    if days < 0:
        return "now"
    if days <= 1 and current in ("soon", "waiting"):
        return "soon"
    return current


def next_action(
    order: Order, *, open_approvals: int, quotes_in_hand: int, today: date
) -> NextAction:
    """What this order needs next. Derived, never stored -- so it cannot go stale."""
    stage_index = _STAGE_BY_STATUS.get(order.status, 0)
    stage = STAGES[stage_index]

    # A person has been asked for something. That outranks everything else the order is
    # doing, because it is the only state where the system has stopped and is waiting.
    if open_approvals > 0:
        label = "Approve the award" if order.status is OrderStatus.AWAITING_APPROVAL else "Decide"
        return NextAction(
            stage=stage,
            stage_index=stage_index,
            label=label,
            detail=(
                f"{open_approvals} decision waiting on a person."
                if open_approvals == 1
                else f"{open_approvals} decisions waiting on a person."
            ),
            actor="operator",
            urgency="now",
        )

    if order.status is OrderStatus.RECEIVED and not order.has_mandate:
        return NextAction(
            stage=STAGES[1],
            stage_index=1,
            label="Grant a mandate",
            detail="Nothing is authorized until a person sets the ceiling and the window.",
            actor="operator",
            urgency=_urgency_floor(order, today, "soon"),
        )

    if order.status is OrderStatus.RECEIVED:
        return NextAction(
            stage=STAGES[2],
            stage_index=2,
            label="Open the market",
            detail="The mandate is granted. Dial carriers for a comparison.",
            actor="operator",
            urgency=_urgency_floor(order, today, "soon"),
        )

    if order.status is OrderStatus.QUOTING:
        detail = (
            "Carriers are being dialled in parallel. Nothing to do until they answer."
            if quotes_in_hand == 0
            else f"{quotes_in_hand} quote(s) in hand. The market closes on timeout."
        )
        return NextAction(
            stage=STAGES[2],
            stage_index=2,
            label="Collecting quotes",
            detail=detail,
            actor="volta",
            urgency=_urgency_floor(order, today, "waiting"),
        )

    if order.status is OrderStatus.AWARDING:
        return NextAction(
            stage=STAGES[4],
            stage_index=4,
            label="Award call in progress",
            detail="Restating the exact terms to the winner and asking for an explicit yes.",
            actor="volta",
            urgency="waiting",
        )

    if order.status is OrderStatus.BOOKED:
        return NextAction(
            stage=STAGES[4],
            stage_index=4,
            label="Booked",
            detail="The written recap was delivered. Waiting on pickup.",
            actor="volta",
            urgency=_urgency_floor(order, today, "waiting"),
        )

    if order.status is OrderStatus.AT_RISK:
        return NextAction(
            stage=STAGES[5],
            stage_index=5,
            label="Review the incident",
            detail="The delivery deadline passed and the carrier was chased.",
            actor="operator",
            urgency="now",
        )

    if order.status is OrderStatus.IN_TRANSIT:
        return NextAction(
            stage=STAGES[5],
            stage_index=5,
            label="In transit",
            detail="Moving. The deadline sweep is watching it.",
            actor="volta",
            urgency="waiting",
        )

    # delivered, closed, cancelled -- done, and saying so is better than an empty cell.
    return NextAction(
        stage=STAGES[5],
        stage_index=5,
        label=str(order.status).replace("_", " ").capitalize(),
        detail="Nothing further is expected on this operation.",
        actor="nobody",
        urgency="none",
    )
