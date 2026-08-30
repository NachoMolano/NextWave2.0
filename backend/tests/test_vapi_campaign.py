"""The parallel dial. Three carriers at once is the point of the whole exercise.

Every test here uses ``FakeCallPlacer`` or a subclass of it. The production placer is never
constructed in this file: a real call costs credits and can ring a real number, and a test
suite is exactly the place where that happens by accident.
"""

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from app.config import Settings
from app.domain import CallContext, CallPhase, Carrier, DialPlan
from app.vapi.assistant import profile_from_settings
from app.vapi.campaign import run_campaign
from app.vapi.client import CallPlacementError, ConcurrencyBlocked, VapiCallPlacer
from tests.fakes import FakeCallPlacer


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "vapi_model": "test-provider/test-model",
        "vapi_voice_id": "test-voice-provider/test-voice",
        "vapi_transcriber": "test-transcriber-provider/test-transcriber",
        "vapi_server_secret": "shared-secret-for-tests",
        "public_base_url": "https://volta.example.ngrok.app",
        "max_concurrent_calls": 8,
    }
    base.update(overrides)
    return Settings(**base)


SETTINGS = _settings()
PROFILE = profile_from_settings(SETTINGS)


def _plan(index: int, *, quotes_in_hand: int = 0, best: Decimal | None = None) -> DialPlan:
    """One carrier to call, with the market state frozen as of dial time.

    ``quotes_in_hand`` is the reason each assistant is composed per call rather than once:
    the third carrier is negotiated with two numbers already behind it, the first with none.
    """
    context = CallContext(
        phase=CallPhase.RFQ,
        today="Sunday, 30 August 2026",
        reference="OP-1042",
        origin="Manzanillo",
        destination="Guadalajara",
        cargo="textiles",
        equipment="40-foot container chassis",
        counterparty_name=f"Carrier {index}",
        price_ceiling=Decimal("9000"),
        target_price=Decimal("8200"),
        quotes_in_hand=quotes_in_hand,
        best_rate_so_far=best,
    )
    return DialPlan(
        call_id=f"call-{index}",
        carrier=Carrier(
            id=f"carrier-{index}", name=f"Carrier {index}", phone=f"+52331234567{index}"
        ),
        to_number=f"+52331234567{index}",
        context=context.model_dump(mode="json"),
    )


async def _no_sleep(seconds: float) -> None:
    """The backoff, without the wall clock. The retry is what is under test, not the wait."""
    return None


# ------------------------------------------------------------------- the definition of done


async def test_three_carriers_are_dialled_from_one_campaign() -> None:
    placer = FakeCallPlacer()
    plans = [_plan(1), _plan(2), _plan(3)]

    placed = await run_campaign(plans, placer, SETTINGS, profile=PROFILE, sleep=_no_sleep)

    assert len(placer.dialled) == 3
    assert {number for number, _ in placer.dialled} == {
        "+523312345671",
        "+523312345672",
        "+523312345673",
    }
    assert placed == {
        "call-1": "vapi-call-1",
        "call-2": "vapi-call-2",
        "call-3": "vapi-call-3",
    }


async def test_each_call_carries_its_own_market_state() -> None:
    """The fifth call negotiates with four numbers behind it; the first with none.

    One assistant reused across the fan-out would tell every carrier the same thing, which
    is the difference between a market and three unrelated phone calls.
    """
    placer = FakeCallPlacer()
    plans = [
        _plan(1, quotes_in_hand=0),
        _plan(2, quotes_in_hand=1, best=Decimal("8500")),
    ]

    await run_campaign(plans, placer, SETTINGS, profile=PROFILE, sleep=_no_sleep)

    prompts = {
        number: assistant["model"]["messages"][0]["content"] for number, assistant in placer.dialled
    }
    assert prompts["+523312345671"] != prompts["+523312345672"]


async def test_the_greeting_never_carries_a_mandate_figure() -> None:
    """The one line guaranteed to be spoken, checked on the payload that actually goes out."""
    placer = FakeCallPlacer()

    await run_campaign([_plan(1)], placer, SETTINGS, profile=PROFILE, sleep=_no_sleep)

    (_, assistant) = placer.dialled[0]
    for figure in ("9000", "9,000", "8200", "8,200"):
        assert figure not in assistant["firstMessage"]


# ---------------------------------------------------------------------------- concurrency


async def test_no_more_than_the_configured_number_of_calls_are_in_flight() -> None:
    """Vapi allows 10 concurrent by default. Stay under it so a retry has somewhere to go."""
    in_flight = 0
    peak = 0

    class CountingPlacer(FakeCallPlacer):
        async def place(self, assistant: dict[str, object], to_number: str) -> str:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await asyncio.sleep(0)
                return await super().place(assistant, to_number)
            finally:
                in_flight -= 1

    placer = CountingPlacer()
    plans = [_plan(i) for i in range(1, 9)]

    await run_campaign(
        plans, placer, _settings(max_concurrent_calls=3), profile=PROFILE, sleep=_no_sleep
    )

    assert len(placer.dialled) == 8
    assert peak <= 3, f"{peak} calls were in flight at once against a limit of 3"


