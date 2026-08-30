"""The tool server, which is the only thing a stranger on the phone can reach.

The first test in this file is the one that matters. Vapi ignores any status code that is
not 200 -- it does not retry, it does not surface an error, it simply carries on as though
the tool call had worked. So a handler that raises must still produce a 200 with an ``error``
string, or invariant #6 inverts: a crash in the code that refuses things becomes permission.

Everything here runs with no network, no database and no phone call.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.domain import CallDirection, CallRecord, Store
from app.notify.sender import NullNotifier
from app.tools.calls import CallLedger
from app.tools.commitments import CommitmentCoordinator
from app.tools.model import (
    ConfirmPreagreementArgs,
    LookupOrderArgs,
    ModelTools,
    ProposeQuoteArgs,
    ReportIncidentArgs,
    VerifyCallerArgs,
)
from app.vapi.toolserver import HOLD_AND_ESCALATE, create_tool_router
from tests.fakes import InMemoryStore

SECRET = "shared-secret-for-tests"
VAPI_CALL_ID = "vapi-call-provisional-1"
TOOL_CALL_ID = "toolu_01DTPAzUm5Gk3zxrpJ969oMF"
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _now() -> datetime:
    return NOW


# --------------------------------------------------------------------------------- doubles


class ScriptedTools(ModelTools):
    """Track A's handlers, replaced by whatever this test needs them to do.

    A subclass rather than a stand-in object so the router is exercised against the real
    class: if Track A renames a handler, these tests fail rather than silently testing a
    shape that no longer exists.
    """

    def __init__(
        self,
        store: Store,
        *,
        answer: str = "Recorded. Nothing is booked.",
        explode: BaseException | None = None,
    ) -> None:
        super().__init__(
            store,
            now=_now,
            ledger=CallLedger(store, now=_now),
            commitments=CommitmentCoordinator(store, NullNotifier(), now=_now),
        )
        self._answer = answer
        self._explode = explode
        self.seen: list[tuple[str, str, Any]] = []

    async def _respond(self, name: str, call_id: str, args: Any) -> str:
        self.seen.append((name, call_id, args))
        if self._explode is not None:
            raise self._explode
        return self._answer

    async def propose_quote(self, call_id: str, args: ProposeQuoteArgs) -> str:
        return await self._respond("propose_quote", call_id, args)

    async def confirm_preagreement(self, call_id: str, args: ConfirmPreagreementArgs) -> str:
        return await self._respond("confirm_preagreement", call_id, args)

    async def verify_caller(self, call_id: str, args: VerifyCallerArgs) -> str:
        return await self._respond("verify_caller", call_id, args)

    async def lookup_order(self, call_id: str, args: LookupOrderArgs) -> str:
        return await self._respond("lookup_order", call_id, args)

    async def report_incident(self, call_id: str, args: ReportIncidentArgs) -> str:
        return await self._respond("report_incident", call_id, args)


def _propose_quote_call(tool_call_id: str = TOOL_CALL_ID) -> dict[str, Any]:
    return {
        "id": tool_call_id,
        "name": "propose_quote",
        "arguments": {
            "components": [{"name": "linehaul", "amount": "8500", "currency": "USD"}],
            "cost_is_final": True,
            "pickup_date": "2026-09-03",
            "equipment": "40-foot container chassis",
            "claimed_identity": "Rafael from Transportes del Pacifico",
        },
    }


def _envelope(*tool_calls: dict[str, Any], vapi_call_id: str = VAPI_CALL_ID) -> dict[str, Any]:
    return {
        "message": {
            "type": "tool-calls",
            "call": {"id": vapi_call_id},
            "toolCallList": list(tool_calls),
        }
    }


# --------------------------------------------------------------------------------- harness


async def _seed_call(store: InMemoryStore, vapi_call_id: str = VAPI_CALL_ID) -> str:
    return await store.upsert_call(
        CallRecord(
            vapi_call_id=vapi_call_id,
            direction=CallDirection.OUTBOUND,
            phase="rfq",
            started_at=NOW,
        )
    )


def _client(tools: ModelTools, store: Store, *, secret: str = SECRET) -> AsyncClient:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_tool_router(tools, store, server_secret=secret), prefix="/vapi")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://toolserver.test")


async def _post(
    tools: ModelTools,
    store: Store,
    payload: dict[str, Any],
    *,
    secret: str = SECRET,
    header: str | None = SECRET,
) -> tuple[int, dict[str, Any]]:
    headers = {"X-Vapi-Secret": header} if header is not None else {}
    async with _client(tools, store, secret=secret) as client:
        response = await client.post("/vapi/tools", json=payload, headers=headers)
    return response.status_code, response.json()


# ------------------------------------------------------- the one that fails open if wrong


async def test_a_raising_handler_still_returns_200_with_an_error_string() -> None:
    """The whole reason this file exists.

    Vapi ignores a 500 completely: the model is told nothing, assumes the tool worked, and
    keeps negotiating. A crash in the code that refuses things would become permission.
    """
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store, explode=RuntimeError("the database fell over"))

    code, body = await _post(tools, store, _envelope(_propose_quote_call()))

    assert code == 200, "a non-200 is discarded by Vapi and fails open"
    (result,) = body["results"]
    assert result["toolCallId"] == TOOL_CALL_ID
    assert "error" in result, "a failure must come back as an error string, not as a result"
    assert "\n" not in result["error"]
    assert result["error"].strip()


async def test_an_incomplete_context_holds_instead_of_pretending() -> None:
    """A missing order cannot become an authorization just because the server is reachable."""
    store = InMemoryStore()
    await _seed_call(store)
    real_tools = ModelTools(
        store,
        now=_now,
        ledger=CallLedger(store, now=_now),
        commitments=CommitmentCoordinator(store, NullNotifier(), now=_now),
    )

    code, body = await _post(real_tools, store, _envelope(_propose_quote_call()))

    assert code == 200
    (result,) = body["results"]
    assert "result" in result
    assert "recorded" not in result["result"].lower()


async def test_the_error_string_never_claims_the_call_succeeded() -> None:
    """A hold line the model can read as "done" is worse than no line at all."""
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store, explode=RuntimeError("boom"))

    _, body = await _post(tools, store, _envelope(_propose_quote_call()))

    error = body["results"][0]["error"].lower()
    for forbidden in ("approved", "booked", "confirmed", "recorded it", "done"):
        assert forbidden not in error, f"the hold line leaked {forbidden!r}"


# ----------------------------------------------------------------------------- happy path


async def test_a_successful_call_echoes_the_exact_tool_call_id() -> None:
    """Vapi matches the answer to the question by id. A near-miss is a dropped result."""
    store = InMemoryStore()
    call_id = await _seed_call(store)
    tools = ScriptedTools(store)

    code, body = await _post(tools, store, _envelope(_propose_quote_call()))

    assert code == 200
    (result,) = body["results"]
    assert result["toolCallId"] == TOOL_CALL_ID
    assert result["result"] == "Recorded. Nothing is booked."
    assert tools.seen == [("propose_quote", call_id, tools.seen[0][2])]
    assert isinstance(tools.seen[0][2], ProposeQuoteArgs)


async def test_arguments_arrive_as_the_validated_model_track_a_expects() -> None:
    """The router parses; the handler never sees a raw dict from the phone."""
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store)

    await _post(tools, store, _envelope(_propose_quote_call()))

    _, _, args = tools.seen[0]
    assert isinstance(args, ProposeQuoteArgs)
    assert args.cost_is_final is True
    assert args.components[0].amount == "8500", "the figure they said, unrounded"
    assert args.components[0].currency == "USD"


async def test_a_multi_line_result_is_collapsed_to_one_line() -> None:
    """Vapi requires a single-line string, and a newline is read aloud as dead air."""
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store, answer="Recorded.\nNothing is booked.\n\n  Ask about tolls.")

    _, body = await _post(tools, store, _envelope(_propose_quote_call()))

    assert body["results"][0]["result"] == "Recorded. Nothing is booked. Ask about tolls."


async def test_several_tool_calls_in_one_envelope_each_get_their_own_result() -> None:
    """Vapi batches. Answering only the first silently strands the rest."""
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store)
    envelope = _envelope(
        _propose_quote_call("toolu_first"),
        {"id": "toolu_second", "name": "lookup_order", "arguments": {}},
    )

    _, body = await _post(tools, store, envelope)

    assert [r["toolCallId"] for r in body["results"]] == ["toolu_first", "toolu_second"]
    assert [name for name, _, _ in tools.seen] == ["propose_quote", "lookup_order"]


async def test_the_provisional_fixture_dispatches() -> None:
    """The hand-written envelope in tests/fixtures/vapi must reach a handler.

    PROVISIONAL until CP4 replaces it with a captured payload, so a green run here means
    the code is self-consistent -- not that it matches what Vapi sends.
    """
    import json
    from pathlib import Path

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "vapi" / "tool_calls.json").read_text()
    )
    store = InMemoryStore()
    call_id = await _seed_call(store, fixture["message"]["call"]["id"])
    tools = ScriptedTools(store)

    code, body = await _post(tools, store, {"message": fixture["message"]})

    assert code == 200
    assert body["results"][0]["toolCallId"] == fixture["message"]["toolCallList"][0]["id"]
    assert tools.seen[0][:2] == ("propose_quote", call_id)


# -------------------------------------------------------------------------- fail closed


async def test_a_wrong_secret_is_refused_before_anything_is_dispatched() -> None:
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store)

    code, _ = await _post(tools, store, _envelope(_propose_quote_call()), header="not-the-secret")

    assert code == 401
    assert tools.seen == [], "an unauthenticated request must not reach a handler"


async def test_a_missing_secret_header_is_refused() -> None:
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store)

    code, _ = await _post(tools, store, _envelope(_propose_quote_call()), header=None)

    assert code == 401
    assert tools.seen == []


async def test_an_unconfigured_secret_refuses_everything() -> None:
    """Fail closed. An empty VAPI_SERVER_SECRET means anyone who finds the URL owns it."""
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store)

    code, _ = await _post(tools, store, _envelope(_propose_quote_call()), secret="", header="")

    assert code == 401
    assert tools.seen == []


async def test_an_uncorrelated_call_holds_and_never_reaches_a_handler() -> None:
    """No call row means no order, no mandate and no anchor. There is nothing safe to do."""
    store = InMemoryStore()  # deliberately empty
    tools = ScriptedTools(store)

    code, body = await _post(tools, store, _envelope(_propose_quote_call()))

    assert code == 200
    assert "error" in body["results"][0]
    assert tools.seen == []


async def test_an_unknown_tool_name_holds() -> None:
    """The model can emit a name we never offered. That is not a reason to improvise."""
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store)

    code, body = await _post(
        tools, store, _envelope({"id": "t1", "name": "wire_the_money", "arguments": {}})
    )

    assert code == 200
    assert "error" in body["results"][0]
    assert tools.seen == []


async def test_arguments_that_fail_validation_write_nothing() -> None:
    """A shape the tool does not accept is not a reason to record a partial quote."""
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store)
    broken = {"id": "t1", "name": "propose_quote", "arguments": {"components": []}}

    code, body = await _post(tools, store, _envelope(broken))

    assert code == 200
    assert "error" in body["results"][0]
    assert tools.seen == []
    assert store.quotes == {}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": "not an object"},
        {"message": {"type": "tool-calls"}},
        {"message": {"type": "tool-calls", "toolCallList": "not a list"}},
    ],
    ids=["empty", "message-not-an-object", "no-tool-call-list", "tool-call-list-not-a-list"],
)
async def test_a_malformed_envelope_is_200_with_no_results(payload: dict[str, Any]) -> None:
    """Nothing to answer and nothing to hold on, but still never a status Vapi discards."""
    store = InMemoryStore()
    tools = ScriptedTools(store)

    code, body = await _post(tools, store, payload)

    assert code == 200
    assert body["results"] == []
    assert tools.seen == []


async def test_a_tool_call_without_an_id_is_skipped_rather_than_guessed() -> None:
    """A result Vapi cannot match gets attached to whichever call it decides on."""
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store)
    envelope = _envelope(
        {"name": "lookup_order", "arguments": {}},
        _propose_quote_call("toolu_real"),
    )

    _, body = await _post(tools, store, envelope)

    assert [r["toolCallId"] for r in body["results"]] == ["toolu_real"]


async def test_a_store_that_is_down_holds_instead_of_authorizing() -> None:
    """Policy unreachable is a hold, not a pass. Invariant #6."""

    class BrokenStore(InMemoryStore):
        async def call_by_vapi_id(self, vapi_call_id: str) -> None:
            raise ConnectionError("supabase is unreachable")

    store = BrokenStore()
    tools = ScriptedTools(store)

    code, body = await _post(tools, store, _envelope(_propose_quote_call()))

    assert code == 200
    assert "error" in body["results"][0]
    assert tools.seen == []


