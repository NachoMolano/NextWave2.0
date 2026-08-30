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
    rfq_carrier_count: int = 3
    #: Vapi allows 10 concurrent calls by default; stay under it so a retry has room.
    max_concurrent_calls: int = 8
    #: How long a market stays open before jobs.py ranks what it has.
    rfq_timeout_minutes: int = 15
    #: How often the deadline sweep runs. OUTBOUND 2.
    sweep_interval_seconds: int = 60

    public_base_url: str = ""
    environment: Literal["local", "demo", "production"] = "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
