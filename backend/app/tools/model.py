"""The five tools exposed to the model. The complete mutation surface a stranger can reach.

MAY IMPORT:  domain, policy, store, notify.
IMPORTED BY: vapi/toolserver.py.

Every handler returns a **single-line string** that Vapi reads back to the model. Three
things that string may never contain, because a counterparty can ask for all three:

  * the price ceiling, the target, or any figure that came from our instructions rather than
    from them;
  * a number, date or name the counterparty did not actually say;
  * the words "approved", "booked" or "confirmed" -- a call creates a pre-agreement, and the
    model must not be handed language that lets it claim otherwise.

The argument models below are frozen in Phase 0 because two tracks depend on them: Track A
implements the handlers, Track B renders these same shapes as the JSON schemas Vapi
validates against. Changing one is a CHANGELOG event.

STATUS: Phase 0 stub -- argument models are real, handlers raise. OWNER: Track A.
"""

from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain import IncidentSubject, Store

__all__ = [
    "ConfirmPreagreementArgs",
    "LookupOrderArgs",
    "ModelTools",
    "ProposeQuoteArgs",
    "QuotedComponent",
    "ReportIncidentArgs",
    "VerifyCallerArgs",
]


class QuotedComponent(BaseModel):
    """One line of what they quoted: the linehaul, the tolls, the waiting time.

    Each carries its own currency. A carrier who quotes the run in pesos and the tolls in
    dollars has said two things, and flattening them loses the one that matters.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=80, description="linehaul, tolls, waiting, ...")
    amount: str = Field(description="Exactly as said. A string so nothing is rounded in transit.")
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217. Never inferred.")


class ProposeQuoteArgs(BaseModel):
    """A rate a carrier stated on the call. A proposal, never an authorization."""

    model_config = ConfigDict(frozen=True)

    components: list[QuotedComponent] = Field(
        min_length=1, description="Every payable element they named, each with its currency."
    )
    cost_is_final: bool = Field(
        description="True only if they said the total covers everything. 'Plus tolls' is False."
    )
    pickup_date: str = Field(description="An explicit calendar date, ISO 8601. Never a weekday.")
    pickup_window_end: str | None = Field(
        default=None, description="ISO 8601, if they gave a window rather than a time."
    )
    equipment: str = Field(min_length=1)
    valid_until: str | None = Field(
        default=None, description="ISO 8601. How long they said the quote holds."
    )
    claimed_identity: str | None = Field(
        default=None, description="The name they gave. Recorded, never trusted."
    )


class ConfirmPreagreementArgs(BaseModel):
    """The carrier confirmed the exact recap, word for word, on an award call."""

    model_config = ConfigDict(frozen=True)

    quote_id: str = Field(min_length=1, description="The quote whose terms were read back.")
    carrier_confirmed_exact_recap: bool = Field(
        description="True only after they answered yes to the complete recap. "
        "A 'sure' in reply to five things is not a yes to five things."
    )
    claimed_identity: str | None = None


class VerifyCallerArgs(BaseModel):
    """One identity attempt on an inbound call.

    The fact must come from them. Never read the expected value out first: a caller who is
    told the plate can repeat the plate, which verifies nothing.
    """

    model_config = ConfigDict(frozen=True)

    claimed_name: str | None = None
    claimed_company: str | None = None
    fact_kind: str = Field(description="reference | plate | container | driver")
    fact_value: str = Field(min_length=1, description="What they said, verbatim.")


class LookupOrderArgs(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str | None = Field(
        default=None, description="Only used on an inbound call not yet correlated."
    )


class ReportIncidentArgs(BaseModel):
    """What the call turned out to be about."""

    model_config = ConfigDict(frozen=True)

    subject: IncidentSubject
    detail: str = Field(min_length=1, description="What they said happened, in their terms.")
    new_eta: str | None = Field(
        default=None,
        description="ISO 8601 with a clock time. Only if they gave an explicit date and time.",
    )
    load_at_risk: bool = False


class ModelTools:
    """The model-facing handlers. Nothing here decides; each one asks policy and records.

    ``now`` is injected rather than read from the clock so a scenario can be replayed and
    produce the same decisions.
    """

    def __init__(self, store: Store, *, now: Callable[[], datetime]) -> None:
        self._store = store
        self._now = now

    async def propose_quote(self, call_id: str, args: ProposeQuoteArgs) -> str:
        """Parse, evaluate against the mandate, write quote + decision + event.

        An ambiguous amount returns the clarification line and writes nothing: "eight five"
        is not data until somebody says which number it is.
        """
        raise NotImplementedError("Track A: implement app/tools/model.py")

    async def confirm_preagreement(self, call_id: str, args: ConfirmPreagreementArgs) -> str:
        """Gate on anchored recap evidence, then open a commitment in state VERBAL."""
        raise NotImplementedError("Track A: implement app/tools/model.py")

    async def verify_caller(self, call_id: str, args: VerifyCallerArgs) -> str:
        """Compare a claimed fact against the order. Returns match / no match, nothing else."""
        raise NotImplementedError("Track A: implement app/tools/model.py")

    async def lookup_order(self, call_id: str, args: LookupOrderArgs) -> str:
        """Read-only. Returns nothing operational until identity_verified is true."""
        raise NotImplementedError("Track A: implement app/tools/model.py")

    async def report_incident(self, call_id: str, args: ReportIncidentArgs) -> str:
        """Record what happened. Moves order status only along a whitelisted transition."""
        raise NotImplementedError("Track A: implement app/tools/model.py")
