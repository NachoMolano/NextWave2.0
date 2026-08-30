"""Deterministic authority. The reference monitor.

MAY IMPORT:  domain. Nothing else -- not store, not notify, not agent, not vapi.
IMPORTED BY: tools.

This package is a sink in the dependency graph, and that is the whole design. Because it
cannot import vapi/ it cannot call a model. Because it cannot import store/ or notify/ it
cannot reach the network. Because it cannot import agent/ no prompt text can reach it.

"The LLM never writes a commitment" is therefore a property of the import graph, checked on
every test run, rather than a rule somebody has to remember at four in the morning.

OWNER: Track A.
"""

from app.policy.engine import evaluate_quote, require_preagreement_evidence, select_best

__all__ = ["evaluate_quote", "require_preagreement_evidence", "select_best"]
