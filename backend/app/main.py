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
from typing import cast

import structlog
from fastapi import FastAPI

from app import jobs
from app.agent.report import OpenAIReportModel
from app.api import PortalStore, create_api_router
from app.config import Settings, get_settings
from app.domain import CallPlacer, DialPlan, Notifier, Store
from app.notify.sender import NullNotifier, ResendTwilioNotifier
from app.store.supabase import SupabaseStore
from app.tools.calls import CallLedger
from app.tools.commitments import CommitmentCoordinator
from app.tools.market import Market
from app.tools.model import ModelTools
from app.vapi.assistant import build_assistant, profile_from_settings
from app.vapi.campaign import run_campaign
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


def build_store(settings: Settings) -> SupabaseStore:
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


def build_tools(
    store: Store,
    *,
    ledger: CallLedger,
    commitments: CommitmentCoordinator,
) -> ModelTools:
    """Assemble the model-facing surface from the pieces two tracks own.

    ModelTools takes the ledger and the coordinator rather than reaching for them, so the
    anchor is measured by the one component that knows how, and the commitments table keeps
    exactly one writer.
    """
    return ModelTools(
        store,
        now=now_utc,
        ledger=ledger,
        commitments=commitments,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    production_errors = settings.production_errors()
    if production_errors:
        raise RuntimeError(
            "production configuration is incomplete: " + ", ".join(production_errors)
        )
    store = build_store(settings)
    placer = build_placer(settings)
    notifier = build_notifier(settings)

    ledger = CallLedger(store, now=now_utc)
    commitments = CommitmentCoordinator(store, notifier, now=now_utc)
    profile = profile_from_settings(settings)
    reporter = OpenAIReportModel(
        api_key=settings.openai_api_key,
        model=settings.openai_report_model,
    )
    model_tools = build_tools(store, ledger=ledger, commitments=commitments)
    market = Market(store, now=now_utc)

    async def dial(
        plans: list[DialPlan],
        call_placer: CallPlacer,
        call_settings: Settings,
    ) -> dict[str, str]:
        """Dial, then bind each planned row to the call that was actually placed.

        The write-back lives here rather than in ``run_campaign`` because ``vapi/`` may not
        import ``store/``. Without it the planned row keeps its ``pending:`` placeholder, the
        webhook opens a second row under the real id carrying no order and no context, and
        every tool call in the conversation correlates to that empty one.
        """
        placed = await run_campaign(plans, call_placer, call_settings, profile=profile)
        for call_id, vapi_call_id in placed.items():
            try:
                await store.attach_vapi_call_id(call_id, vapi_call_id)
            except Exception:
                # The call is already ringing; failing here would strand it entirely. Log
                # loudly -- this row is now the split-evidence case until someone repairs it.
                log.exception("call.correlation_failed", call_id=call_id, vapi_call_id=vapi_call_id)
        return placed

    async def dial_plans(plans: list[DialPlan]) -> object:
        """The portal's dialler, with the placer and settings already bound."""
        return await dial(plans, placer, settings)

    async def sweep() -> list[str]:
        return await jobs.sweep_deadlines(store, placer, settings, now=now_utc, dial=dial)

    # Built here so the same capability instances are injected into every router. In
    # particular, webhook lifecycle events and model tools share one CallLedger.
    app_state = {
        "store": store,
        "placer": placer,
        "notifier": notifier,
        "tools": model_tools,
        "market": market,
        "ledger": ledger,
        "commitments": commitments,
        "profile": profile,
        "reporter": reporter,
        "now": now_utc,
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(
            jobs.run_forever(store, placer, settings, now=now_utc, dial=dial)
        )
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

    app.include_router(
        create_tool_router(model_tools, store, server_secret=settings.vapi_server_secret),
        prefix="/vapi",
    )
    app.include_router(
        create_webhook_router(
            store=store,
            ledger=ledger,
            reporter=reporter,
            profile=profile,
            build_assistant_for=lambda context: build_assistant(profile, context, settings),
            escalation_number=settings.escalation_phone_number,
            server_secret=settings.vapi_server_secret,
            now=now_utc,
        ),
        prefix="/vapi",
    )
    app.include_router(
        create_api_router(
            cast(PortalStore, store),
            market=market,
            sweep=sweep,
            dial=dial_plans,
            now=now_utc,
            settings=settings,
        ),
        prefix="/api",
    )

    return app


app = create_app()