def test_the_five_tools_are_the_whole_surface() -> None:
    """Adding a sixth widens what a stranger on the phone can reach. It is not a fix."""
    from app.vapi.assistant import TOOL_ARGUMENT_MODELS

    assert set(TOOL_ARGUMENT_MODELS) == {
        "propose_quote",
        "confirm_preagreement",
        "verify_caller",
        "lookup_order",
        "report_incident",
    }


def test_every_tool_name_has_a_handler_on_track_as_class() -> None:
    """The router dispatches by name with getattr. A rename must fail here, not on a call."""
    from app.vapi.assistant import TOOL_ARGUMENT_MODELS

    for name in TOOL_ARGUMENT_MODELS:
        handler: Callable[..., Any] | None = getattr(ModelTools, name, None)
        assert callable(handler), f"tools/model.py has no handler named {name}"


# --- the envelope Vapi actually sends -------------------------------------------------
#
# The fixture above was written from docs.vapi.ai and never captured from a live call, and
# it is wrong. In production every propose_quote came back "That tool is not available on
# this call": Vapi mirrors the shape the tool was *defined* in, and build_tool_definitions
# writes the OpenAI-style {"type": "function", "function": {...}} form, so the callback
# carries the name under "function" and the arguments as a JSON string. Two carrier quotes
# were lost to this while this whole file stayed green.

