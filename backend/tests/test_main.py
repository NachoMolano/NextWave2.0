"""Composition-root integration checks.

The track suites build their routers independently. This test proves that their real factory
signatures are wired together by ``main.py`` without touching Supabase, Vapi, or a provider.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import (
    ApprovalReason,
    CallDirection,
    CallRecord,
    CallReport,
    Carrier,
    Commitment,
    CommitmentState,
    IncidentSubject,
    NotificationChannel,
    Order,
    OrderStatus,
    Severity,
)
from app.main import build_after_report, create_app
from app.tools.commitments import CommitmentCoordinator
from tests.fakes import InMemoryStore, RecordingNotifier

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def test_production_refuses_to_start_without_readiness_gates() -> None:
    import pytest

    with pytest.raises(RuntimeError, match="PRODUCTION_RETENTION_READY"):
        create_app(Settings(environment="production"))


def test_a_demo_deployment_reports_the_keys_it_is_missing() -> None:
    """The deploy runs as ``demo``, so the production gate never looked at it.

    OPENAI_REPORT_MODEL, RESEND_API_KEY and MANAGER_EMAIL were all empty on a deployment
    placing real calls, and nothing said so: /health answered a flat "ok" while every call
    brief and every written confirmation failed. A demo still boots -- refusing to start
    minutes before a demo is the worse failure -- but it says what is missing.
    """
    # _env_file=None: the assertion is about an unset key, and a developer's own .env
    # setting it would quietly delete the test.
    app = create_app(Settings(environment="demo", _env_file=None))

    with TestClient(app) as client:
        body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["config"] == "incomplete"
    assert "OPENAI_REPORT_MODEL" in body["missing"]
    assert "RESEND_API_KEY" in body["missing"]


def test_a_local_run_needs_none_of_the_deployment_keys() -> None:
    app = create_app(Settings(environment="local", _env_file=None))

    with TestClient(app) as client:
        body = client.get("/health").json()

    assert body == {"status": "ok", "environment": "local"}


def test_create_app_mounts_every_integrated_surface() -> None:
    app = create_app(Settings(supabase_url="", supabase_secret_key=""))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/vapi/events").status_code == 401
        assert client.post("/vapi/tools").status_code == 401
        # The portal route is mounted; an unconfigured store must degrade to 503 rather than
        # attempting a network connection while the integration test is running.
        assert client.get("/api/orders").status_code == 503


async def test_award_report_sends_official_recap_and_books_only_after_delivery() -> None:
    store = InMemoryStore()
    store.add_order(Order(id="order-1", reference="OP-1", status=OrderStatus.AWARDING))
    store.add_carrier(
        Carrier(
            id="carrier-1",
            name="Carrier One",
            phone="+525500000001",
            email="dispatch@example.com",
        )
    )
    call = CallRecord(
        id="call-1",
        vapi_call_id="vapi-1",
        direction=CallDirection.OUTBOUND,
        phase="award",
        order_id="order-1",
        carrier_id="carrier-1",
        recording_url="https://recordings.example/call-1.wav",
    )
    await store.upsert_call(call)
    notifier = RecordingNotifier()
    coordinator = CommitmentCoordinator(store, notifier, now=lambda: NOW)
    commitment_id = await coordinator.open_verbal(
        Commitment(
            order_id="order-1",
            quote_id="quote-1",
            evidence_call_id="call-1",
            evidence_anchor_ms=12_345,
            terms={"amount": "8100 USD", "pickup": "2026-09-03T08:00:00Z"},
        )
    )
    after_report = build_after_report(store, notifier, coordinator, Settings(), now=lambda: NOW)

    await after_report(call, CallReport(call_id="call-1", summary="Carrier confirmed."))

    commitment = await store.commitment(commitment_id)
    assert commitment is not None and commitment.state is CommitmentState.COMMITTED
    assert store.orders["order-1"].status is OrderStatus.BOOKED
    assert len(notifier.sent) == 1
    message = notifier.sent[0]
    assert message.to_address == "dispatch@example.com"
    assert "00:12.345" in message.body
    assert "https://recordings.example/call-1.wav" in message.body


async def test_an_award_call_that_confirmed_nothing_says_so() -> None:
    """Silence here is an order stuck in AWARDING with nothing on the screen about it.

    An award call ending without a confirmed pre-agreement is a legitimate outcome -- the
    terms had moved, the recap was never assented to. What is not legitimate is returning
    quietly: the carrier was never confirmed, no email is owed to anyone, and the only party
    who can move it is a person who has not been told.
    """
    store = InMemoryStore()
    notifier = RecordingNotifier()
    coordinator = CommitmentCoordinator(store, notifier, now=lambda: NOW)
    call = CallRecord(
        id="call-1",
        vapi_call_id="vapi-1",
        direction=CallDirection.OUTBOUND,
        phase="award",
        order_id="order-1",
        carrier_id="carrier-1",
    )
    await store.upsert_call(call)
    after_report = build_after_report(store, notifier, coordinator, Settings(), now=lambda: NOW)

    await after_report(call, CallReport(call_id="call-1", summary="Terms had changed."))

    assert notifier.sent == [], "nothing was confirmed, so nothing is confirmed in writing"
    raised = list(store.approvals.values())
    assert [a.reason for a in raised] == [ApprovalReason.POLICY_FAILURE]
    assert "no confirmed pre-agreement" in str(raised[0].context["detail"])


async def test_incident_report_notifies_manager_by_both_channels_once() -> None:
    store = InMemoryStore()
    notifier = RecordingNotifier()
    coordinator = CommitmentCoordinator(store, notifier, now=lambda: NOW)
    after_report = build_after_report(
        store,
        notifier,
        coordinator,
        Settings(manager_email="manager@example.com", manager_whatsapp="+573001112233"),
        now=lambda: NOW,
    )
    call = CallRecord(
        id="call-incident",
        vapi_call_id="vapi-incident",
        direction=CallDirection.INBOUND,
        phase="inbound",
    )
    report = CallReport(
        call_id="call-incident",
        summary="Carrier reported a collision.",
        subject=IncidentSubject.ACCIDENT,
        severity=Severity.HIGH,
    )

    await after_report(call, report)
    await after_report(call, report)

    assert [message.channel for message in notifier.sent] == [
        NotificationChannel.EMAIL,
        NotificationChannel.WHATSAPP,
    ]
    assert len(store.deliveries) == 2
