# Changelog

Communal. It answers "what did the others change while I was heads down?"

- **Write an entry when** your change touches another track's module, alters a shared
  contract (`domain/` types, `ports.py`, tool signatures, policy outcomes, DB schema), or is
  knowingly breaking. Not for ordinary work inside your own track — that is what `git log`
  is for.
- Newest at the top: `## <timestamp> · <module> · <who>`, what changed, then a mandatory
  `→ Affects:` line. Write `nobody` if it is self-contained.
- **Take the timestamp from `date "+%Y-%m-%dT%H:%M%z"`. Never write one from memory** — a
  guess at the current time is routinely hours or months off, and invented timestamps make
  the ordering lie silently.
- **On merge conflict: keep both entries, order by timestamp.** Never resolve by deleting
  one.

---

## 2026-08-30T01:13-0500 · vapi · track-b

Track B. `app/vapi/` is built: assistant composition, the outbound client, the tool server,
the event webhook, and the parallel dial. No stub in the package raises any more.

**Three signatures changed, all in files Track B owns. Track E wires them.** Phase 0's
zero-argument factories could not receive their dependencies, so each now takes them
explicitly rather than reaching for `get_settings()` — `/health` still boots with an empty
environment, and a test composes a router without one.

```python
# main.py, replacing the two "Track B:" comment lines
profile = profile_from_settings(settings)          # app.vapi.assistant
app.include_router(
    create_tool_router(model_tools, store, server_secret=settings.vapi_server_secret),
    prefix="/vapi",
)
app.include_router(
    create_webhook_router(
        store=store,
        ledger=call_ledger,                        # tools/calls.py::CallLedger — Track E
        reporter=report_model,                     # agent/report.py — Track D
        profile=profile,
        build_assistant_for=lambda ctx: build_assistant(profile, ctx, settings),
        escalation_number=settings.escalation_phone_number,
        server_secret=settings.vapi_server_secret,
        now=now_utc,
    ),
    prefix="/vapi",
)
```

**PR #4 landed a `_mount()` helper that calls these factories with no arguments and catches
only `NotImplementedError`.** They now raise `TypeError`, which it does not catch, so
`create_app()` dies at import and the server does not boot at all — verified on a trial merge
of `feat/b-telephony` + `feat/AE_tracks`:

```
TypeError: create_webhook_router() missing 8 required keyword-only arguments: 'store',
'ledger', 'reporter', 'profile', 'build_assistant_for', 'escalation_number',
'server_secret', and 'now'
```

Nothing catches this today because no test calls `create_app()`. Use the snippet above
instead of `_mount` for the two `/vapi` routers; `_mount` still earns its place for Track C's
`create_api_router`. The same trial merge is otherwise clean — one conflict, in this file,
resolved by the keep-both rule — and green at **245 passed, 1 xfailed** once
`tests/test_toolserver.py` stopped calling `ModelTools.__init__` (Track A grew it a `ledger`
and a `commitments` argument; the double now overrides every handler and never calls it).

`run_campaign(plans, placer, settings, *, profile, sleep=asyncio.sleep)` — `profile` is new
and required. The assistant is composed per plan, because each `DialPlan.context` carries the
market state as of dial time; one assistant reused across the fan-out would tell every carrier
the same thing. `sleep` is injected only so the concurrency backoff is testable.

**`.env` convention, needed before any call is placed.** Vapi wants a provider next to every
vendor id and `Settings` holds one string per slot, so each is now written `provider/id`:

```
VAPI_MODEL=openai/gpt-4o          VAPI_VOICE_ID=11labs/burt          VAPI_TRANSCRIBER=deepgram/nova-3
```

Those three values are illustrative — **verify each against current Vapi docs before filling
`.env`**. A value without a `/`, or an empty one, raises at composition rather than producing a
call that connects and cannot speak. `backend/.env.example` is not Track B's file and still
carries the old bare-id comment; whoever owns it should add the `provider/id` note.

Other decisions worth knowing about:

- **`vapi/` reads through the `Store` protocol.** It still may not import `store/`, and every
  *write* goes through `tools/` (`CallLedger` owns the call row). The reads are call
  correlation and the inbound carrier lookup; the two exceptions that write are `save_report`
  and the escalation `raise_approval`, neither of which has a policy question in it.
  `test_layering.py` is green — the type comes from `domain`, the instance from `main`.
- **The tool server returns 401 when `VAPI_SERVER_SECRET` is unset**, not 200. An unset secret
  means anyone who finds the URL owns the mutation surface. Vapi never sees that 401, so it
  cannot fail open the way a 500 would.
- **`transferCall` carries no destinations.** That is what makes Vapi send
  `transfer-destination-request`, which is the only way the destination is decided by us
  rather than chosen by the model. `transferPlan.mode="warm-transfer-say-summary"` therefore
  rides on the destination we return from `webhook.py`, not on the tool definition — a
  transferPlan can only sit on a destination.
