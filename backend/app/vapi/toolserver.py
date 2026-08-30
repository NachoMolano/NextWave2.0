"""POST /vapi/tools -- the model's entire mutation surface.

Two rules that are not style preferences:

  * ALWAYS return HTTP 200. Vapi ignores any other status code completely, so a 500 fails
    *open*: the tool call silently does nothing and the agent carries on as though it had
    worked. On any internal error, return 200 with an "error" string that makes the agent
    hold and escalate.
  * result and error must be single-line strings, and toolCallId must match exactly.

Request:  {"message": {"type": "tool-calls", "toolCallList": [{"id", "name", "arguments"}]}}
Response: {"results": [{"toolCallId": "<exact>", "result": "<single-line string>"}]}

The one deliberate exception to "always 200" is authentication. A request without the shared
secret is not Vapi, so there is no agent on the other end for a 200 to reassure -- answering
it at all only tells an unauthenticated caller that the endpoint exists. Vapi never sees that
401, so it cannot fail open.

This module may reach the ``Store`` protocol to turn a vendor call id into ours. It reads;
it never writes. Every write on this path goes through ``tools/``, which is where policy is.

STATUS: built. OWNER: Track B.
"""

import json
from typing import Any

import structlog
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ValidationError

from app.domain import Store
from app.tools.model import ModelTools
from app.vapi.assistant import TOOL_ARGUMENT_MODELS

__all__ = ["HOLD_AND_ESCALATE", "create_tool_router", "single_line"]

log = structlog.get_logger(__name__)

#: What the model is told when the server could not do what it was asked. It states a fact
#: and points at the only safe next move. It never says the attempt succeeded, never repeats
#: a figure, and never offers a workaround the agent could take as permission.
HOLD_AND_ESCALATE = (
    "That did not go through on our side. Do not treat it as recorded, do not agree to "
    "anything, and tell them a person from the team will follow up."
)

#: Sent when a caller reaches a tool that this call has no business calling -- an unknown
#: name, or a call we cannot correlate. Same shape, same effect: hold.
_UNKNOWN_TOOL = (
    "That tool is not available on this call. Do not treat anything as recorded, and tell "
    "them a person from the team will follow up."
)

_MAX_RESULT_CHARS = 1200


def single_line(text: str) -> str:
    """Collapse to one line. Vapi requires it, and a newline is read aloud as a pause.

    Truncation is deliberate rather than a guard against a long answer: everything here is
    written to be one sentence, so a long string means something went wrong upstream, and a
    wall of text read to a carrier is worse than a clipped one.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) > _MAX_RESULT_CHARS:
        return collapsed[: _MAX_RESULT_CHARS - 1].rstrip() + "…"
    return collapsed


def _name_and_arguments(entry: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Read one tool call out of either envelope Vapi sends.

    Vapi mirrors the shape the tool was *defined* in. ``build_tool_definitions`` writes the
    OpenAI-style ``{"type": "function", "function": {...}}`` form, and the callback comes
    back the same way -- name under ``function``, arguments as a JSON *string*. The flat
    ``{"name": ..., "arguments": {...}}`` form in the docs is what the older tool config
    produces. Both are accepted because guessing which one is live is what broke this:
    every test passed against a fixture written from the docs and never captured, while in
    production every propose_quote came back "that tool is not available on this call" and
    two carrier quotes were lost.

    Returns ``(None, None)`` for anything unrecognised rather than raising: the caller
    already holds and escalates on a name it does not know.
    """
    function = entry.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        raw = function.get("arguments")
    else:
        name = entry.get("name")
        raw = entry.get("arguments")

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            # A malformed argument string is not a partial fact. The caller holds.
            return (name if isinstance(name, str) else None), None
    return (
        name if isinstance(name, str) else None,
        raw if isinstance(raw, dict) else None,
    )


def _error(tool_call_id: str, message: str) -> dict[str, str]:
    return {"toolCallId": tool_call_id, "error": single_line(message)}


def _result(tool_call_id: str, message: str) -> dict[str, str]:
    return {"toolCallId": tool_call_id, "result": single_line(message)}


