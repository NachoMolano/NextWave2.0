"""The phone. The single vendor surface for voice and telephony.

MAY IMPORT:  domain, config, agent, tools.
IMPORTED BY: jobs, main.

Vapi runs the model, the transcriber and the voice, so this package holds no audio code at
all -- it composes an assistant, places calls, and receives webhooks. It may not import
store/: every write from a webhook or a tool call goes through tools/, which is where policy
sits. That one missing edge is what keeps a stranger on the phone from reaching the database
directly.

OWNER: Track B.
"""