- **`artifact.messages[].secondsFromStart` above 24h is dropped, not stored.** The reported
  epoch-value bug would otherwise write a 1.7-billion-second "offset" into evidence.
  `recording_url` is read from `artifact.recordingUrl`, `artifact.recording` (string) and
  `artifact.recording.stereoUrl` / `.mono.combinedUrl`, because the fixtures and the current
  docs disagree about which one exists.
- **Fixtures are still PROVISIONAL.** No real call has been placed, so CP4 has not happened.
  A green suite here proves the code is self-consistent, not that it matches what Vapi sends.
- New test files: `tests/test_toolserver.py`, `tests/test_vapi_webhook.py`,
  `tests/test_vapi_assistant.py`, `tests/test_vapi_campaign.py`. The last two are outside the
  two files named in the plan's "Owns" line; they cover `assistant.py` and `campaign.py`,
  which had no test file assigned. Nobody else owns them.

**Environment trap, not a code change.** `uv run pytest` intermittently dies with
`ModuleNotFoundError: No module named 'app'` — including on `tests/test_seam.py`, which
nothing has touched. uv rewrites `.venv/.../_editable_impl_volta.pth` on every sync, and this
machine's interpreter then ignores it, so the editable install disappears while `uv pip list`
still reports it. **`uv run python -m pytest` is unaffected** (it puts the working directory on
`sys.path`) and is what every number below was measured with. `uv sync --reinstall-package
volta` fixes `uv run pytest` until the next sync. Not root-caused further.

```
uv run ruff check .        All checks passed!
uv run mypy app/           Success: no issues found in 34 source files
uv run python -m pytest    148 passed, 1 xfailed
```

Definition of done, each pinned by a named test: a raising handler still returns 200 with an
`error` string (`test_a_raising_handler_still_returns_200_with_an_error_string`); replaying the
same `end-of-call-report` fixture twice is a no-op and the extraction model runs once
(`test_replaying_the_same_end_of_call_report_is_a_no_op`); `FakeCallPlacer` records three dials
from one campaign (`test_three_carriers_are_dialled_from_one_campaign`).

→ Affects: **Track E** — the wiring snippet above, `run_campaign`'s new `profile` argument, and
`.env.example`. **Track A** — `vapi/assistant.py` renders the argument models in `tools/model.py`
into the JSON schemas Vapi validates against, so adding a field there changes the tool surface;
`ModelTools` handler names are dispatched by `getattr`, and a rename breaks the tool server.
**Everyone** — use `uv run python -m pytest` if `uv run pytest` claims `app` does not exist.

---

## 2026-08-30T00:48-0500 · phase-0 · nacho/claude

Phase 0. The repo, the seams, and the schema — everything four tracks build against.

- `backend/` created: uv + Python 3.12, FastAPI, eight packages, `config.py`, `jobs.py`,
  `main.py`. `GET /health` boots.
- `app/domain/` is complete and **frozen**: `security.py` and `company.py` ported from the
  old repo, plus `models.py` (the operational vocabulary) and `ports.py` (the four
  Protocols: `Store`, `CallPlacer`, `Notifier`, `ReportModel`).
- `CallContext` moved from `agent/` to `domain/context.py`. `tools/` builds one, `agent/`
  renders it and `vapi/` stores it, and under the layering contract `tools` may not import
  `agent` — so a type all three share cannot live there. `prompts.py` now imports it from
  `app.domain`.
- `CallPhase.STATUS_CHECK` added for OUTBOUND 2. **`prompts.py` has no `_STATUS_CHECK` block
  or greeting yet**, so composing that phase raises `KeyError`. This is pinned by a
  `strict=True` xfail in `tests/test_seam.py`, which will FAIL once the block lands — that
  is deliberate, so whoever writes it has to delete the marker.
- `supabase/migrations/0001_init.sql`: all ten tables with the three load-bearing
  constraints (one award per order, `evidence_anchor_ms NOT NULL`, unique
  `idempotency_key`), `revoke update, delete` on `decisions` and `events`, RLS on
  everything with no policies. **Written but NOT applied** — Docker was down locally. Track
  C applies it first thing and reports any syntax error here.
- `tests/fakes.py`: `InMemoryStore` is a working implementation, not a stub — it enforces
  the idempotency-key refusal, the award conflict and quote superseding, because those are
  the behaviours the other tracks assume.
- `tests/test_layering.py`: the `ALLOWED` map, checked by AST. It caught a real violation on
  its first run (`agent/report.py` importing `config`), fixed by injecting credentials
  instead of widening the contract.
- `ruff` per-file-ignore for `RUF001` on `agent/prompts.py`: the prompt is prose a voice
  model reads aloud, so typographic dashes there are deliberate, not homoglyph typos.

`uv run pytest` → 58 passed, 1 xfailed. `uv run ruff check .` and `uv run mypy app/` clean.

→ Affects: **everyone**. Pull `main` before starting your track. `app/domain/ports.py` is
the contract — do not change a signature there without an entry here first.
