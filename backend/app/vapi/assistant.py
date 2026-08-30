"""Composes the transient assistant sent with every call.

Assembles: the system prompt from agent/prompts.py, the greeting, voice and transcriber ids
from config, the five custom tools with their JSON schemas pointed at /vapi/tools, the
built-in transferCall with transferPlan.mode="warm-transfer-say-summary" so a human hears
the context before being bridged in, and artifactPlan.recordingEnabled.

Model, voice and transcriber ids come from config.py and are never hardcoded here.

STATUS: Phase 0 stub. OWNER: Track B.
"""

from app.config import Settings
from app.domain import CallContext, CompanyProfile

__all__ = ["build_assistant"]


def build_assistant(
    profile: CompanyProfile, context: CallContext, settings: Settings
) -> dict[str, object]:
    raise NotImplementedError("Track B: implement app/vapi/assistant.py")
