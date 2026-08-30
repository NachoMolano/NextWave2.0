"""Track D prompt, mapping, and post-call extraction tests."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from openai import AsyncOpenAI

from app.agent import company_profile_from_settings, context_from_order
from app.agent.prompts import build_greeting, build_runtime_system_prompt, build_system_prompt
from app.agent.report import OpenAIReportModel
from app.config import Settings
from app.domain import (
    CallContext,
    CallPhase,
    Carrier,
    CompanyProfile,
    IncidentSubject,
    Money,
    Order,
    QuoteRow,
    Severity,
    Turn,
)


@pytest.fixture
def profile() -> CompanyProfile:
    return CompanyProfile(
        display_name="Pacific Textiles",
        business_type="importer",
        city="Guadalajara",
        country="Mexico",
        currency="USD",
        primary_language="en",
        fallback_language="es-MX",
    )


@pytest.fixture
def context() -> CallContext:
    return CallContext(
        phase=CallPhase.RFQ,
        today="Sunday, August 30, 2026",
        reference="OP-1042",
        origin="Manzanillo",
        destination="Guadalajara",
        cargo="textiles",
        price_ceiling=Decimal("9000"),
        target_price=Decimal("8200"),
        missed_deadline="2026-08-30T09:00:00+00:00",
    )


@pytest.mark.parametrize("phase", list(CallPhase))
def test_all_five_phases_compose(
    phase: CallPhase, profile: CompanyProfile, context: CallContext
) -> None:
    phase_context = context.model_copy(update={"phase": phase})
    assert build_system_prompt(profile, phase_context).strip()
    assert build_runtime_system_prompt(profile, phase_context).strip()
    assert build_greeting(profile, phase_context).strip()


def test_status_check_collects_eta_without_authorizing_changes(
    profile: CompanyProfile, context: CallContext
) -> None:
    status_context = context.model_copy(update={"phase": CallPhase.STATUS_CHECK})
    prompt = build_system_prompt(profile, status_context)
    runtime = build_runtime_system_prompt(profile, status_context)
    combined = f"{prompt}\n{runtime}".lower()

    assert "what happened" in combined
    assert "clock time" in combined
    assert "calendar date" in combined
    assert "do not accept a price change" in combined
    assert "detention" in combined
    assert "escalate" in combined


def test_no_greeting_leaks_mandate_figures(profile: CompanyProfile, context: CallContext) -> None:
    for phase in CallPhase:
        greeting = build_greeting(profile, context.model_copy(update={"phase": phase}))
        for figure in ("9000", "9,000", "8200", "8,200"):
            assert figure not in greeting


def test_rfq_uses_a_warm_finite_negotiation_and_quote_tool(
    profile: CompanyProfile, context: CallContext
) -> None:
    rfq = context.model_copy(update={"phase": CallPhase.RFQ})
    prompt = build_system_prompt(profile, rfq).lower()
    runtime = build_runtime_system_prompt(profile, rfq).lower()

    assert "good moment to discuss it" in prompt
    assert "lowest workable price" in prompt
    assert "competitive alternatives" in prompt
    assert "invented competing quote" in prompt
    assert "propose_quote" in runtime
    assert "never call confirm_preagreement" in runtime
    assert "never imply booking" in runtime


def test_booking_confirms_exact_terms_without_renegotiating(
    profile: CompanyProfile, context: CallContext
) -> None:
    award = context.model_copy(update={"phase": CallPhase.AWARD})
    prompt = build_system_prompt(profile, award).lower()
    runtime = build_runtime_system_prompt(profile, award).lower()

    assert "not sourcing, comparing, or" in prompt
    assert "negotiating on this call" in prompt
    assert "explicit verbal confirmation" in prompt
    assert "confirm_preagreement" in runtime
    assert "never use propose_quote" in runtime
    assert "written confirmation" in runtime


def test_inbound_listens_verifies_and_reports_without_promising_resolution(
    profile: CompanyProfile, context: CallContext
) -> None:
    inbound = context.model_copy(update={"phase": CallPhase.INBOUND})
    prompt = build_system_prompt(profile, inbound).lower()
    runtime = build_runtime_system_prompt(profile, inbound).lower()

    assert "predisposed to listen" in prompt
    assert "verify_caller" in prompt
    assert "lookup_order" in prompt
    assert "report_incident" in prompt
    assert "member of the team will contact them" in prompt
    assert "never promise a decision or a callback time" in runtime


def test_profile_and_context_mapping_is_explicit() -> None:
    settings = Settings(
        company_name="Andes Imports",
        company_business_type="importer",
        company_city="Bogota",
        company_country="Colombia",
        company_currency="COP",
        company_timezone="America/Bogota",
        company_primary_language="es-CO",
        company_fallback_language="en",
        agent_name="Luz",
        agent_role="coordinadora de transporte",
    )
    profile = company_profile_from_settings(settings)
    carrier = Carrier(id="carrier-1", name="Ruta Uno", phone="+573001112233")
    order = Order(
        id="order-1",
        reference="NW-7",
        origin="Cartagena",
        destination="Bogota",
        cargo="fabric",
        equipment="container chassis",
        weight="18 tonnes",
        cap=Money(cents=900_000, currency="USD"),
        target=Money(cents=820_000, currency="USD"),
        pickup_not_before=datetime(2026, 9, 2, 8, tzinfo=UTC),
        pickup_not_after=datetime(2026, 9, 4, 17, tzinfo=UTC),
        delivery_deadline=datetime(2026, 9, 5, 12, tzinfo=UTC),
    )
    quote = QuoteRow(
        order_id=order.id,
        carrier_id=carrier.id,
        call_id="call-1",
        anchor_ms=2_500,
        amount=Money(cents=850_000, currency="USD"),
        pickup_at=datetime(2026, 9, 3, 9, tzinfo=UTC),
        equipment="container chassis",
        valid_until=datetime(2026, 9, 1, 18, tzinfo=UTC),
    )

    mapped = context_from_order(
        order, carrier, CallPhase.STATUS_CHECK, [quote], today=date(2026, 8, 30)
    )

    assert profile.display_name == "Andes Imports"
    assert profile.primary_language == "es-CO"
    assert mapped.reference == "NW-7"
    assert mapped.counterparty_name == "Ruta Uno"
    assert mapped.price_ceiling == Decimal("9000")
    assert mapped.best_rate_so_far == Decimal("8500")
    assert mapped.quotes_in_hand == 1
    assert mapped.today == "Sunday, August 30, 2026"
    assert mapped.missed_deadline == "2026-09-05T12:00:00+00:00"


class _FakeResponses:
    async def parse(self, **kwargs: object) -> SimpleNamespace:
        schema = kwargs["text_format"]
        parsed = schema(
            summary="Carrier confirmed a revised ETA.",
            subject=IncidentSubject.DELAY,
            severity=Severity.HIGH,
            actions=[{"text": "Send gate code", "offset_ms": 1_200}],
            mentions=[{"text": "Traffic closure", "offset_ms": 700}],
            quoted_prices=[{"amount": "8500", "currency": "USD", "offset_ms": 900}],
            objections=["Terminal queue"],
            conditions=["Subject to gate reopening"],
            agreement_candidates=[
                {
                    "counterparty": "Ruta Uno",
                    "terms": ["Delivery at 14:00 on 2026-08-30"],
                    "offset_ms": 1_500,
                }
            ],
        )
        return SimpleNamespace(output_parsed=parsed)


class _FakeOpenAI:
    responses = _FakeResponses()


async def test_structured_report_contains_all_fields_and_anchored_candidates(
    context: CallContext,
) -> None:
    model = OpenAIReportModel(
        api_key="unused",
        model="scripted",
        client=cast(AsyncOpenAI, _FakeOpenAI()),
    )
    report = await model.report(
        "call-7",
        [Turn(speaker="caller", text="We can deliver at two today", offset_ms=1_500)],
        context,
    )

    assert report.call_id == "call-7"
    assert report.subject is IncidentSubject.DELAY
    assert report.severity is Severity.HIGH
    assert report.actions and report.mentions and report.quoted_prices
    assert report.objections and report.conditions
    assert report.agreement_candidates[0]["offset_ms"] == 1_500
    assert report.model == "scripted"
