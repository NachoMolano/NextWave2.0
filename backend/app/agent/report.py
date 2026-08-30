"""Post-call structured extraction: evidence for review, never authorization."""

from datetime import UTC, datetime

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from app.domain import CallContext, CallReport, IncidentSubject, Severity, Turn

__all__ = ["OpenAIReportModel"]


class _AnchoredItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    offset_ms: int = Field(ge=0)


class _QuotedPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: str
    currency: str
    offset_ms: int = Field(ge=0)


class _AgreementCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterparty: str | None = None
    terms: list[str]
    offset_ms: int = Field(ge=0)


class _ReportExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    subject: IncidentSubject
    severity: Severity
    actions: list[_AnchoredItem]
    mentions: list[_AnchoredItem]
    quoted_prices: list[_QuotedPrice]
    objections: list[str]
    conditions: list[str]
    agreement_candidates: list[_AgreementCandidate]


_SYSTEM = """You extract a factual logistics call report from an anchored transcript.
Use only facts explicitly present in the supplied turns. Never infer a number, date, currency,
identity, agreement, or authority. Every action, mention, quoted price, and agreement candidate
must use the non-negative offset_ms from the exact source turn. An agreement candidate is
evidence only; it never means approved, booked, or committed. Omit anything without an anchor."""


class OpenAIReportModel:
    """OpenAI structured-output implementation of the frozen ReportModel protocol."""

    def __init__(
        self, *, api_key: str, model: str, client: AsyncOpenAI | None = None
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = client

    async def report(self, call_id: str, turns: list[Turn], context: CallContext) -> CallReport:
        if not self._model.strip():
            # An empty model id reaches the provider as
            # "The requested model '' does not exist" -- a 400 that reads like a broken
            # request rather than the unset environment variable it actually is. Name the
            # variable here, so the log says what to set instead of what OpenAI refused.
            raise RuntimeError(
                "OPENAI_REPORT_MODEL is not configured; no call brief can be generated"
            )
        transcript = "\n".join(
            f"[{turn.offset_ms if turn.offset_ms is not None else 'unanchored'} ms] "
            f"{turn.speaker}: {turn.text}"
            for turn in turns
        )
        client = self._client or AsyncOpenAI(api_key=self._api_key)
        response = await client.responses.parse(
            model=self._model,
            input=[
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Trusted call context:\n{context.model_dump_json()}\n\n"
                        f"Transcript:\n{transcript}"
                    ),
                },
            ],
            text_format=_ReportExtraction,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise ValueError("report model returned no structured output")
        return CallReport(
            call_id=call_id,
            summary=parsed.summary,
            subject=parsed.subject,
            severity=parsed.severity,
            actions=[item.model_dump() for item in parsed.actions],
            mentions=[item.model_dump() for item in parsed.mentions],
            quoted_prices=[item.model_dump() for item in parsed.quoted_prices],
            objections=parsed.objections,
            conditions=parsed.conditions,
            agreement_candidates=[item.model_dump() for item in parsed.agreement_candidates],
            model=self._model,
            generated_at=datetime.now(UTC),
        )
