"""The composition root. The only place that knows which implementation is which.

MAY IMPORT:  everything.
IMPORTED BY: nothing.

Every dependency is built here and injected. No handler constructs a client of its own,
which is what lets the whole system run against fakes in a test and against Supabase and
Vapi in a demo without a single conditional inside the business logic.

/health must work when nothing else does. It is the endpoint you need most when the database
is unreachable, so construction of the store and the placer never touches the network --
they fail on first use, not at boot.

OWNER: Track E.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, FastAPI

from app import jobs
from app.api.routes import create_api_router
from app.config import Settings, get_settings
from app.domain import CallPlacer, Notifier, Store
from app.notify.sender import NullNotifier, ResendTwilioNotifier
from app.store.supabase import SupabaseStore
from app.tools.calls import CallLedger
from app.tools.commitments import CommitmentCoordinator
from app.tools.market import Market
from app.tools.model import ModelTools
from app.vapi.client import VapiCallPlacer
from app.vapi.toolserver import create_tool_router
from app.vapi.webhook import create_webhook_router

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


def build_tools(store: Store, notifier: Notifier) -> ModelTools:
    """Assemble the model-facing surface from the pieces two tracks own.

    ModelTools takes the ledger and the coordinator rather than reaching for them, so the
    anchor is measured by the one component that knows how, and the commitments table keeps
    exactly one writer.
    """
    return ModelTools(
        store,
        now=now_utc,
        ledger=CallLedger(store, now=now_utc),
        commitments=CommitmentCoordinator(store, notifier, now=now_utc),
    )


def _mount(app: FastAPI, factory: object, prefix: str, owner: str) -> bool:
    """Mount a router if its track has built it; log and carry on if it has not.

    Tracks B and C land after this one, and their factories raise NotImplementedError until
    they do. Letting that propagate would mean the server does not boot -- so the surface
    that exists stays up, the clock keeps running, and the gap is named in the log rather
    than hidden. This whole function disappears once CP4 and CP3 are in.
    """
    try:
        router = factory()  # type: ignore[operator]
    except NotImplementedError:
        log.warning("router.not_built", prefix=prefix, owner=owner)
        return False
    if isinstance(router, APIRouter):
        app.include_router(router, prefix=prefix)
        return True
    log.warning("router.not_a_router", prefix=prefix, owner=owner)
    return False


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = build_store(settings)
    placer = build_placer(settings)
    notifier = build_notifier(settings)

    # Built here so they exist for the routers to close over, and so a handler can never
    # construct one of its own with a different clock.
    app_state = {
        "store": store,
        "placer": placer,
        "notifier": notifier,
        "tools": build_tools(store, notifier),
        "market": Market(store, now=now_utc),
        "ledger": CallLedger(store, now=now_utc),
        "commitments": CommitmentCoordinator(store, notifier, now=now_utc),
        "now": now_utc,
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(jobs.run_forever(store, placer, settings, now=now_utc))
        log.info("jobs.started", interval_seconds=settings.sweep_interval_seconds)
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Volta", version="0.1.0", lifespan=lifespan)
    app.state.volta = app_state

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "environment": settings.environment}

    _mount(app, create_webhook_router, "/vapi", "Track B")
    _mount(app, create_tool_router, "/vapi", "Track B")
    _mount(app, create_api_router, "/api", "Track C")

    return app


app = create_app()
