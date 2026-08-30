"""Spoken money and dates, parsed deterministically before anything is believed.

Ported from nextwave/backend/app/tools/conversation_guard.py, keeping the money and date
grammar and dropping the token-stream filter -- Vapi owns the stream now, so there is no
longer a place to stand between the transcriber and the model. What survives is the part
that matters: the point where a spoken figure becomes a number in the database.

The rule this file exists to enforce (AGENTS.md #8): never infer. "Eight five" can be eight
thousand five hundred or eighty-five thousand, and picking the likelier one is how an agent
books a load at ten times the rate.

Every ambiguity rule below is *grammatical*. None of them weighs whether a figure sounds
plausible for a drayage run: a rule that asks "is this a sensible rate?" is the same rule
that decides a confident-sounding counterparty is probably right, and that is exactly the
judgement the whole design refuses to make.

OWNER: Track A.
"""

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

__all__ = ["Ambiguous", "date_states_a_time", "parse_amount", "parse_date"]


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


# ------------------------------------------------------------------------------- the grammar

_DIGIT_AMOUNT = r"\d[\d,]*(?:\.\d{1,2})?"
_UNIT_WORD = (
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety"
)
_NUMBER_WORD = rf"(?:{_UNIT_WORD}|hundred|thousand|million|and|[- ])+"
_CURRENCY_TEXT = (
    r"USD|MXN|EUR|CAD|GBP|US dollars?|Mexican pesos?|euros?|Canadian dollars?|British pounds?"
)
_MONEY = re.compile(
    rf"(?i)(?:\$\s*(?P<dollar>{_DIGIT_AMOUNT})|"
    rf"(?P<digits>{_DIGIT_AMOUNT})\s*(?P<digit_currency>{_CURRENCY_TEXT})|"
    rf"(?P<words>{_NUMBER_WORD})\s+(?P<word_currency>{_CURRENCY_TEXT}))"
)

#: Two or more *single-digit* words in a row. "Eight five" is the canonical case: 8,500 and
#: 85,000 are both faithful readings and nothing in the utterance chooses between them.
#: Widened from the old _AMBIGUOUS_SHORT_WORDS, which only fired after a keyword like "rate"
#: and so missed the same words arriving in a structured tool argument.
#:
#: Restricted to one-through-nine on purpose. "Twenty five hundred" and "eighty five" are
#: ordinary English composition and resolve to exactly one number; only a run of bare digits
#: is someone reading a figure out digit by digit and leaving the magnitude off.
_DIGIT_RUN = re.compile(
    r"(?i)\b(?:one|two|three|four|five|six|seven|eight|nine)"
    r"(?:[- ](?:one|two|three|four|five|six|seven|eight|nine)\b)+"
)

#: A currency word that names more than one currency. "Dollars" is USD, CAD, AUD; "pesos" is
#: MXN, COP, ARS. The company currency is what we pay in, not what they said.
_UNQUALIFIED_CURRENCY = re.compile(r"(?i)\b(?:dollars?|pesos?|pounds?)\b")
_QUALIFIED_CURRENCY = re.compile(
    r"(?i)\b(?:US|American|Mexican|Canadian|Australian|British|Colombian|Argentine)\s+"
    r"(?:dollars?|pesos?|pounds?)\b"
)

_ANY_NUMBER = re.compile(rf"(?i)(?:{_DIGIT_AMOUNT}|\b(?:{_UNIT_WORD}|hundred|thousand|million)\b)")

_SMALL: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_CURRENCIES: dict[str, str] = {
    "usd": "USD",
    "us dollar": "USD",
    "us dollars": "USD",
    "mxn": "MXN",
    "mexican peso": "MXN",
    "mexican pesos": "MXN",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "cad": "CAD",
    "canadian dollar": "CAD",
    "canadian dollars": "CAD",
    "gbp": "GBP",
    "british pound": "GBP",
    "british pounds": "GBP",
}

_MONTH = r"January|February|March|April|May|June|July|August|September|October|November|December"
_SPELLED_DATE = re.compile(
    rf"(?i)\b(?P<month>{_MONTH})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<year>\d{{4}})\b"
)
_ISO_DATE = re.compile(
    r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"(?:[T ](?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?"
)
_WEEKDAY = re.compile(
    r"(?i)\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b"
)
_RELATIVE_DAY = re.compile(r"(?i)\b(?:today|tomorrow|tonight|manana|hoy|next week|this week)\b")


