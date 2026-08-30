"""Typed failures the rest of the app can act on without knowing Postgres exists.

MAY IMPORT:  domain.
IMPORTED BY: store, tools, api.

``AwardConflict`` is deliberately NOT redefined here. It lives in ``domain/models.py`` and is
what ``tests/fakes.py`` raises, and the fake and the real store must raise *the same class* or
the shared contract suite in ``tests/test_store.py`` proves nothing. It is re-exported so
callers have one obvious import site.

The rule these exist to enforce: no caller anywhere matches on the text of a database error.
A driver message is not an API. Every conflict below is classified from the SQLSTATE plus the
name of the index that fired -- names we chose and that live in ``supabase/migrations/`` --
and an unrecognised one raises loudly rather than being folded into the nearest neighbour.

OWNER: Track C.
"""

from app.domain import AwardConflict

__all__ = ["AwardConflict", "RowNotFound", "StoreError", "StoreUnavailable"]


class StoreError(Exception):
    """Anything the store could not do that the caller did not ask for."""


class StoreUnavailable(StoreError):
    """No usable database configuration.

    Raised on first use rather than at construction, so a server with no ``SUPABASE_URL``
    still boots and still answers ``/health`` -- which is the endpoint you need most when the
    database is the thing that is broken.
    """


class RowNotFound(StoreError):
    """A write named a row that is not there.

    PostgREST answers a zero-row update with success and an empty body, so without this every
    ``set_order_status`` against a typo would look like it worked. ``InMemoryStore`` raises
    ``KeyError`` in the same places; the shared suite asserts that both refuse.
    """
