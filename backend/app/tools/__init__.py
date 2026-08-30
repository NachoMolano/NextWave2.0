"""The boundary: the only place a proposal meets policy.

MAY IMPORT:  domain, policy, store, notify.
IMPORTED BY: vapi, api, jobs.

Everything that mutates state passes through here, whether the caller is the model on a
phone call or the server itself. Adding a function to this package widens what a stranger on
the phone can reach, so it is an architectural decision -- flag it, do not just ship it.

OWNER: Track A (model.py, parse.py, commitments.py) and Track E (market.py, calls.py).
"""