def _spoken_integer(value: str) -> Decimal | None:
    """Fold a run of number words into one integer. Returns None if the run is not one."""
    tokens = value.lower().replace("-", " ").split()
    if not tokens or all(token == "and" for token in tokens):
        return None
    total = 0
    group = 0
    for token in tokens:
        if token == "and":
            continue
        if token in _SMALL:
            group += _SMALL[token]
        elif token == "hundred" and group:
            group *= 100
        elif token == "thousand" and group:
            total += group * 1_000
            group = 0
        elif token == "million" and group:
            total += group * 1_000_000
            group = 0
        else:
            return None
    return Decimal(total + group)


def _money(match: re.Match[str]) -> tuple[Decimal, str] | None:
    raw = match.group("dollar") or match.group("digits")
    amount: Decimal | None
    if raw is not None:
        try:
            amount = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            return None
    else:
        amount = _spoken_integer(match.group("words") or "")
        if amount is None:
            return None
    currency_text = match.group("digit_currency") or match.group("word_currency")
    currency = "USD" if match.group("dollar") else _CURRENCIES.get((currency_text or "").lower())
    return (amount, currency) if currency is not None else None


def _spelled(match: re.Match[str]) -> datetime | None:
    try:
        return datetime.strptime(
            f"{match.group('month')} {match.group('day')} {match.group('year')}", "%B %d %Y"
        ).replace(tzinfo=UTC)
    except ValueError:
        return None


# -------------------------------------------------------------------------- the public surface


def parse_amount(text: str) -> tuple[Decimal, str] | Ambiguous | None:
    """Extract (amount, ISO 4217 currency) from spoken text.

    Returns None when there is no amount at all, Ambiguous when there is one that cannot be
    read a single way, and never a guess. An amount with no currency is Ambiguous, not a
    default: the company currency is what we pay in, not what they said.
    """
    if not text or not text.strip():
        return None

    # Checked before anything tries to read the text as a number: a run of bare units
    # resolves to nothing on its own, and "eight five USD" carries a currency and is still
    # two different numbers.
    run = _DIGIT_RUN.search(text)
    if run is not None:
        return Ambiguous(
            heard=run.group(0),
            why="a run of single digits with no scale word: 8,500 and 85,000 both fit",
        )

    if _UNQUALIFIED_CURRENCY.search(text) and not _QUALIFIED_CURRENCY.search(text):
        return Ambiguous(
            heard=text.strip(),
            why="the currency word names more than one currency and was not qualified",
        )

    match = _MONEY.search(text)
    if match is None:
        if _ANY_NUMBER.search(text):
            return Ambiguous(
                heard=text.strip(),
                why="an amount with no currency is incomplete data, not a default",
            )
        return None

    parsed = _money(match)
    if parsed is None:
        return Ambiguous(heard=match.group(0), why="the figure could not be read a single way")
    return parsed


def date_states_a_time(text: str) -> bool:
    """Did the utterance carry a clock time, or only a day?

    "September fourth" is a day. "2026-09-04T14:00" is a moment. Both currently resolve to a
    datetime, and the difference is lost the instant ``parse_date`` returns -- which is how a
    pickup nobody put an hour on ends up stored at midnight UTC and judged, to the minute,
    against a window an operator typed in local business hours.

    Kept beside ``parse_date`` and derived from the same regex rather than inferred later from
    a 00:00 timestamp. A midnight that was actually spoken and a midnight that was never
    spoken are different facts, and only the utterance can tell them apart.
    """
    match = _ISO_DATE.search(text or "")
    return match is not None and match.group("hour") is not None


def parse_date(text: str, today: date) -> datetime | Ambiguous | None:
    """Resolve a spoken date against today.

    A bare weekday is Ambiguous. "Thursday" has to be worked out into a calendar date and
    read back before it can be treated as heard -- and this function deliberately will not
    do the working out, because "Thursday" spoken on a Thursday is either today or in seven
    days and the utterance does not say which.

    ``today`` is a parameter for the same reason ``now`` is one in policy/: a date that
    depends on when the code happened to run cannot be replayed.
    """
    if not text or not text.strip():
        return None

    iso = _ISO_DATE.search(text)
    if iso is not None:
        try:
            return datetime(
                int(iso.group("year")),
                int(iso.group("month")),
                int(iso.group("day")),
                int(iso.group("hour") or 0),
                int(iso.group("minute") or 0),
                int(iso.group("second") or 0),
                tzinfo=UTC,
            )
        except ValueError:
            return Ambiguous(heard=iso.group(0), why="not a real calendar date")

    spelled = _SPELLED_DATE.search(text)
    if spelled is not None:
        resolved = _spelled(spelled)
        if resolved is None:
            return Ambiguous(heard=spelled.group(0), why="not a real calendar date")
        return resolved

    if _WEEKDAY.search(text) or _RELATIVE_DAY.search(text):
        return Ambiguous(
            heard=text.strip(),
            why="a weekday or relative day is not a calendar date until it is read back",
        )
    return None
