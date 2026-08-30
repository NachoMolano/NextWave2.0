"""POST https://api.vapi.ai/call. Implements domain.ports.CallPlacer.

The only code in the repository that spends money. NEVER call it from a test: a real call
costs credits and can dial a real number. tests/fakes.py::FakeCallPlacer exists for that.

The assistant is passed transient rather than as an assistantId, so the prompt composed by
agent/prompts.py is the single source of truth and no call depends on dashboard config that
can drift between the rehearsal and the demo.

Two failures are told apart on purpose. A call that comes back ``queued`` with
``concurrencyBlocked`` has not failed -- Vapi is holding it because every slot is busy, and
the right answer is to wait and ask again. Everything else is a real failure and must not be
retried blindly: a retry of a request that may already have dialled is a second phone call to
a real carrier.

STATUS: built. OWNER: Track B.
"""

from typing import Any

import httpx
import structlog

from app.config import Settings

__all__ = ["CallPlacementError", "ConcurrencyBlocked", "VapiCallPlacer"]

log = structlog.get_logger(__name__)

_CALLS_URL = "https://api.vapi.ai/call"

#: Generous, because the request returns as soon as the call is queued rather than when
#: anybody answers. A short timeout here would abandon a request that has already dialled.
_TIMEOUT_SECONDS = 30.0


class CallPlacementError(Exception):
    """Vapi refused or could not be reached. The call was not placed, or we cannot say."""


class ConcurrencyBlocked(CallPlacementError):
    """Every concurrent slot is busy. Not an error -- a "later". Retried by campaign.py."""


class VapiCallPlacer:
    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.vapi_api_key
        self._phone_number_id = settings.vapi_phone_number_id

    async def place(self, assistant: dict[str, object], to_number: str) -> str:
        """Dial to_number with a transient assistant. Returns the Vapi call id.

        Body: {"assistant": {...}, "phoneNumberId": ..., "customer": {"number": to_number}}.
        A response with status "queued" and concurrencyBlocked true is a retry, not an error.
        """
        if not self._api_key or not self._phone_number_id:
            # Refuse before the request rather than send an unauthenticated one. An
            # unconfigured placer in a demo should fail loudly at the first dial, not
            # produce three silent 401s while somebody watches an empty dashboard.
            raise CallPlacementError(
                "VAPI_API_KEY and VAPI_PHONE_NUMBER_ID must both be set before a call is placed"
            )

        body: dict[str, Any] = {
            "assistant": assistant,
            "phoneNumberId": self._phone_number_id,
            "customer": {"number": to_number},
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    _CALLS_URL,
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.HTTPError as exc:
            raise CallPlacementError(f"could not reach Vapi: {exc}") from exc

        if response.status_code >= 400:
            raise CallPlacementError(
                f"Vapi refused the call with {response.status_code}: {response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise CallPlacementError("Vapi returned a body that is not JSON") from exc

        if not isinstance(payload, dict):
            raise CallPlacementError(f"Vapi returned {type(payload).__name__}, not an object")

        if payload.get("status") == "queued" and payload.get("concurrencyBlocked"):
            raise ConcurrencyBlocked(
                f"all concurrent slots are busy; call to {to_number} was not started"
            )

        call_id = payload.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise CallPlacementError("Vapi accepted the call but returned no id")

        log.info("vapi.call.placed", vapi_call_id=call_id, status=payload.get("status"))
        return call_id