NESTED_TOOL_CALL_ID = "call_lDoxFYNlQTASX9GfefdfMGO7"


def _nested_propose_quote_call(tool_call_id: str = NESTED_TOOL_CALL_ID) -> dict[str, Any]:
    """One toolCallList entry as captured from Vapi call
    01a0522a-9413-7000-9840-2ea58ebd6475 on 2026-08-30, arguments verbatim."""
    return {
        "id": tool_call_id,
        "type": "function",
        "function": {
            "name": "propose_quote",
            "arguments": json.dumps(
                {
                    "components": [{"name": "linehaul", "amount": "15000", "currency": "MXN"}],
                    "cost_is_final": True,
                    "pickup_date": "2026-09-03",
                    "equipment": "40-foot container chassis",
                    "claimed_identity": "Ana Beltran",
                }
            ),
        },
    }


async def test_the_nested_function_envelope_reaches_the_handler() -> None:
    """The regression that cost two live carrier quotes."""
    store = InMemoryStore()
    call_id = await _seed_call(store)
    tools = ScriptedTools(store)

    status_code, body = await _post(tools, store, _envelope(_nested_propose_quote_call()))

    assert status_code == 200
    result = body["results"][0]
    assert "error" not in result, result
    assert result["toolCallId"] == NESTED_TOOL_CALL_ID
    assert [name for name, _, _ in tools.seen] == ["propose_quote"]
    assert tools.seen[0][1] == call_id


