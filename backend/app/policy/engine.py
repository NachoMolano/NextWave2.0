"""Pure reference monitor. No model, network, persistence, or ambient clock access.

Every function here is total: it always returns a decision, and the default is deny. A
technical failure must never degrade into permission, so there is no code path that raises
past a caller and leaves "allowed" as the effective outcome.

``now`` is a parameter, never ``datetime.now()``. A decision that depends on an ambient
clock cannot be reproduced from its stored inputs, and a decision that cannot be reproduced
cannot be defended.

STATUS: Phase 0 stub. Track A ports the implementation from
``nextwave/backend/app/policy/engine.py`` and adds tests/test_policy.py.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta

from app.domain import (
    FxSnapshot,
    Mandate,
    PolicyDecision,
    QuoteProposal,
)

__all__ = ["evaluate_quote", "require_preagreement_evidence", "select_best"]


def evaluate_quote(
    mandate: Mandate,
    proposal: QuoteProposal,
    fx: Mapping[str, FxSnapshot],
    *,
    now: datetime,
    max_fx_age: timedelta = timedelta(hours=2),
) -> PolicyDecision:
    """Evaluate one proposal against an immutable mandate and evidence snapshots.

    Order of checks matters and is part of the contract: mandate match, cost finality,
    validity, pickup window, equipment, then the all-in conversion against the ceiling.
    """
    raise NotImplementedError("Track A: port from nextwave/backend/app/policy/engine.py")


def require_preagreement_evidence(
    mandate: Mandate, proposal: QuoteProposal, decision: PolicyDecision
) -> PolicyDecision:
    """A model-interpreted yes is not enough without exact, anchored recap evidence."""
    raise NotImplementedError("Track A: port from nextwave/backend/app/policy/engine.py")


def select_best(decisions: list[tuple[QuoteProposal, PolicyDecision]]) -> QuoteProposal | None:
    """Lowest eligible all-in cost, with deterministic tie-breaks.

    Deterministic because the comparison has to be reproducible in front of a human who
    asks why this carrier and not that one.
    """
    raise NotImplementedError("Track A: port from nextwave/backend/app/policy/engine.py")
