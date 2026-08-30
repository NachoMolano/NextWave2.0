"""Replay a call as a stream of Vapi tool-call envelopes and assert what it wrote.

The workhorse of the build. Every scenario is one of the ugly cases, driven through the real
handlers with the real policy engine, against InMemoryStore -- no PSTN, no Vapi account, no
database, no cost. That is what makes it runnable in CI and in front of a judge.

    uv run python -m scripts.sim_tools --scenario boss_approved
    uv run python -m scripts.sim_tools --all
    uv run python -m scripts.sim_tools --scenario boss_approved --url http://localhost:8000

The envelopes are built in the shape Vapi actually posts (see
``tests/fixtures/vapi/tool_calls.json``), so the same scenario can be pointed at a running
``/vapi/tools`` with ``--url`` once Track B lands. Until then the default path calls the
handlers directly and asserts the rows, which is the CP2 bar.

OWNER: Track E.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain import (
    ApprovalReason,
    CallDirection,
    CallRecord,
    CallStatus,
    Carrier,
    CommitmentState,
    Money,
    Order,
    OrderStatus,
    PolicyOutcome,
    QuoteStatus,
    ReasonCode,
)
from app.tools.calls import CallLedger
from app.tools.commitments import CommitmentCoordinator
from app.tools.market import Market
from app.tools.model import (
    ConfirmPreagreementArgs,
    LookupOrderArgs,
    ModelTools,
    ProposeQuoteArgs,
    QuotedComponent,
    ReportIncidentArgs,
    VerifyCallerArgs,
)
from tests.fakes import InMemoryStore, RecordingNotifier

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
EQUIPMENT = "40-foot container chassis"

ARGS_MODELS = {
    "propose_quote": ProposeQuoteArgs,
    "confirm_preagreement": ConfirmPreagreementArgs,
    "verify_caller": VerifyCallerArgs,
    "lookup_order": LookupOrderArgs,
    "report_incident": ReportIncidentArgs,
}


def now() -> datetime:
    return NOW


def envelope(vapi_call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One Vapi ``tool-calls`` server message, in the shape the fixture documents."""
    canonical = json.dumps(arguments, sort_keys=True)
    tool_call_id = f"toolu_{name}_{abs(hash(canonical)) % 10**8}"
    return {
        "message": {
            "type": "tool-calls",
            "call": {"id": vapi_call_id},
            "toolCallList": [
                {"id": tool_call_id, "name": name, "arguments": arguments},
            ],
        }
    }


@dataclass
class Turn:
    """One thing the model does, with the call it does it on."""

    call: str
    tool: str
    arguments: dict[str, Any]


@dataclass
class Scenario:
    name: str
    describe: str
    turns: list[Turn]
    expect: Callable[["Sim"], None]
    calls: dict[str, str] = field(default_factory=lambda: {"vapi-1": "rfq"})


