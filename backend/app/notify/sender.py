"""Email via Resend, WhatsApp via Twilio. Implements domain.ports.Notifier.

Neither sender raises. A failure comes back as DeliveryResult(status=FAILED) so
tools/commitments.py can leave the commitment unpromoted and raise an approval instead.
That distinction is load-bearing: a failed recap means there was no commitment, not that
there was a defective one.

STATUS: Phase 0 stub. OWNER: Track D.
"""

from app.config import Settings
from app.domain import DeliveryResult, DeliveryStatus, NotificationChannel, OutboundMessage

__all__ = ["NullNotifier", "ResendTwilioNotifier"]


class ResendTwilioNotifier:
    """Routes on message.channel: Resend for email, Twilio for WhatsApp."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        raise NotImplementedError("Track D: implement app/notify/sender.py")


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