def create_tool_router(
    tools: ModelTools,
    store: Store,
    *,
    server_secret: str,
) -> APIRouter:
    """The tool endpoint, with its dependencies already chosen by the composition root.

    ``server_secret`` is passed rather than read from ``Settings`` so a test can hand this
    router a secret without an environment, and so there is exactly one place -- main.py --
    that decides which secret is in force.
    """
    router = APIRouter()

    async def _dispatch(name: str, call_id: str, arguments: dict[str, Any]) -> str:
        model = TOOL_ARGUMENT_MODELS[name]
        args: BaseModel = model.model_validate(arguments)
        handler = getattr(tools, name)
        outcome = await handler(call_id, args)
        if not isinstance(outcome, str):
            raise TypeError(f"{name} returned {type(outcome).__name__}, not a string")
        return outcome

    @router.post("/tools")
    async def handle_tool_calls(request: Request, response: Response) -> dict[str, object]:
        if not server_secret or request.headers.get("x-vapi-secret") != server_secret:
            # Fail closed. An unset secret means anyone who finds this URL owns the
            # mutation surface, so an unset secret refuses everything.
            log.warning("vapi.tools.unauthenticated", configured=bool(server_secret))
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return {"error": "unauthorized"}

        try:
            body = await request.json()
        except Exception:
            # No toolCallId to answer against, so there is nothing to hold on. An empty
            # results array is the most honest 200 available.
            log.exception("vapi.tools.unparseable_body")
            return {"results": []}

        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, dict):
            log.warning("vapi.tools.no_message")
            return {"results": []}

        tool_calls = message.get("toolCallList")
        if not isinstance(tool_calls, list):
            log.warning("vapi.tools.no_tool_call_list", type=message.get("type"))
            return {"results": []}

        call = message.get("call")
        vapi_call_id = call.get("id") if isinstance(call, dict) else None

        call_id: str | None = None
        if isinstance(vapi_call_id, str) and vapi_call_id:
            try:
                record = await store.call_by_vapi_id(vapi_call_id)
            except Exception:
                # A store that is down must not become permission. The agent holds.
                log.exception("vapi.tools.correlation_failed", vapi_call_id=vapi_call_id)
                record = None
            call_id = record.id if record is not None else None

        results: list[dict[str, str]] = []
        for entry in tool_calls:
            if not isinstance(entry, dict):
                continue
            tool_call_id = entry.get("id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                # Without the id there is nothing Vapi can match a result to; answering
                # anyway would attach our reply to whichever call it guessed.
                log.warning("vapi.tools.tool_call_without_id", name=entry.get("name"))
                continue

            name, arguments = _name_and_arguments(entry)
            if not isinstance(name, str) or name not in TOOL_ARGUMENT_MODELS:
                # The keys are logged, not the values: an unrecognised envelope is the one
                # case where we need to see the shape, and the values are caller speech.
                log.warning(
                    "vapi.tools.unknown_tool", name=name, entry_keys=sorted(entry.keys())
                )
                results.append(_error(tool_call_id, _UNKNOWN_TOOL))
                continue

            if call_id is None:
                log.warning("vapi.tools.uncorrelated_call", vapi_call_id=vapi_call_id, name=name)
                results.append(_error(tool_call_id, HOLD_AND_ESCALATE))
                continue

            if not isinstance(arguments, dict):
                log.warning("vapi.tools.unparseable_arguments", name=name)
                results.append(_error(tool_call_id, HOLD_AND_ESCALATE))
                continue

            try:
                outcome = await _dispatch(name, call_id, arguments)
            except ValidationError as exc:
                # The model sent a shape the tool does not accept. That is not a reason to
                # improvise a partial write: nothing is recorded and the agent holds.
                log.warning("vapi.tools.invalid_arguments", name=name, errors=exc.error_count())
                results.append(_error(tool_call_id, HOLD_AND_ESCALATE))
            except Exception:
                # The whole point of this file. Any handler exception -- a bug, a dead
                # database, a NotImplementedError from a track that has not landed yet --
                # comes back as 200 with an error the agent can act on safely.
                log.exception("vapi.tools.handler_failed", name=name, call_id=call_id)
                results.append(_error(tool_call_id, HOLD_AND_ESCALATE))
            else:
                results.append(_result(tool_call_id, outcome))

        return {"results": results}

    return router
