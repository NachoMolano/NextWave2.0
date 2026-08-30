"""Persistence and evidence. Obeys, never decides.

MAY IMPORT:  domain, config.
IMPORTED BY: tools, api.

Implements domain.ports.Store. store/supabase.py is the only module in the codebase allowed
to import the Supabase client; everything else talks to the Protocol, which is what lets the
other tracks run against InMemoryStore with no database.

OWNER: Track C.
"""

from app.store.errors import AwardConflict, RowNotFound, StoreError, StoreUnavailable
from app.store.supabase import SupabaseStore

__all__ = [
    "AwardConflict",
    "RowNotFound",
    "StoreError",
    "StoreUnavailable",
    "SupabaseStore",
]
