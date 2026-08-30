"""Prompts and post-call extraction. Content, never authority.

MAY IMPORT:  domain.
IMPORTED BY: vapi.

Prompts shape a conversation; they decide nothing. A counterparty can talk the agent into
ignoring a sentence of its instructions, and that must never be the same thing as talking it
past its limits -- which is why authorization lives in policy/ and cannot be reached from
here.

OWNER: Track D.
"""
