"""Explicit adapters from trusted configuration and domain records into prompt inputs.

The adapter deliberately names every copied field. Adding a database or mandate column must
not silently add something the phone agent can say.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, cast

from app.domain import (
    BusinessType,
    CallContext,
    CallPhase,
    Carrier,
    CompanyProfile,
    Order,
    QuoteRow,
)

__all__ = ["CompanySettings", "company_profile_from_settings", "context_from_order"]


class CompanySettings(Protocol):
    """The configuration fields agent composition consumes, without importing config/."""

    company_name: str
    company_business_type: str
    company_city: str
    company_country: str
    company_currency: str
    company_timezone: str
    company_primary_language: str
    company_fallback_language: str
    agent_name: str
    agent_role: str


def company_profile_from_settings(settings: CompanySettings) -> CompanyProfile:
    """Build the spoken company identity from explicit configuration fields."""
    return CompanyProfile(
        display_name=settings.company_name,
        business_type=cast(BusinessType, settings.company_business_type),
        city=settings.company_city,
        country=settings.company_country,
        currency=settings.company_currency,
        timezone=settings.company_timezone,
        primary_language=settings.company_primary_language,
        fallback_language=settings.company_fallback_language,
        agent_name=settings.agent_name,
        agent_role=settings.agent_role,
    )


def _window(order: Order) -> str | None:
    if order.pickup_not_before is None or order.pickup_not_after is None:
        return None
    start = order.pickup_not_before.isoformat()
    end = order.pickup_not_after.isoformat()
    return f"{start} to {end}"


def _best_rate(quotes: Sequence[QuoteRow], order: Order) -> Decimal | None:
    currency = order.cap.currency if order.cap is not None else None
    comparable = [quote.amount.amount for quote in quotes if quote.amount.currency == currency]
    return min(comparable) if comparable else None


def context_from_order(
    order: Order,
    carrier: Carrier | None,
    phase: CallPhase,
    market_state: Sequence[QuoteRow],
    *,
    today: date | None = None,
) -> CallContext:
    """Map operational state into the fixed prompt context for one call.

    ``today`` is injectable for replay and tests. The default exists for composition code that
    has not supplied a clock yet; once built, the resulting context is stored with the call.
    """
    calendar_day = today or datetime.now(UTC).date()
    return CallContext(
        phase=phase,
        today=calendar_day.strftime("%A, %B %d, %Y"),
        reference=order.reference,
        origin=order.origin,
        destination=order.destination,
        cargo=order.cargo,
        equipment=order.equipment,
        weight=order.weight,
        pickup_window=_window(order),
        counterparty_name=carrier.name if carrier else None,
        counterparty_contact=carrier.contact_name if carrier else None,
        price_ceiling=order.cap.amount if order.cap else None,
        target_price=order.target.amount if order.target else None,
        quotes_in_hand=len(market_state),
        best_rate_so_far=_best_rate(market_state, order),
        missed_deadline=(
            order.delivery_deadline.isoformat() if order.delivery_deadline is not None else None
        ),
        expected_driver=order.expected_driver,
        expected_plate=order.expected_plate,
        expected_carrier=carrier.name if carrier else None,
    )