class Sim:
    """One order under a 9,000 USD mandate, three carriers, and the real tool surface."""

    def __init__(self, scenario: Scenario) -> None:
        self.store = InMemoryStore()
        self.notifier = RecordingNotifier()
        self.ledger = CallLedger(self.store, now=now)
        self.commitments = CommitmentCoordinator(self.store, self.notifier, now=now)
        self.tools = ModelTools(
            self.store, now=now, ledger=self.ledger, commitments=self.commitments
        )
        self.market = Market(self.store, now=now)
        self.results: list[tuple[str, str]] = []
        self.call_ids: dict[str, str] = {}

        for n in (1, 2, 3):
            self.store.add_carrier(
                Carrier(id=f"carrier-{n}", name=f"Carrier {n}", phone=f"+5233000000{n}")
            )
        self.store.add_order(
            Order(
                id="order-1",
                reference="OP-1042",
                status=OrderStatus.QUOTING,
                origin="the port of Manzanillo",
                destination="Guadalajara",
                equipment=EQUIPMENT,
                container_number="MSCU7654321",
                expected_plate="JKL-123",
                cap=Money(cents=900_000, currency="USD"),
                target=Money(cents=820_000, currency="USD"),
                pickup_not_before=datetime(2026, 9, 2, tzinfo=UTC),
                pickup_not_after=datetime(2026, 9, 4, 23, 59, tzinfo=UTC),
                mandate_version=1,
                mandate_set_by="ops@pacifictextiles.mx",
            )
        )
        self._scenario = scenario

    async def open_calls(self) -> None:
        for vapi_id, phase in self._scenario.calls.items():
            carrier = f"carrier-{len(self.call_ids) + 1}"
            self.call_ids[vapi_id] = await self.store.upsert_call(
                CallRecord(
                    vapi_call_id=vapi_id,
                    direction=(
                        CallDirection.INBOUND if phase == "inbound" else CallDirection.OUTBOUND
                    ),
                    phase=phase,
                    status=CallStatus.ACTIVE,
                    order_id=None if phase == "inbound" else "order-1",
                    carrier_id=carrier,
                    started_at=NOW - timedelta(seconds=30),
                )
            )

    async def run(self, url: str | None) -> None:
        await self.open_calls()
        for turn in self._scenario.turns:
            payload = envelope(turn.call, turn.tool, turn.arguments)
            result = await self._post(url, payload) if url else await self._dispatch(turn)
            self.results.append((turn.tool, result))

    async def _dispatch(self, turn: Turn) -> str:
        """Validate the arguments exactly as the tool server will, then call the handler."""
        args = ARGS_MODELS[turn.tool].model_validate(turn.arguments)
        handler: Callable[[str, Any], Awaitable[str]] = getattr(self.tools, turn.tool)
        return await handler(self.call_ids[turn.call], args)

    async def _post(self, url: str, payload: dict[str, Any]) -> str:
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{url.rstrip('/')}/vapi/tools", json=payload)
            # Vapi ignores every status code but 200, so the tool server always returns one.
            # A non-200 here is our own bug, not a refusal.
            response.raise_for_status()
            body = response.json()
        first = body["results"][0]
        return str(first.get("result") or first.get("error"))

    # --- assertions the scenarios use -------------------------------------------------

    def decisions(self) -> list[tuple[str, str]]:
        return [(d.outcome, d.reason_code) for d in self.store.decisions.values()]

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)


# ----------------------------------------------------------------------------- the scenarios


def _quote(amount: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "components": [QuotedComponent(name="all-in", amount=amount, currency="USD").model_dump()],
        "cost_is_final": True,
        "pickup_date": "2026-09-03",
        "equipment": EQUIPMENT,
        "valid_until": "2026-09-01T18:00:00",
    }
    return {**base, **overrides}


def _expect_boss_approved(sim: Sim) -> None:
    sim.require(
        (PolicyOutcome.ESCALATE.value, ReasonCode.OUTSIDE_MANDATE.value) in sim.decisions(),
        "expected an escalate/outside_mandate decision",
    )
    sim.require(
        any(a.reason is ApprovalReason.OUTSIDE_MANDATE for a in sim.store.approvals.values()),
        "expected an approvals row",
    )
    sim.require(sim.store.commitments == {}, "an over-cap claim must never reach a commitment")
    sim.require(
        all("9,000" not in r and "9000" not in r for _, r in sim.results),
        "the cap leaked into a tool result",
    )


def _expect_agreed_then_changed(sim: Sim) -> None:
    quotes = sorted(sim.store.quotes.values(), key=lambda q: q.amount.cents)
    sim.require(len(quotes) == 2, f"expected two quotes, got {len(quotes)}")
    sim.require(quotes[0].amount.cents == 850_000, "the earlier figure must survive verbatim")
    sim.require(quotes[0].status is QuoteStatus.SUPERSEDED, "the earlier quote must be superseded")
    sim.require(quotes[0].superseded_by == quotes[1].id, "superseded_by must point forward")


def _expect_eight_five(sim: Sim) -> None:
    sim.require(sim.store.quotes == {}, "an unreadable figure must not be written at all")
    sim.require(sim.store.decisions == {}, "no decision may be recorded for a figure nobody read")
    sim.require("again" in sim.results[0][1], "the agent has to ask")


