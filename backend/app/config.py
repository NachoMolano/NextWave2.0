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

    # --- Human portal authority ---
    #: Authentication for every /api route. Request bodies never get to choose their own
    #: audit actor: the identity comes from the credential, because a name typed into a form
    #: authenticates nothing.
    #:
    #: One token per person, as ``token:identity`` pairs separated by commas or newlines:
    #:
    #:     PORTAL_TOKENS=k7f...:maria@volta.mx,q2p...:diego@volta.mx
    #:
    #: This is the difference between an audit trail that means something and one that only
    #: looks like it does. With a shared token, "maria@volta.mx approved this" really means
    #: "somebody holding the shared token approved this" -- a human-looking address claiming
    #: more accountability than the system can back. Per-person tokens make the same row true,
    #: and let one person's access be revoked without rotating everyone's.
    portal_tokens: str = ""

    #: The single-token fallback. Still honoured so an existing deployment keeps working, but
    #: PORTAL_TOKENS is the one to reach for.
    portal_api_token: str = ""
    portal_manager_identity: str = ""

    #: Below this a token is a password, not a secret. Not enforced -- refusing to boot over a
    #: policy judgement in the middle of a demo is the wrong trade -- but it is logged loudly
    #: at startup, because a short token on a public URL is a real exposure and a silent one.
    portal_minimum_token_length: int = 24

    def portal_identities(self) -> dict[str, str]:
        """token -> the identity it acts as. Empty when the portal is unconfigured."""
        pairs: dict[str, str] = {}
        for entry in self.portal_tokens.replace("\n", ",").split(","):
            token, separator, identity = entry.strip().partition(":")
            if separator and token.strip() and identity.strip():
                pairs[token.strip()] = identity.strip()
        if not pairs and self.portal_api_token.strip() and self.portal_manager_identity.strip():
            pairs[self.portal_api_token.strip()] = self.portal_manager_identity.strip()
        return pairs

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
    #: Recording is opt-in. Enabling it requires an approved consent/retention process.
    recording_enabled: bool = False
    recording_consent_notice: str = ""
    production_tenant_auth_ready: bool = False
    production_retention_ready: bool = False
    production_provider_deletion_ready: bool = False
    production_legal_review_ready: bool = False
    environment: Literal["local", "demo", "production"] = "local"

    def production_errors(self) -> tuple[str, ...]:
        """Return missing production gates without exposing secret values."""
        if self.environment != "production":
            return ()
        required = {
            "VAPI_API_KEY": self.vapi_api_key,
            "VAPI_PHONE_NUMBER_ID": self.vapi_phone_number_id,
            "VAPI_SERVER_SECRET": self.vapi_server_secret,
            "SUPABASE_URL": self.supabase_url,
            "SUPABASE_SECRET_KEY": self.supabase_secret_key,
            "PORTAL_API_TOKEN": self.portal_api_token,
            "PORTAL_MANAGER_IDENTITY": self.portal_manager_identity,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if self.recording_enabled and not self.recording_consent_notice.strip():
            missing.append("RECORDING_CONSENT_NOTICE")
        gates = {
            "PRODUCTION_TENANT_AUTH_READY": self.production_tenant_auth_ready,
            "PRODUCTION_RETENTION_READY": self.production_retention_ready,
            "PRODUCTION_PROVIDER_DELETION_READY": self.production_provider_deletion_ready,
            "PRODUCTION_LEGAL_REVIEW_READY": self.production_legal_review_ready,
        }
        missing.extend(name for name, ready in gates.items() if not ready)
        return tuple(missing)


@lru_cache
def get_settings() -> Settings:
    return Settings()
