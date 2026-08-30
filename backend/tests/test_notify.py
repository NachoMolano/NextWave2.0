"""Track D outbox rendering and offline provider tests."""

from datetime import UTC, datetime

import httpx
import pytest

from app.config import Settings
from app.domain import (
    CallReport,
    Commitment,
    Comparison,
    ComparisonEntry,
    DeliveryStatus,
    IncidentSubject,
    Money,
    NotificationChannel,
    OutboundMessage,
    Severity,
)
from app.notify.render import (
    render_award_request,
    render_commitment_email,
    render_incident_report,
    render_not_selected_email,
)
from app.notify.sender import NullNotifier, ResendTwilioNotifier


@pytest.fixture
def report() -> CallReport:
    return CallReport(
        call_id="call-1",
        summary="Carrier reported a missed deadline and a new ETA.",
        subject=IncidentSubject.DELAY,
        severity=Severity.HIGH,
        actions=[{"text": "Notify warehouse", "offset_ms": 4_000}],
        objections=["Terminal congestion"],
        conditions=["Gate must reopen"],
    )


def test_commitment_email_contains_terms_anchors_and_recording(report: CallReport) -> None:
    commitment = Commitment(
        id="commitment-1",
        order_id="order-1",
        quote_id="quote-1",
        evidence_call_id="call-1",
        evidence_anchor_ms=65_432,
        terms={"rate": "8,500 USD", "pickup": "2026-09-02 09:00"},
    )
    message = render_commitment_email(
        commitment, report, "carrier@example.com", "https://recordings.example/call-1"
    )

    assert message.channel is NotificationChannel.EMAIL
    assert message.commitment_id == "commitment-1"
    assert "8,500 USD" in message.body
    assert "2026-09-02 09:00" in message.body
    assert message.body.count("01:05.432") == 2
    assert "https://recordings.example/call-1" in message.body


def test_award_and_incident_templates_include_required_facts(report: CallReport) -> None:
    comparison = Comparison(
        order_id="order-1",
        entries=[
            ComparisonEntry(
                quote_id="quote-1",
                carrier_id="carrier-1",
                carrier_name="Ruta Uno",
                amount=Money(cents=850_000, currency="USD"),
                all_in_usd_cents=850_000,
                pickup_at=datetime(2026, 9, 2, 9, tzinfo=UTC),
                equipment="chassis",
                outcome="allow",
                reason_code="within_mandate",
                is_winner=True,
            )
        ],
        winner_quote_id="quote-1",
        cap_at_decision_cents=900_000,
        cap_currency="USD",
        mandate_version=2,
        built_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    award = render_award_request(comparison, "manager@example.com")
    email = render_incident_report(report, "manager@example.com")
    whatsapp = render_incident_report(report, "+573001112233", whatsapp=True)

    assert "Ruta Uno" in award.body
    assert "within_mandate" in award.body
    assert "quote-1" in award.body
    assert email.subject and "High incident" in email.subject
    assert "Terminal congestion" in email.body
    assert whatsapp.channel is NotificationChannel.WHATSAPP
    assert whatsapp.subject is None
    assert len(whatsapp.body) < len(email.body)


def test_not_selected_email_is_courteous_and_non_committal() -> None:
    message = render_not_selected_email(
        order_id="order-1",
        reference="OP-1042",
        carrier_name="Carrier One",
        to_address="dispatch@example.com",
    )
    assert "not selected" in message.body
    assert "future opportunities" in message.body


def _settings() -> Settings:
    return Settings(
        resend_api_key="resend-key",
        notify_from_email="volta@example.com",
        twilio_account_sid="AC123",
        twilio_auth_token="twilio-token",
        twilio_whatsapp_from="+15550001111",
    )


async def test_resend_and_twilio_success_are_offline() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.resend.com":
            return httpx.Response(200, json={"id": "email-1"})
        return httpx.Response(201, json={"sid": "SM123"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = ResendTwilioNotifier(_settings(), client=client)
        email = await notifier.send(
            OutboundMessage(
                channel=NotificationChannel.EMAIL,
                to_address="carrier@example.com",
                subject="Commitment",
                body="Confirmed",
            )
        )
        whatsapp = await notifier.send(
            OutboundMessage(
                channel=NotificationChannel.WHATSAPP,
                to_address="+573001112233",
                body="Incident",
            )
        )

    assert email.status is DeliveryStatus.SENT
    assert email.provider_message_id == "email-1"
    assert whatsapp.status is DeliveryStatus.SENT
    assert whatsapp.provider_message_id == "SM123"
    assert len(seen) == 2
    assert seen[0].url.host == "api.resend.com"
    assert b"whatsapp%3A%2B573001112233" in seen[1].content


@pytest.mark.parametrize(
    ("response", "expected", "status"),
    [
        (httpx.Response(503, text="down"), "HTTP 503", DeliveryStatus.FAILED),
        (httpx.Response(200, text="not-json"), "malformed", DeliveryStatus.UNKNOWN),
        (httpx.Response(200, json={}), "missing id", DeliveryStatus.UNKNOWN),
    ],
)
async def test_provider_failures_return_failed(
    response: httpx.Response, expected: str, status: DeliveryStatus
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        result = await ResendTwilioNotifier(_settings(), client=client).send(
            OutboundMessage(
                channel=NotificationChannel.EMAIL,
                to_address="carrier@example.com",
                body="Message",
            )
        )
    assert result.status is status
    assert result.error and expected in result.error


async def test_transport_exception_and_missing_configuration_never_raise() -> None:
    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
        transport_failure = await ResendTwilioNotifier(_settings(), client=client).send(
            OutboundMessage(
                channel=NotificationChannel.EMAIL,
                to_address="carrier@example.com",
                body="Message",
            )
        )
    missing = await ResendTwilioNotifier(Settings()).send(
        OutboundMessage(
            channel=NotificationChannel.WHATSAPP,
            to_address="+573001112233",
            body="Message",
        )
    )
    null = await NullNotifier().send(
        OutboundMessage(
            channel=NotificationChannel.EMAIL,
            to_address="carrier@example.com",
            body="Message",
        )
    )

    assert transport_failure.status is DeliveryStatus.UNKNOWN
    assert missing.status is DeliveryStatus.FAILED
    assert null.status is DeliveryStatus.FAILED
