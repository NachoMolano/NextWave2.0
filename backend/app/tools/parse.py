"""Spoken money and dates, parsed deterministically before anything is believed.

Ported from nextwave/backend/app/tools/conversation_guard.py, keeping the money and date
grammar and dropping the token-stream filter -- Vapi owns the stream now, so there is no
longer a place to stand between the transcriber and the model. What survives is the part
that matters: the point where a spoken figure becomes a number in the database.

The rule this file exists to enforce (AGENTS.md #8): never infer. "Eight five" can be eight
thousand five hundred or eighty-five thousand, and picking the likelier one is how an agent
books a load at ten times the rate.

STATUS: Phase 0 stub. OWNER: Track A.
"""

from datetime import date, datetime
from decimal import Decimal

__all__ = ["Ambiguous", "parse_amount", "parse_date"]


class Ambiguous:
    """Returned instead of a value when the utterance admits more than one reading.

    A distinct type rather than None, because None reads as "absent" and this is "present
    and dangerous" -- the caller has to ask, not skip.
    """

    __slots__ = ("heard", "why")

    def __init__(self, heard: str, why: str) -> None:
        self.heard = heard
        self.why = why

    def __repr__(self) -> str:
        return f"Ambiguous(heard={self.heard!r}, why={self.why!r})"


def parse_amount(text: str) -> tuple[Decimal, str] | Ambiguous | None:
    """Extract (amount, ISO 4217 currency) from spoken text.

    Returns None when there is no amount at all, Ambiguous when there is one that cannot be
    read a single way, and never a guess. An amount with no currency is Ambiguous, not a
    default: the company currency is what we pay in, not what they said.
    """
    raise NotImplementedError("Track A: port the money grammar from conversation_guard.py")


def parse_date(text: str, today: date) -> datetime | Ambiguous | None:
    """Resolve a spoken date against today.

    A bare weekday is Ambiguous. "Thursday" has to be worked out into a calendar date and
    read back before it can be treated as heard.
    """
    raise NotImplementedError("Track A: port the date grammar from conversation_guard.py")
