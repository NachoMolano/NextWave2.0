"""Email via Resend, WhatsApp via Twilio. Implements domain.ports.Notifier.

Neither sender raises. A failure comes back as DeliveryResult(status=FAILED) so
tools/commitments.py can leave the commitment unpromoted and raise an approval instead.
That distinction is load-bearing: a failed recap means there was no commitment, not that
there was a defective one.

"""

from datetime import UTC, datetime

import httpx

from app.config import Settings
from app.domain import DeliveryResult, DeliveryStatus, NotificationChannel, OutboundMessage

__all__ = ["NullNotifier", "ResendTwilioNotifier"]


class ResendTwilioNotifier:
    """Routes on message.channel: Resend for email, Twilio for WhatsApp."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        try:
            if message.channel is NotificationChannel.EMAIL:
                return await self._send_email(message)
            return await self._send_whatsapp(message)
        except httpx.RequestError as exc:
            return _unknown(f"notification outcome unknown: {type(exc).__name__}: {exc}")
        except Exception as exc:  # configuration/program failures must cross as data
            return _failed(f"notification provider error: {type(exc).__name__}: {exc}")

    async def _send_email(self, message: OutboundMessage) -> DeliveryResult:
        if not self._settings.resend_api_key or not self._settings.notify_from_email:
            return _failed("RESEND_API_KEY or NOTIFY_FROM_EMAIL not configured")
        headers = {"Authorization": f"Bearer {self._settings.resend_api_key}"}
        payload = {
            "from": (
                f"{self._settings.notify_from_name} <{self._settings.notify_from_email}>"
            ),
            "to": [message.to_address],
            "subject": message.subject or "Volta notification",
            "text": message.body,
        }
        if self._client is not None:
            response = await self._client.post(
                "https://api.resend.com/emails", headers=headers, json=payload
            )
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    "https://api.resend.com/emails", headers=headers, json=payload
                )
        return _result(response, id_field="id")

    async def _send_whatsapp(self, message: OutboundMessage) -> DeliveryResult:
        required = (
            self._settings.twilio_account_sid,
            self._settings.twilio_auth_token,
            self._settings.twilio_whatsapp_from,
        )
        if not all(required):
            return _failed("Twilio WhatsApp credentials not configured")
        account_sid = self._settings.twilio_account_sid
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        auth = (account_sid, self._settings.twilio_auth_token)
        data = {
            "From": _whatsapp_address(self._settings.twilio_whatsapp_from),
            "To": _whatsapp_address(message.to_address),
            "Body": message.body,
        }
        if self._client is not None:
            response = await self._client.post(url, auth=auth, data=data)
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, auth=auth, data=data)
        return _result(response, id_field="sid")


class NullNotifier:
    """Used when no provider is configured. Records the intent, sends nothing.

    Returns FAILED rather than SENT on purpose. A notifier that reports success without
    sending would promote a commitment that no counterparty ever received in writing.
    """

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        channel = (
            "RESEND_API_KEY"
            if message.channel is NotificationChannel.EMAIL
            else "TWILIO_WHATSAPP_FROM"
        )
        return DeliveryResult(
            status=DeliveryStatus.FAILED,
            error=f"{channel} not configured; nothing was sent",
        )


def _result(response: httpx.Response, *, id_field: str) -> DeliveryResult:
    if not response.is_success:
        detail = response.text.strip().replace("\n", " ")[:300]
        return _failed(f"provider returned HTTP {response.status_code}: {detail}")
    try:
        provider_id = response.json().get(id_field)
    except (ValueError, AttributeError):
        return _unknown("provider returned malformed success response")
    if not isinstance(provider_id, str) or not provider_id:
        return _unknown(f"provider success response missing {id_field}")
    return DeliveryResult(
        status=DeliveryStatus.SENT,
        provider_message_id=provider_id,
        sent_at=datetime.now(UTC),
    )


def _failed(error: str) -> DeliveryResult:
    return DeliveryResult(status=DeliveryStatus.FAILED, error=error)


def _unknown(error: str) -> DeliveryResult:
    return DeliveryResult(status=DeliveryStatus.UNKNOWN, error=error)


def _whatsapp_address(address: str) -> str:
    return address if address.startswith("whatsapp:") else f"whatsapp:{address}"
