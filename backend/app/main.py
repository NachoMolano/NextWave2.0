"""The composition root. The only place that knows which implementation is which.

MAY IMPORT:  everything.
IMPORTED BY: nothing.

Every dependency is built here and injected. No handler constructs a client of its own,
which is what lets the whole system run against fakes in a test and against Supabase and
Vapi in a demo without a single conditional inside the business logic.

/health must work when nothing else does. It is the endpoint you need most when the database
is unreachable, so construction of the store and the placer never touches the network --
they fail on first use, not at boot.

STATUS: Phase 0 gives the factory and /health. Track E wires the routers and the jobs loop.
"""

from datetime import UTC, datetime

import structlog
from fastapi import FastAPI

from app.config import Settings, get_settings
from app.domain import CallPlacer, Notifier, Store
from app.notify.sender import NullNotifier, ResendTwilioNotifier
from app.store.supabase import SupabaseStore
from app.vapi.client import VapiCallPlacer

__all__ = ["build_store", "create_app", "now_utc"]

log = structlog.get_logger(__name__)


def now_utc() -> datetime:
    """The single source of the current time.

    Injected everywhere rather than called in place, so a scenario can be replayed and
    produce the same policy decisions it produced live.
    """
    return datetime.now(UTC)


def build_store(settings: Settings) -> Store:
    return SupabaseStore(settings)


def build_placer(settings: Settings) -> CallPlacer:
    return VapiCallPlacer(settings)


def build_notifier(settings: Settings) -> Notifier:
    """Fall back to the null sender when no provider is configured.

    NullNotifier reports FAILED, never SENT, so an unconfigured demo cannot promote a
    commitment that no carrier ever received in writing.
    """
    if settings.resend_api_key and settings.notify_from_email:
        return ResendTwilioNotifier(settings)
    log.warning("notify.unconfigured", detail="recaps will report FAILED; nothing will send")
    return NullNotifier()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Volta", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "environment": settings.environment}

    # Track B: app.include_router(create_webhook_router(...), prefix="/vapi")
    # Track B: app.include_router(create_tool_router(...), prefix="/vapi")
    # Track C: app.include_router(create_api_router(...), prefix="/api")
    # Track E: start the jobs.run_forever loop on startup

    return app


app = create_app()
