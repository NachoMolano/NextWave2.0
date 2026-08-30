# Vapi payload fixtures

**Every file in here marked PROVISIONAL was written by hand from the documentation, not
captured from a real call.** They exist so Tracks A, D and E can write assertions before any
phone rings.

Two fields the docs did not confirm, and which these fixtures therefore guess at:

- `artifact.messages[].secondsFromStart` — referenced in the API reference and in community
  threads, absent from the guide pages, and reported to sometimes return an epoch value
  rather than an offset.
- `artifact.stereoRecordingUrl` — same status.

This is why `tools/calls.py` measures its own anchor server-side instead of reading either
one. Evidence does not rest on a field nobody has seen.

**CP4 replaces this directory.** Track B places one throwaway call, dumps the raw
`end-of-call-report` here, deletes the PROVISIONAL marker, and tells the team. Until that
happens, treat a green suite as proof the code is self-consistent — not as proof it matches
what Vapi sends.
