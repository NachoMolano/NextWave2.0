"""Post a Vapi webhook fixture twice and prove the second delivery changes nothing.

Vapi retries on any non-2xx and on a timeout, so the same ``end-of-call-report`` arrives more
than once as a matter of course. Invariant 7 says the second one is a no-op; this is the
script that demonstrates it rather than asserting it in a unit test nobody watches.

    uv run python -m scripts.replay_webhook
    uv run python -m scripts.replay_webhook --fixture status_update
    uv run python -m scripts.replay_webhook --url http://localhost:8000

Default runs in process through ``tools/calls.py`` against InMemoryStore -- no server, no
database. ``--url`` posts at a running ``/vapi/events`` once Track B has built it.

The fixtures are PROVISIONAL until CP4: they were written from the docs, not captured from a
real call. See ``tests/fixtures/vapi/README.md``.

OWNER: Track E.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.domain import CallDirection, CallRecord, CallStatus, Turn
from app.tools.calls import CallLedger
from tests.fakes import InMemoryStore

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "vapi"
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

_STATUS = {
    "queued": CallStatus.QUEUED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.ACTIVE,
    "ended": CallStatus.ENDED,
    "forwarding": CallStatus.ACTIVE,
}


def now() -> datetime:
    return NOW


def load(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return payload


def to_call_record(message: dict[str, Any]) -> CallRecord:
    """Map a Vapi server message onto our call row.

    ``secondsFromStart`` is read here only to fill the transcript for display. It is never
    the anchor: the offset a quote or a commitment records is measured server-side when the
    tool fires, because this field is undocumented and has a reported epoch-value bug.
    """
    call = message.get("call", {})
    artifact = message.get("artifact", {})
    turns = [
        Turn(
            speaker="agent" if entry.get("role") == "assistant" else "caller",
            text=str(entry.get("message", "")),
            offset_ms=(
                int(float(entry["secondsFromStart"]) * 1000)
                if isinstance(entry.get("secondsFromStart"), int | float)
                else None
            ),
        )
        for entry in artifact.get("messages", [])
    ]
    cost = message.get("cost")
    return CallRecord(
        vapi_call_id=str(call.get("id")),
        direction=(
            CallDirection.INBOUND
            if str(call.get("type", "")).startswith("inbound")
            else CallDirection.OUTBOUND
        ),
        phase="rfq",
        status=_STATUS.get(str(message.get("status", "")), CallStatus.ENDED),
        started_at=NOW - timedelta(minutes=3),
        ended_reason=message.get("endedReason"),
        recording_url=artifact.get("recordingUrl"),
        transcript=turns,
        cost_cents=int(float(cost) * 100) if isinstance(cost, int | float) else None,
    )


async def replay_in_process(name: str) -> bool:
    payload = load(name)
    message = payload["message"]
    store = InMemoryStore()
    ledger = CallLedger(store, now=now)
    record = to_call_record(message)
    # Keyed on the call and the message type -- the same pair Vapi redelivers.
    key = f"{record.vapi_call_id}:{message['type']}"
    apply = (
        ledger.finalize if message["type"] == "end-of-call-report" else (ledger.upsert_from_webhook)
    )

    first = await apply(record, key)
    second = await apply(record, key)

    print(f"  first  delivery -> call id {first}")
    print(f"  second delivery -> {second}")
    print(f"  calls={len(store.calls)} events={len(store.events)}")

    if second is not None:
        print("  FAIL  the second delivery was applied; it must be a no-op")
        return False
    if len(store.calls) != 1 or len(store.events) != 1:
        print("  FAIL  a redelivery created a second row")
        return False
    print("  ok    the second delivery changed nothing")
    return True


async def replay_over_http(name: str, url: str) -> bool:
    import httpx

    payload = load(name)
    async with httpx.AsyncClient(timeout=20) as client:
        endpoint = f"{url.rstrip('/')}/vapi/events"
        first = await client.post(endpoint, json=payload)
        second = await client.post(endpoint, json=payload)

    print(f"  first  {first.status_code} {first.text[:120]}")
    print(f"  second {second.status_code} {second.text[:120]}")
    if first.status_code != 200 or second.status_code != 200:
        print("  FAIL  both deliveries must answer 200")
        return False
    print("  ok    both answered 200; check the rows in the portal to confirm the no-op")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        default="end_of_call_report",
        choices=["end_of_call_report", "status_update"],
        help="which PROVISIONAL fixture to replay",
    )
    parser.add_argument("--url", help="post at a running /vapi/events instead of in process")
    options = parser.parse_args()

    print(f"=== replaying {options.fixture} twice ===")
    ok = (
        await replay_over_http(options.fixture, options.url)
        if options.url
        else await replay_in_process(options.fixture)
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
