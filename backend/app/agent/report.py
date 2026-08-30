"""Post-call extraction. Implements domain.ports.ReportModel.

Runs after the call with no latency budget, so it may use a slower model freely. What it
returns is evidence: agreement *candidates*, each carrying the offset at which it was said.
The model proposes; policy decides whether a candidate ever becomes a commitment.

The credentials arrive as arguments rather than from config, because agent/ may import only
domain. That is not bookkeeping: agent/ is the untrusted layer -- it holds the text a
counterparty can argue with -- and a layer that cannot read the environment cannot be
reconfigured at a distance by anything that reaches it.

STATUS: Phase 0 stub. OWNER: Track D.
"""

from app.domain import CallContext, CallReport, Turn

__all__ = ["OpenAIReportModel"]


class OpenAIReportModel:
    def __init__(self, *, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def report(self, call_id: str, turns: list[Turn], context: CallContext) -> CallReport:
        raise NotImplementedError("Track D: implement app/agent/report.py")
