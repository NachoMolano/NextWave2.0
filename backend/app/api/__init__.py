"""The dashboard's REST surface. Authenticated humans, not strangers on a phone.

MAY IMPORT:  domain, config, tools, store.
IMPORTED BY: main.

Separate from vapi/ because the trust levels are different: this router carries the only
endpoint that can write a price cap, and vapi/ carries an endpoint anyone can POST to. Reads
go straight to store/; every mutation goes through tools/, which is why this package may not
import policy.

OWNER: Track C.
"""

from app.api.routes import PortalStore, create_api_router

__all__ = ["PortalStore", "create_api_router"]
