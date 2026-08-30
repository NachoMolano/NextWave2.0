"""Phase 0's own test: the seams hold.

Nothing here tests behaviour anybody built. It tests that the contract four tracks are about
to code against is real -- that the fakes and the production classes satisfy the same
Protocols, and that the three behaviours the other tracks *assume* are actually implemented
in the fake rather than merely described in a docstring.

If this file goes red, one of the tracks is building against something that does not exist.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.agent.prompts import (
    DEMO_CONTEXT,
    DEMO_PROFILE,
    build_greeting,
    build_system_prompt,
)
from app.agent.report import OpenAIReportModel
from app.config import Settings
from app.domain import (
    AwardConflict,
    CallPhase,
    CallPlacer,
    DeliveryStatus,
    EventRow,
    Money,
    NotificationChannel,
    Notifier,
    Order,
    OrderStatus,
    OutboundMessage,
    QuoteRow,
    ReportModel,
    Store,
)
from app.notify.sender import NullNotifier, ResendTwilioNotifier
from app.store.supabase import SupabaseStore
from app.vapi.client import VapiCallPlacer
from tests.fakes import (
    FakeCallPlacer,
    InMemoryStore,
    RecordingNotifier,
    ScriptedReportModel,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _quote(order_id: str, carrier_id: str, cents: int) -> QuoteRow:
    return QuoteRow(
        order_id=order_id,
        carrier_id=carrier_id,
        call_id="call-1",
        anchor_ms=11_200,
        amount=Money(cents=cents, currency="USD"),
        cost_is_final=True,
        pickup_at=NOW + timedelta(days=3),
        equipment="40-foot container chassis",
        valid_until=NOW + timedelta(days=1),
    )


# --------------------------------------------------------------------- protocol conformance


@pytest.mark.parametrize(
    ("implementation", "protocol"),
    [
        (InMemoryStore(), Store),
        (SupabaseStore(Settings()), Store),
        (FakeCallPlacer(), CallPlacer),
        (VapiCallPlacer(Settings()), CallPlacer),
        (RecordingNotifier(), Notifier),
        (NullNotifier(), Notifier),
        (ResendTwilioNotifier(Settings()), Notifier),
        (ScriptedReportModel(), ReportModel),
        (OpenAIReportModel(api_key="", model=""), ReportModel),
    ],
    ids=lambda value: type(value).__name__ if not isinstance(value, type) else value.__name__,
)
def test_implements_its_protocol(implementation: object, protocol: type) -> None:
    """The fake and the real thing answer to the same name, or a track is coding blind."""
    assert isinstance(implementation, protocol)


# ------------------------------------------------- the three behaviours tracks depend on


async def test_repeated_idempotency_key_is_refused() -> None:
    """Vapi redelivers webhooks. The second delivery must be a no-op, not a second row."""
    store = InMemoryStore()
    event = EventRow(type="call.ended", idempotency_key="vapi-call-1:end-of-call-report")

    assert await store.append_event(event) is True
    assert await store.append_event(event) is False, (
        "a repeated key must return False so the caller stops. Raising would make a "
        "redelivery an error, and it is a normal event."
    )
    assert len(store.events) == 1


async def test_second_award_conflicts() -> None:
    """Two carriers confirming at once must not both find an empty slot."""
    store = InMemoryStore()
    first = await store.add_quote(_quote("order-1", "carrier-1", 850_000))
    second = await store.add_quote(_quote("order-1", "carrier-2", 870_000))

    await store.accept_quote("order-1", first)
    with pytest.raises(AwardConflict):
        await store.accept_quote("order-1", second)


async def test_superseding_a_quote_keeps_both_rows() -> None:
    """They said 8,500 and then 9,200. Both were said; neither may be overwritten."""
    store = InMemoryStore()
    first = await store.add_quote(_quote("order-1", "carrier-1", 850_000))
    second = await store.add_quote(_quote("order-1", "carrier-1", 920_000))

    await store.supersede_quote(first, second)

    assert len(await store.quotes_for("order-1")) == 2
    old = await store.quote(first)
    assert old is not None
    assert old.amount.cents == 850_000, "the earlier figure must survive verbatim"
    assert old.superseded_by == second


# ------------------------------------------------------------------------- the sweep query


async def test_due_for_chase_skips_deliveries_already_underway() -> None:
    """OUTBOUND 2 fires on a passed deadline, but never on a load that is already moving."""
    store = InMemoryStore()
    overdue = NOW - timedelta(hours=2)
    store.add_order(
        Order(id="late", reference="OP-1", delivery_deadline=overdue, status=OrderStatus.BOOKED)
    )
    store.add_order(
        Order(
            id="moving",
            reference="OP-2",
            delivery_deadline=overdue,
            status=OrderStatus.IN_TRANSIT,
        )
    )
    store.add_order(
        Order(
            id="future",
            reference="OP-3",
            delivery_deadline=NOW + timedelta(days=1),
            status=OrderStatus.BOOKED,
        )
    )

    due = await store.due_for_chase(NOW)

    assert [o.id for o in due] == ["late"]


# ------------------------------------------------------------------- the mandate projection


def test_an_order_without_a_mandate_authorizes_nothing() -> None:
    """A missing ceiling is not "no limit". It is a mandate that was never granted."""
    order = Order(id="o", reference="OP-1042", equipment="chassis")

    assert order.has_mandate is False
    with pytest.raises(ValueError, match="no mandate"):
        order.mandate()


def test_a_complete_mandate_projects_into_policys_value() -> None:
    order = Order(
        id="o",
        reference="OP-1042",
        equipment="40-foot container chassis",
        cap=Money(cents=900_000, currency="USD"),
        pickup_not_before=NOW,
        pickup_not_after=NOW + timedelta(days=5),
        mandate_version=1,
        mandate_set_by="ops@pacifictextiles.mx",
    )

    mandate = order.mandate()

    assert mandate.max_all_in_usd == 9000
    assert mandate.operation_id == "o"
    assert mandate.allowed_equipment == frozenset({"40-foot container chassis"})


# ------------------------------------------------------------------------- the null sender


async def test_an_unconfigured_notifier_reports_failure_not_success() -> None:
    """A sender that claimed success without sending would promote an unwritten commitment."""
    result = await NullNotifier().send(
        OutboundMessage(
            channel=NotificationChannel.EMAIL, to_address="ops@example.com", body="recap"
        )
    )

    assert result.status is DeliveryStatus.FAILED
    assert result.error is not None


# ----------------------------------------------------------------- every stub is honest


def test_placer_never_dials_in_the_suite() -> None:
    """FakeCallPlacer records instead of dialling. A real call costs money and rings a phone."""
    placer = FakeCallPlacer()
    assert placer.dialled == []


# ------------------------------------------------------------------ the phase table's gap


@pytest.mark.parametrize(
    "phase",
    [
        CallPhase.RFQ,
        CallPhase.AWARD,
        CallPhase.RENEGOTIATION,
        CallPhase.INBOUND,
        CallPhase.STATUS_CHECK,
    ],
    ids=lambda phase: phase.value,
)
def test_every_call_phase_composes_a_prompt(phase: CallPhase) -> None:
    """A phase the system can enter but cannot speak in is a crash waiting for a live call."""
    context = DEMO_CONTEXT.model_copy(update={"phase": phase})

    prompt = build_system_prompt(DEMO_PROFILE, context)
    greeting = build_greeting(DEMO_PROFILE, context)

    assert prompt.strip()
    assert greeting.strip()


def test_no_mandate_figure_reaches_a_greeting() -> None:
    """The ceiling is in the prompt so the agent can negotiate. It is never in what it says.

    The greeting is the one line guaranteed to be spoken, so it is the cheapest place to
    catch a leak. It is not a substitute for policy/: a figure that escapes here is an
    embarrassment, and policy is what keeps it from also being an authorization.
    """
    context = DEMO_CONTEXT.model_copy(
        update={"price_ceiling": Decimal("9000"), "target_price": Decimal("8200")}
    )

    greeting = build_greeting(DEMO_PROFILE, context)

    for figure in ("9000", "9,000", "8200", "8,200"):
        assert figure not in greeting, f"the greeting leaked a mandate figure: {figure}"
