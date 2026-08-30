"""Immutable values crossing Volta's untrusted/trusted security boundary."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CommitmentMode(StrEnum):
    AUTONOMOUS = "autonomous"
    HUMAN_ESCALATION = "human_escalation"


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


class ReasonCode(StrEnum):
    ALLOWED = "allowed"
    OUTSIDE_MANDATE = "outside_mandate"
    INCOMPLETE_COST = "incomplete_cost"
    CURRENCY_UNSUPPORTED = "currency_unsupported"
    FX_EVIDENCE_MISSING = "fx_evidence_missing"
    STALE_EVIDENCE = "stale_evidence"
    INVALID_WINDOW = "invalid_window"
    MANDATE_MISMATCH = "mandate_mismatch"
    EVIDENCE_MISSING = "evidence_missing"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"
    NO_ELIGIBLE_CANDIDATE = "no_eligible_candidate"
    REPLAYED_EVENT = "replayed_event"
    CONFLICTING_STATE = "conflicting_state"


class CostComponent(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=80)
    amount: Decimal = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def canonical_currency(cls, value: str) -> str:
        if not value.isalpha():
            raise ValueError("currency must be an ISO 4217 alphabetic code")
        return value.upper()


class FxSnapshot(BaseModel):
    """How many USD one unit of quote currency bought at the observed instant."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(min_length=1)
    quote_currency: str = Field(min_length=3, max_length=3)
    usd_per_unit: Decimal = Field(gt=0)
    observed_at: datetime
    source: str = Field(min_length=1)

    @field_validator("quote_currency")
    @classmethod
    def canonical_currency(cls, value: str) -> str:
        return value.upper()


class Mandate(BaseModel):
    model_config = ConfigDict(frozen=True)

    mandate_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    owner_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    max_all_in_usd: Decimal = Field(gt=0)
    pickup_not_before: datetime
    pickup_not_after: datetime
    allowed_equipment: frozenset[str] = Field(min_length=1)
    commitment_mode: CommitmentMode
    fx_margin_bps: int | None = Field(default=None, ge=0)


class QuoteProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    carrier_id: str = Field(min_length=1)
    carrier_contact_id: str = Field(min_length=1)
    components: tuple[CostComponent, ...] = Field(min_length=1)
    cost_is_final: bool
    pickup_at: datetime
    pickup_is_date_only: bool = Field(
        default=False,
        description="True when the carrier named a day and no clock time. False means the "
        "instant is real and is judged to the minute. The default is the strict reading, so "
        "a caller that cannot tell the difference never loosens the window by omission.",
    )
    equipment: str = Field(min_length=1)
    valid_until: datetime
    source_call_id: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    transcript_anchor_ms: int | None = Field(default=None, ge=0)
    carrier_confirmed_exact_recap: bool = False
    confirmed_at: datetime | None = None


class CostEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    original_totals: dict[str, Decimal]
    unbuffered_usd: Decimal
    margin_bps: int
    buffered_usd: Decimal
    fx_snapshot_ids: tuple[str, ...]


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: PolicyOutcome
    reason: ReasonCode
    mandate_id: str
    mandate_version: int
    proposal_id: str
    cost: CostEvidence | None = None


class PreparedCommitment(BaseModel):
    model_config = ConfigDict(frozen=True)

    commitment_id: str
    proposal_id: str
    mandate_id: str
    mandate_version: int
    canonical_payload_sha256: str
    prepared_at: datetime
    expires_at: datetime
    human_approval_id: str | None = None
