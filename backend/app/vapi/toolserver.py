"""POST /vapi/tools -- the model's entire mutation surface.

Two rules that are not style preferences:

  * ALWAYS return HTTP 200. Vapi ignores any other status code completely, so a 500 fails
    *open*: the tool call silently does nothing and the agent carries on as though it had
    worked. On any internal error, return 200 with an "error" string that makes the agent
    hold and escalate.
  * result and error must be single-line strings, and toolCallId must match exactly.

Request:  {"message": {"type": "tool-calls", "toolCallList": [{"id", "name", "arguments"}]}}
Response: {"results": [{"toolCallId": "<exact>", "result": "<single-line string>"}]}

STATUS: Phase 0 stub. OWNER: Track B.
"""

from fastapi import APIRouter

__all__ = ["create_tool_router"]


def create_tool_router() -> APIRouter:
    raise NotImplementedError("Track B: implement app/vapi/toolserver.py")
