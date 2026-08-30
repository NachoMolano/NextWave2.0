"""Settings. The only module in the codebase that reads the environment.

MAY IMPORT:  stdlib, pydantic-settings. Nothing from app.
IMPORTED BY: store, notify, vapi, api, jobs, main.

A leaf, like domain/. Centralised so a missing key fails at startup rather than three hours
later, mid-call, when the recap tries to send.

Every value defaults to empty rather than to something plausible. A default API key that
silently does nothing is worse than a crash, and a default price cap would be a mandate
nobody granted.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Voice and telephony (Vapi) ---
    vapi_api_key: str = ""
    vapi_phone_number_id: str = ""
    #: Sent by Vapi as the X-Vapi-Secret header. Verified before a webhook body is parsed.
    vapi_server_secret: str = ""
    #: Model, voice and transcriber ids move, and most tutorials online are stale. Verify
    #: each against current Vapi docs before filling these in; never hardcode one in source.
    vapi_model: str = ""
    vapi_voice_id: str = ""
    vapi_transcriber: str = ""
    #: Seconds Vapi will wait for our tool server. The maximum is 300; the default is not
    #: documented, so it is set explicitly rather than inherited.
    vapi_tool_timeout_seconds: int = 20
    #: Which smart-endpointing model decides a caller has finished speaking. Empty derives it
    #: from the primary language, because 'livekit' is English-only and degrades silently on
    #: anything else. Set it to override; the ids move, so verify against current Vapi docs.
    vapi_endpointing_provider: str = ""
    #: Final pause before the agent speaks, on top of whatever endpointing decided. Raise it
    #: if the agent still clips the ends of sentences; lower it if the call feels dead.
    vapi_start_speaking_wait_seconds: float = 0.4

    # --- Persistence ---
    supabase_url: str = ""
    supabase_secret_key: str = ""

    # --- Post-call extraction only. Never in the conversational path. ---
    openai_api_key: str = ""
    openai_report_model: str = ""

    # --- The outbox ---
    resend_api_key: str = ""
    notify_from_email: str = ""
    notify_from_name: str = "Volta"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""
    manager_email: str = ""
    manager_whatsapp: str = ""

    # --- Portal audit identity ---
    #: The portal is deliberately unauthenticated. This label is written to mandate and
    #: approval audit rows so actions remain attributable to the running deployment.
    portal_manager_identity: str = ""

    # --- Escalation ---
    #: Where a live call is transferred when the agent hands off to a person.
    escalation_phone_number: str = ""

    # --- The company the agent works for. Rendered into every prompt. ---
    company_name: str = "Pacific Textiles"
    company_business_type: str = "importer"
    company_city: str = "Guadalajara"
    company_country: str = "Mexico"
    company_currency: str = "USD"
    company_timezone: str = "America/Mexico_City"
    company_primary_language: str = "en"
    company_fallback_language: str = "es-MX"
    agent_name: str = "Volta"
    agent_role: str = "transport coordinator"

    # --- Orchestration ---
    #: How many carriers one RFQ dials. The brief requires at least three.
    #: 0 means every active, on-file carrier. Set a positive value to cap a test campaign.
    rfq_carrier_count: int = 0
    #: Vapi allows 10 concurrent calls by default; stay under it so a retry has room.
    max_concurrent_calls: int = 8
    #: How long a market stays open before jobs.py ranks what it has.
    rfq_timeout_minutes: int = 15
    #: How often the deadline sweep runs. OUTBOUND 2.
    sweep_interval_seconds: int = 60
    strict_conversation_security: bool = False

    public_base_url: str = ""
    #: Recording is opt-in. Enabling it requires an approved consent/retention process.
    recording_enabled: bool = False
    recording_consent_notice: str = ""
    production_retention_ready: bool = False
    production_provider_deletion_ready: bool = False
    production_legal_review_ready: bool = False
    environment: Literal["local", "demo", "production"] = "local"

    def missing_keys(self) -> tuple[str, ...]:
        """Keys a deployed environment needs to do its job, by name -- never a value.

        Not production-only. A ``demo`` deployment places real calls to real carriers and
        sends real email, so every key below is load-bearing there too. Discovering that one
        was empty by reading a traceback -- which is how ``OPENAI_REPORT_MODEL`` was found,
        after a fortnight of every call brief failing -- is what this exists to prevent.

        ``local`` is exempt: running the suite or a simulated call needs none of it.
        """
        if self.environment == "local":
            return ()
        required = {
            "VAPI_API_KEY": self.vapi_api_key,
            "VAPI_PHONE_NUMBER_ID": self.vapi_phone_number_id,
            "VAPI_SERVER_SECRET": self.vapi_server_secret,
            "SUPABASE_URL": self.supabase_url,
            "SUPABASE_SECRET_KEY": self.supabase_secret_key,
            "PUBLIC_BASE_URL": self.public_base_url,
            "VAPI_MODEL": self.vapi_model,
            "VAPI_VOICE_ID": self.vapi_voice_id,
            "VAPI_TRANSCRIBER": self.vapi_transcriber,
            "OPENAI_API_KEY": self.openai_api_key,
            "OPENAI_REPORT_MODEL": self.openai_report_model,
            "RESEND_API_KEY": self.resend_api_key,
            "NOTIFY_FROM_EMAIL": self.notify_from_email,
            "MANAGER_EMAIL": self.manager_email,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if self.recording_enabled and not self.recording_consent_notice.strip():
            missing.append("RECORDING_CONSENT_NOTICE")
        return tuple(missing)

    def production_errors(self) -> tuple[str, ...]:
        """Return what stops production from starting, without exposing secret values.

        Only ``production`` refuses to boot. A ``demo`` deployment logs its gaps loudly at
        startup instead -- see ``main.create_app`` -- because a demo backend that will not
        start is a worse failure than one running with a known, logged hole in it.
        """
        if self.environment != "production":
            return ()
        gates = {
            "PRODUCTION_RETENTION_READY": self.production_retention_ready,
            "PRODUCTION_PROVIDER_DELETION_READY": self.production_provider_deletion_ready,
            "PRODUCTION_LEGAL_REVIEW_READY": self.production_legal_review_ready,
        }
        return self.missing_keys() + tuple(name for name, ready in gates.items() if not ready)


@lru_cache
def get_settings() -> Settings:
    return Settings()