async def test_the_nested_envelope_arguments_arrive_as_the_validated_model() -> None:
    """The JSON string must be parsed before Track A's model sees it, not passed through."""
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store)

    await _post(tools, store, _envelope(_nested_propose_quote_call()))

    args = tools.seen[0][2]
    assert isinstance(args, ProposeQuoteArgs)
    assert args.components[0].currency == "MXN"
    assert args.components[0].amount == "15000"
    assert args.claimed_identity == "Ana Beltran"


async def test_the_flat_envelope_still_dispatches() -> None:
    """A widening, not a swap: the documented shape must keep working."""
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store)

    _, body = await _post(tools, store, _envelope(_propose_quote_call()))

    assert "error" not in body["results"][0]
    assert [name for name, _, _ in tools.seen] == ["propose_quote"]


async def test_an_unparseable_argument_string_holds_and_dispatches_nothing() -> None:
    """A JSON string that does not parse is not a partial fact."""
    entry = _nested_propose_quote_call()
    entry["function"]["arguments"] = "{not json"
    store = InMemoryStore()
    await _seed_call(store)
    tools = ScriptedTools(store)

    status_code, body = await _post(tools, store, _envelope(entry))

    assert status_code == 200
    assert body["results"][0]["error"] == HOLD_AND_ESCALATE
    assert tools.seen == []
