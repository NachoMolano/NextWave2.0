"""POST https://api.vapi.ai/call. Implements domain.ports.CallPlacer.

The only code in the repository that spends money. NEVER call it from a test: a real call
costs credits and can dial a real number. tests/fakes.py::FakeCallPlacer exists for that.

The assistant is passed transient rather than as an assistantId, so the prompt composed by
agent/prompts.py is the single source of truth and no call depends on dashboard config that
can drift between the rehearsal and the demo.

STATUS: Phase 0 stub. OWNER: Track B.
"""

from app.config import Settings

__all__ = ["VapiCallPlacer"]


class VapiCallPlacer:
    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.vapi_api_key
        self._phone_number_id = settings.vapi_phone_number_id

    async def place(self, assistant: dict[str, object], to_number: str) -> str:
        """Dial to_number with a transient assistant. Returns the Vapi call id.

        Body: {"assistant": {...}, "phoneNumberId": ..., "customer": {"number": to_number}}.
        A response with status "queued" and concurrencyBlocked true is a retry, not an error.
        """
        raise NotImplementedError("Track B: implement app/vapi/client.py")