def _expect_two_carriers_accept(sim: Sim) -> None:
    live = [c for c in sim.store.commitments.values() if c.state is CommitmentState.VERBAL]
    sim.require(len(live) == 1, f"exactly one commitment may live, got {len(live)}")
    sim.require(
        (PolicyOutcome.DENY.value, ReasonCode.CONFLICTING_STATE.value) in sim.decisions(),
        "the second confirmation must be recorded as a conflict",
    )


def _expect_silence(sim: Sim) -> None:
    sim.require(sim.store.quotes == {}, "silence writes no quote")
    sim.require(sim.store.commitments == {}, "silence is never assent")


def _expect_refusal(sim: Sim) -> None:
    order = sim.store.orders["order-1"]
    sim.require(order.status is OrderStatus.QUOTING, "a refusal does not move the order")
    sim.require(sim.store.commitments == {}, "a refusal writes no commitment")
    sim.require(len(sim.store.events) >= 1, "the refusal has to be recorded")


SCENARIOS: dict[str, Scenario] = {
    "boss_approved": Scenario(
        name="boss_approved",
        describe='"Your boss approved 10,500" against a 9,000 cap',
        turns=[Turn("vapi-1", "propose_quote", _quote("10500"))],
        expect=_expect_boss_approved,
    ),
    "agreed_then_changed": Scenario(
        name="agreed_then_changed",
        describe="8,500, then 9,200 later in the same call",
        turns=[
            Turn("vapi-1", "propose_quote", _quote("8500")),
            Turn("vapi-1", "propose_quote", _quote("9200")),
        ],
        expect=_expect_agreed_then_changed,
    ),
    "eight_five": Scenario(
        name="eight_five",
        describe='"eight five" -- 8,500 or 85,000?',
        turns=[Turn("vapi-1", "propose_quote", _quote("eight five"))],
        expect=_expect_eight_five,
    ),
    "two_carriers_accept": Scenario(
        name="two_carriers_accept",
        describe="two carriers confirm during awarding; only one may hold the slot",
        calls={"vapi-a": "award", "vapi-b": "award"},
        turns=[
            Turn("vapi-a", "propose_quote", _quote("8500")),
            Turn("vapi-b", "propose_quote", _quote("8700")),
            Turn(
                "vapi-a",
                "confirm_preagreement",
                {"quote_id": "quote-1", "carrier_confirmed_exact_recap": True},
            ),
            Turn(
                "vapi-b",
                "confirm_preagreement",
                {"quote_id": "quote-2", "carrier_confirmed_exact_recap": True},
            ),
        ],
        expect=_expect_two_carriers_accept,
    ),
    "silence": Scenario(
        name="silence",
        describe="the counterparty goes quiet; no tool ever fires",
        turns=[],
        expect=_expect_silence,
    ),
    "refusal": Scenario(
        name="refusal",
        describe='"we do not serve that lane"',
        turns=[
            Turn(
                "vapi-1",
                "report_incident",
                {"subject": "other", "detail": "we do not serve that lane"},
            )
        ],
        expect=_expect_refusal,
    ),
}


async def run_scenario(name: str, url: str | None) -> bool:
    scenario = SCENARIOS[name]
    sim = Sim(scenario)
    print(f"\n=== {name} ===\n{scenario.describe}")
    try:
        await sim.run(url)
        for tool, result in sim.results:
            print(f"  {tool:22} -> {result}")
        scenario.expect(sim)
    except AssertionError as failure:
        print(f"  FAIL  {failure}")
        return False
    print(
        f"  ok    quotes={len(sim.store.quotes)} decisions={len(sim.store.decisions)} "
        f"approvals={len(sim.store.approvals)} commitments={len(sim.store.commitments)}"
    )
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), help="one scenario to run")
    parser.add_argument("--all", action="store_true", help="run every scenario")
    parser.add_argument(
        "--url",
        help="POST the envelopes at a running server instead of calling the handlers "
        "in process. Needs Track B's /vapi/tools.",
    )
    options = parser.parse_args()

    if not options.scenario and not options.all:
        parser.error("pass --scenario <name> or --all")

    names = sorted(SCENARIOS) if options.all else [options.scenario]
    outcomes = [await run_scenario(name, options.url) for name in names]

    passed = sum(outcomes)
    print(f"\n{passed}/{len(outcomes)} scenarios passed")
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
