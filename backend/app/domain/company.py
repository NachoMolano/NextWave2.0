"""Who the agent works for. Filled once at pre-registration, from the dashboard.

MAY IMPORT:  stdlib, pydantic. Nothing from app.
IMPORTED BY: agent (renders it into the prompt); repo and the dashboard API later.

This is the state that lets one agent serve any company that moves freight by road — an
importer, an exporter, a retailer, a forwarder — without a second codebase. It is always
the *shipper's* side of the phone: the agent buys ground transport, it never sells it.

Nothing here authorizes anything. This object says who the agent is and how it should
sound; what it may agree to is the mandate, which is a different object and is evaluated
in policy/. Keep it that way: a counterparty can talk the agent into ignoring a sentence
of its profile, and that must never be the same thing as talking it past its limits.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["BusinessType", "CompanyProfile"]

# Why an enum and not free text: the agent introduces itself differently to a carrier
# depending on which of these it is ("we import", "we ship for our stores"), and a
# dispatcher reads a wrong self-description as a wrong number.
BusinessType = Literal[
    "importer",
    "exporter",
    "retailer",
    "manufacturer",
    "distributor",
    "freight_forwarder",
    "3pl",
]


class CompanyProfile(BaseModel):
    """Everything the agent needs to sound like it actually works at this company.

    Frozen on purpose. The profile is written by a human before any call and read during
    one; nothing that hears audio may hold a mutable reference to it.
    """

    model_config = ConfigDict(frozen=True)

    display_name: str = Field(description="What the agent says out loud on the phone.")
    business_type: BusinessType
    city: str
    country: str

    legal_name: str | None = Field(
        default=None, description="Only for written recaps. Never spoken."
    )
    commodities: list[str] = Field(
        default_factory=list,
        description="What this company moves, in the words a dispatcher would use.",
    )
    equipment: list[str] = Field(
        default_factory=list,
        description="Equipment it normally books: dry van, container chassis, reefer, flatbed.",
    )

    # Currency and units are read out loud, so they are part of the voice, not formatting.
    # An agent that says "eight thousand" without a currency has produced incomplete data
    # (invariant #8), which is why this has no None case.
    currency: str = Field(default="USD", description="ISO 4217. Always said with the amount.")
    units: Literal["metric", "imperial"] = "metric"

    # Needed to turn "Thursday" into a calendar date. Without it the agent cannot obey
    # invariant #8 and must ask for the date every single time.
    timezone: str = Field(default="UTC", description="IANA name, e.g. America/Bogota.")

    # BCP-47-ish tags. The agent opens in primary_language and follows the counterparty
    # into fallback_language if that is what they speak.
    primary_language: str = "en"
    fallback_language: str = "es-CO"

    agent_name: str = Field(default="Volta", description="The name the agent gives on the call.")
    agent_role: str = Field(
        default="transport coordinator",
        description="How it describes its job in one phrase, in the primary language.",
    )

    business_hours: str | None = Field(
        default=None, description="Spoken as-is when scheduling, e.g. 'Monday to Friday, 8 to 6'."
    )
    recap_channel: Literal["sms", "email", "both"] = Field(
        default="email",
        description="What the agent promises on the call. A commitment only counts once it is out.",
    )