async def test_a_blocked_slot_is_retried_rather_than_failed() -> None:
    """``status: queued`` with ``concurrencyBlocked`` is a "later", not an error."""
    attempts = 0

    class BlockedOncePlacer(FakeCallPlacer):
        async def place(self, assistant: dict[str, object], to_number: str) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConcurrencyBlocked("all slots busy")
            return await super().place(assistant, to_number)

    placer = BlockedOncePlacer()

    placed = await run_campaign([_plan(1)], placer, SETTINGS, profile=PROFILE, sleep=_no_sleep)

    assert attempts == 2
    assert placed == {"call-1": "vapi-call-1"}


async def test_a_permanently_blocked_slot_gives_up_instead_of_spinning() -> None:
    """An RFQ that spends four minutes waiting has missed the window it was placed to beat."""
    waits: list[float] = []

    async def record_sleep(seconds: float) -> None:
        waits.append(seconds)

    class AlwaysBlockedPlacer(FakeCallPlacer):
        async def place(self, assistant: dict[str, object], to_number: str) -> str:
            raise ConcurrencyBlocked("all slots busy")

    placer = AlwaysBlockedPlacer()

    placed = await run_campaign([_plan(1)], placer, SETTINGS, profile=PROFILE, sleep=record_sleep)

    assert placed == {}
    assert placer.dialled == []
    assert waits == [2.0, 4.0, 8.0], "the backoff doubles rather than hammering"


async def test_only_a_blocked_slot_is_retried() -> None:
    """Every other failure may already have dialled, and a blind retry is a second call."""
    attempts = 0

    class FailingPlacer(FakeCallPlacer):
        async def place(self, assistant: dict[str, object], to_number: str) -> str:
            nonlocal attempts
            attempts += 1
            raise CallPlacementError("Vapi refused the call with 400")

    placed = await run_campaign(
        [_plan(1)], FailingPlacer(), SETTINGS, profile=PROFILE, sleep=_no_sleep
    )

    assert placed == {}
    assert attempts == 1, "a failure that may have reached the network is never repeated"


# -------------------------------------------------------------------------- partial failure


async def test_one_dead_number_does_not_cancel_the_other_carriers() -> None:
    """An RFQ that dialled two of three is a smaller market. One that cancelled the rest is
    no market at all."""

    class OneBadNumberPlacer(FakeCallPlacer):
        async def place(self, assistant: dict[str, object], to_number: str) -> str:
            if to_number.endswith("2"):
                raise CallPlacementError("number disconnected")
            return await super().place(assistant, to_number)

    placer = OneBadNumberPlacer()
    plans = [_plan(1), _plan(2), _plan(3)]

    placed = await run_campaign(plans, placer, SETTINGS, profile=PROFILE, sleep=_no_sleep)

    assert set(placed) == {"call-1", "call-3"}
    assert len(placer.dialled) == 2


async def test_a_plan_whose_assistant_cannot_be_composed_fails_alone() -> None:
    """An unset vendor id takes down that call, not the campaign."""
    placer = FakeCallPlacer()
    broken = _settings(vapi_model="")

    placed = await run_campaign([_plan(1)], placer, broken, profile=PROFILE, sleep=_no_sleep)

    assert placed == {}
    assert placer.dialled == []


async def test_an_empty_plan_dials_nothing() -> None:
    placer = FakeCallPlacer()

    assert await run_campaign([], placer, SETTINGS, profile=PROFILE) == {}
    assert placer.dialled == []


# ----------------------------------------------------------------------------- the money


async def test_an_unconfigured_production_placer_refuses_before_the_request() -> None:
    """The only construction of the real placer in the suite, and it never reaches the network.

    Every other test dials through ``FakeCallPlacer``. This one exists because an
    unconfigured placer must fail loudly at the first dial rather than send three silent
    401s while somebody watches an empty dashboard -- and proving that requires the real
    class. The credentials are empty, so the refusal happens before any request is built.
    """
    with pytest.raises(CallPlacementError, match="VAPI_API_KEY"):
        await VapiCallPlacer(Settings(vapi_api_key="", vapi_phone_number_id="")).place(
            {}, "+520000000000"
        )


# ------------------------------------------------------------------------ launch spacing


async def test_the_dials_are_spaced_rather_than_fired_in_one_instant() -> None:
    """Every call in a campaign leaves from one number, and providers rate-limit per number.

    Three creates inside the same millisecond raced transport allocation on a live run: one
    carrier never rang (``call.start.error-get-transport``, no provider id) and one rang with
    no assistant on it. Only the first dial starts immediately; the rest wait their turn.
    """
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    placer = FakeCallPlacer()

    await run_campaign(
        [_plan(1), _plan(2), _plan(3)], placer, SETTINGS, profile=PROFILE, sleep=record
    )

    assert len(placer.dialled) == 3
    assert waits == [2.0, 4.0], "the first dials at once; the others are spaced behind it"


async def test_a_single_carrier_waits_for_nothing() -> None:
    """A one-carrier campaign has nothing to race, so it must not pay the spacing."""
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    await run_campaign([_plan(1)], FakeCallPlacer(), SETTINGS, profile=PROFILE, sleep=record)

    assert waits == []
