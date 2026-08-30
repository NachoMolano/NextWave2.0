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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import cast

import structlog
from fastapi import FastAPI

from app import jobs
from app.agent.report import OpenAIReportModel
from app.api import PortalStore, create_api_router
from app.config import Settings, get_settings
from app.domain import (
    Approval,
    ApprovalKind,
    ApprovalReason,
    CallPhase,
    CallPlacer,
    CallRecord,
    CallReport,
    CommitmentState,
    Comparison,
    DialPlan,
    EventRow,
    Notifier,
    Order,
    Store,
)
from app.notify.render import (
    render_award_request_with_minutes,
    render_commitment_email,
    render_incident_report,
    render_not_selected_email,
)
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

__all__ = ["build_after_report", "build_store", "create_app", "now_utc"]

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


def build_after_report(
    store: Store,
    notifier: Notifier,
    commitments: CommitmentCoordinator,
    settings: Settings,
    *,
    now: Callable[[], datetime],
) -> Callable[[CallRecord, CallReport], Awaitable[None]]:
    """Compose post-call notifications without granting the report any authority."""

    async def raise_notification_escalation(call: CallRecord, detail: str) -> None:
        await store.raise_approval(
            Approval(
                order_id=call.order_id,
                call_id=call.id,
                kind=ApprovalKind.ESCALATION,
                reason=ApprovalReason.POLICY_FAILURE,
                context={"detail": detail},
                raised_at=now(),
            )
        )

    async def after_report(call: CallRecord, report: CallReport) -> None:
        if call.phase == CallPhase.AWARD.value and call.order_id:
            commitment = await store.live_commitment(call.order_id)
            if (
                commitment is None
                or commitment.id is None
                or commitment.state is not CommitmentState.VERBAL
                or commitment.evidence_call_id != call.id
            ):
                # The award call ended and confirmed nothing. That is a legitimate outcome --
                # the carrier's terms had changed, the recap was never assented to -- but it
                # used to return here in silence, leaving the order in AWARDING, the carrier
                # never confirmed, no email owed to anybody, and a portal with nothing on it
                # to say so. The only person who can move it now is a person, so say so.
                await raise_notification_escalation(
                    call,
                    "the award call ended with no confirmed pre-agreement; nothing is "
                    "committed and no written confirmation was sent",
                )
                await store.append_event(
                    EventRow(
                        order_id=call.order_id,
                        call_id=call.id,
                        type="award.unconfirmed",
                        payload={"call_id": call.id or ""},
                        idempotency_key=f"award-unconfirmed:{call.id}",
                    )
                )
                return
            carrier = await store.carrier(call.carrier_id) if call.carrier_id else None
            if carrier is None or not carrier.email:
                await raise_notification_escalation(
                    call, "the awarded carrier has no email; the written recap was not sent"
                )
                return
            message = render_commitment_email(
                commitment, report, carrier.email, call.recording_url
            )
            await commitments.send_recap_and_promote(commitment.id, message)
            return

        if call.phase not in {CallPhase.INBOUND.value, CallPhase.STATUS_CHECK.value}:
            return
        for address, whatsapp in (
            (settings.manager_email.strip(), False),
            (settings.manager_whatsapp.strip(), True),
        ):
            if not address:
                continue
            channel = "whatsapp" if whatsapp else "email"
            claimed = await store.append_event(
                EventRow(
                    order_id=call.order_id,
                    call_id=call.id,
                    type="incident.notification_claimed",
                    payload={"channel": channel},
                    idempotency_key=f"incident-notification:{call.id}:{channel}",
                )
            )
            if not claimed:
                continue
            message = render_incident_report(report, address, whatsapp=whatsapp)
            result = await notifier.send(message)
            await store.record_delivery(message, result)

    return after_report


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    production_errors = settings.production_errors()
    if production_errors:
        raise RuntimeError(
            "production configuration is incomplete: " + ", ".join(production_errors)
        )
    missing_keys = settings.missing_keys()
    if missing_keys:
        # A demo deployment boots anyway -- a backend that refuses to start minutes before a
        # demo is worse than one running with a hole in it -- but the hole is now the first
        # thing in the log rather than something found weeks later in a traceback. Names
        # only; a value here would put a secret in the deployment log.
        log.error("config.incomplete", environment=settings.environment, missing=list(missing_keys))
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

    async def alert_award(comparison: Comparison, approval: Approval) -> None:
        if not settings.manager_email.strip():
            log.warning("award.alert_skipped", detail="MANAGER_EMAIL is not configured")
            return
        minutes: list[tuple[str, CallReport | None]] = []
        for entry in comparison.entries:
            quote = await store.quote(entry.quote_id)
            report = await store.report_for(quote.call_id) if quote is not None else None
            minutes.append((entry.carrier_name, report))
        message = render_award_request_with_minutes(
            comparison, settings.manager_email, minutes
        ).model_copy(
            update={"approval_id": approval.id}
        )
        result = await notifier.send(message)
        await store.record_delivery(message, result)

    async def notify_award_decision(order: Order, winner_quote_id: str) -> None:
        quotes = await store.quotes_for(order.id)
        winner = await store.quote(winner_quote_id)
        if winner is None:
            return
        notified: set[str] = set()
        for quote in quotes:
            if quote.carrier_id == winner.carrier_id or quote.carrier_id in notified:
                continue
            carrier = await store.carrier(quote.carrier_id)
            if carrier is None or not carrier.email:
                continue
            claimed = await store.append_event(
                EventRow(
                    order_id=order.id,
                    type="award.not_selected_notification_claimed",
                    payload={"carrier_id": carrier.id, "winner_quote_id": winner_quote_id},
                    idempotency_key=f"award-not-selected:{order.id}:{carrier.id}:{winner_quote_id}",
                )
            )
            if not claimed:
                continue
            notified.add(carrier.id)
            message = render_not_selected_email(
                order_id=order.id,
                reference=order.reference,
                carrier_name=carrier.name,
                to_address=carrier.email,
            )
            result = await notifier.send(message)
            await store.record_delivery(message, result)

    after_report = build_after_report(store, notifier, commitments, settings, now=now_utc)

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
            jobs.run_forever(store, placer, settings, now=now_utc, dial=dial, alert=alert_award)
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
    async def health() -> dict[str, object]:
        # Names of unset keys, never values. /health answering a flat "ok" while every call
        # brief and every written confirmation was failing on empty configuration is how the
        # gap stayed invisible; one curl should now show it. Still 200 -- Render's health
        # check points here, and an incomplete demo is running, not down.
        body: dict[str, object] = {"status": "ok", "environment": settings.environment}
        if missing_keys:
            body["config"] = "incomplete"
            body["missing"] = list(missing_keys)
        return body

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
            after_report=after_report,
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
            notifier=notifier,
            notify_award_decision=notify_award_decision,
        ),
        prefix="/api",
    )

    return app


app = create_app()
