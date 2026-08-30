"""The thirteen endpoints the dashboard consumes.

Reads go straight to store/. Every mutation goes through tools/ -- which is why this package
may not import policy: there must be no way to write state here that skips the boundary.

POST /api/orders/{id}/mandate is the only price-cap writer in the system. Nothing reachable
from a phone call can touch it.

STATUS: Phase 0 stub. OWNER: Track C.
"""

from fastapi import APIRouter

__all__ = ["create_api_router"]


def create_api_router() -> APIRouter:
    raise NotImplementedError("Track C: implement app/api/routes.py")
