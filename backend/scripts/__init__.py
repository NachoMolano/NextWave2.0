"""Operator scripts. Not part of the application, and never imported by app/.

These run a scenario end to end with no PSTN, no Vapi account and no database, which is what
makes them safe to run in CI and in front of a judge. They import ``tests.fakes`` on purpose:
the fake store is the reference implementation of the Store contract, and a second in-memory
store written just for scripts would be a second thing to keep honest.

OWNER: Track E.
"""
