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

## 2026-08-30T03:09-0500 · vapi, tools, store, api, frontend · nacho/track-c

Three things: a leak, the bug that was keeping the ledger empty, and the business profile.

**The composed prompt was being published.** Vapi returns it as `artifact.messages[0]` with
role `system`, and `_turns()` mapped roles through a lookup with an `"other"` fallback -- so it
was kept, written to `calls.transcript` and rendered in the portal, including the section
headed FIGURES YOU MUST NEVER SAY OUT LOUD: the mandate ceiling and the negotiation target. It
also reached the report model as though a person had said it. The role map is now a whitelist;
an unrecognised role is logged and dropped. The portal filters again on render. **Rows written
before this fix still contain it** -- the scrub SQL is in PR #10.

**Calls were losing their operation, which is why nothing reached the ledger.** `plan_rfq`
creates the call row before anyone is dialled, holding `pending:{order}:{carrier}`.
`run_campaign` returns `{call_id: vapi_call_id}` and **every caller discarded it**, so the
placeholder was never replaced; the first webhook looked up the real id, missed, and inserted a
*second* row with no order, no carrier and no context. Every tool call on that call then
answered "I do not have that operation on this call" -- the correct refusal for a call the
server cannot place -- and the agent held and escalated. The conversation looked fine and
nothing was recorded. Across eight real calls: zero quotes, zero decisions, zero commitments.

`CallLedger.attach_provider_id` now re-keys the row, and `upsert_call` honours an explicit id
so re-keying moves the row rather than minting a second one. That last change is in
`tests/fakes.py` as well as `store/supabase.py`: the fake had the same behaviour, and a fake
more permissive than the database makes a green suite mean nothing.

**`company_profile`, and a Business page.** BUILD_PLAN section 2 listed this under "Not
tables", and for the prompt fields that was right -- an agent name is configuration. The
warehouse is not: it has a street address, a contact and opening hours, it changes because the
business changed rather than because someone redeployed, and the person who knows it is an
operator with a browser. One row, enforced by `check (id = 1)` rather than by everyone
remembering `limit 1`. Editing requires `updated_by`, for the same reason a mandate does: the
address is spoken to carriers on a recorded line.

`ruff` and `mypy --strict` clean, 318 passed / 15 skipped, frontend builds, `oxlint` clean.

Affects: **everyone** -- migration 0003 is applied. Anyone testing calls should know the
correlation fix is what makes the Decision Trace, the comparison and any commitment possible
at all; before it, every real call produced an empty ledger.

---
## 2026-08-30T02:57-0500 · ports, store, main, vapi · nacho

Three carriers, one order, three phones ringing -- verified on a live run.

The dial trigger itself is not mine: `3183b8d` landed the same fix on main first and went
further, claiming the `rfq-planned` event before it plans so a second click cannot re-dial.
What follows is what this branch adds on top of it.

`Store` gains **`attach_vapi_call_id(call_id, vapi_call_id)`**. A campaign writes the call row
— order, carrier, frozen negotiation context — before it dials, because the context is what
makes a call replayable, but the Vapi id does not exist until the dial returns. The row was
created with a `pending:` placeholder and nothing ever corrected it, so the webhook opened a
*second* row under the real id carrying no order and no context. Every tool call in the
conversation correlated to that empty one. On a live call the evidence for a single
conversation sat in two rows that never met. Implemented in `store/supabase.py` (update by row
id — `upsert_call` keys on `vapi_call_id` and would insert rather than correct) and in
`tests/fakes.py`; the shared store suite covers it against both.

`run_campaign` spaces its dials by `_LAUNCH_SPACING_SECONDS`. Three creates in the same
millisecond left one call ringing with no assistant on it; the same three two seconds apart
connected. The spacing sits before the semaphore, so a plan waiting its turn does not hold a
concurrency slot.

Not fixed, and worth someone's morning: **the mandate currency is dropped**.
`CallContext.price_ceiling` is a bare `Decimal`, so every builder does `order.cap.amount` and
throws `order.cap.currency` away; `prompts.py` then reattaches `COMPANY_CURRENCY`. A cap of
11,000 MXN reaches the agent as `Ceiling: 11,000 USD`. Policy is unaffected — it totals per
currency against real `Money` — so this is the agent's conversational judgement being
calibrated in the wrong unit, not an authorization hole. `Money` exists to make this
impossible and the prompt context is the one place that discards it.

→ Affects: **Everyone** — `ports.py` grew a method, so any `Store` implementation must add it.
**Track D** — `agent/context.py`'s
`context_from_order` and `company_profile_from_settings` are exported but imported by nothing;
the live path uses `market._context_for` and `assistant.profile_from_settings`, and the two
disagree on date format. One of them is what the next person will read and believe.

## 2026-08-30T02:16-0500 · vapi/assistant · nacho

CP4: the first real outbound call connected. Two things had to change to get there.

`_parameters_schema` now collapses Pydantic's `anyOf` unions into a single `type` and drops
the `description` on the parameters object. Vapi rejects both — `str | None` compiles to a
bare `anyOf` with no type of its own, and a description is allowed on every property but not
on the parameters object that holds them. Either one 400s the *whole* assistant at dial time,
so every carrier in a campaign fails at once and the log says only "dial failed". Verified
against the live API, not the docs: the error text names `description` even when the actual
offender is the missing type.

`tzdata` is now a declared dependency. Without it `zoneinfo` has no database on Windows,
`spoken_today` fell back to UTC, and the agent read tomorrow's date to a carrier every evening
after 18:00 Guadalajara. Linux CI has a system tzdb, so it only ever showed on a laptop.

Also found, not fixed: `POST /api/orders/{id}/rfq` calls `market.plan_rfq` and discards the
returned `list[DialPlan]`. Plans and call rows are written, the order flips to QUOTING, and
nothing dials. `run_campaign` is wired only to the chase path in `jobs.py`, so the parallel
RFQ fan-out — the centre of the brief — has no trigger.

→ Affects: **Track C** — `start_rfq` needs the injected dialler to place the plans it makes.
**Track E** — `sim_tools --url` and `replay_webhook --url` send no `x-vapi-secret`, so both
get 401 against a correctly configured server and the live HTTP rung cannot be exercised.

## 2026-08-30T02:23-0500 · security kernel · codex

Implemented the locally enforceable controls from the Volta security-kernel and drayage
procurement specifications: authenticated portal authority with server-derived actors,
optimistic mandate writes, award/pre-agreement revalidation, one-shot recap claims, explicit
ambiguous-delivery state, opt-in recording with a required notice, concrete inbound identity
levels, production readiness gates, and the missing IANA timezone dependency. The unimplemented
renegotiation route is no longer advertised. The frontend now supplies the demo bearer token from
tab-scoped storage and uses versioned human-action payloads.

`DeliveryStatus.UNKNOWN` is additive to the shared domain vocabulary. Migration 0003 extends the
notifications status constraint accordingly; it must be applied before deploying this branch.

→ Affects: **Everyone** — `/api` now requires `PORTAL_API_TOKEN` and
`PORTAL_MANAGER_IDENTITY`; mandate requests require `expected_version`; approval requests no
longer accept `decided_by`; recording defaults off; production startup requires the four named
readiness gates. **Track C/data** — apply migration 0003. **Frontend** — users enter the manager
token once per browser tab.

## 2026-08-30T01:52-0500 · frontend · nacho/track-c

The portal, brought over from the old repo's control tower and rewired to `/api`. `dashboard/`
is now **`frontend/`**, which is a rename away from what BUILD_PLAN section 1 says.

BUILD_PLAN describes it as "existing frontend -- untouched, consumes /api". Neither half was
true: it was not in this repo at all, and its client called `/operations`,
`/operations/{id}/workspace` and `/operations/{id}/rfqs/{id}/activate` -- no `/api` prefix and
an operations/workspace/rfq vocabulary with no counterpart in the ten-table schema. It could
not have been pointed at this backend.

What carried over: the design system (~280 lines of tokens, and the class names mostly already
fit -- `.mandate-card`, `.countdown.urgent`, `.offer-card`, `.transcript-line` with its anchor
column), the shell, hash routing, and the queue then detail then evidence shape. What was
rewritten: `types.ts` and `api.ts` entirely, and `App.tsx` against the new aggregate. Screens
are Operations, Operation, Approvals, Carriers, Call evidence.

Verified end to end against a running backend and live Supabase, not merely compiled: granting
a mandate through the portal produced `mandate_version = 1`, `mandate_set_by = diego@volta.test`,
`status = quoting` and one `mandate.set:<order>:v1` ledger row. `npm run build` and `oxlint`
are clean.

Two things worth knowing:

- **CORS does not exist and did not need to.** Vite proxies `/api` and `/health` in
  development, so the browser makes a same-origin request. A deployed build either ships from
  the API's origin or needs a narrow allowlist -- `*` is the wrong answer on the surface that
  carries the only endpoint able to write a price cap. No change to `main.py` was required.
- **The live store suite had been writing into the demo database.** Seven `OP-TEST-*` orders
  and twelve test carriers were sitting in the queue against one real order, because cleanup
  cannot delete an order once an event references it. Removed as table owner. That suite is
  opt-in and should point at a scratch project, never this one.

Affects: **nobody's files** -- `frontend/` is new and no track owned `dashboard/`. Worth
knowing anyway: the portal is step 9 of Flow A, so it is on the live demo path.

---

## 2026-08-30T01:42-0500 · integration, ci · codex

All four tracks are reconciled on the Track C branch. `main.py` now injects the real tool,
webhook, portal, report-model, market and campaign dependencies instead of invoking the router
factories with their obsolete zero-argument signatures. The deadline sweep receives the
profile-aware campaign dialler, so an OUTBOUND 2 call composes the same transient assistant as
an RFQ.

`tests/test_main.py` proves the resulting application mounts `/health`, `/vapi/events`,
`/vapi/tools`, and the portal without a database, Vapi, or a phone. The fake store now preserves
late-call evidence and decision timestamps like the real store; stale Track B assertions now
cover the implemented `STATUS_CHECK` phase and the current `ModelTools` constructor.

The real transcript export at `supabase/legacy_call_evidence.json` was removed: recordings and
transcripts do not belong in Git. `.github/workflows/ci.yml` makes the offline test suite,
Ruff, and mypy required CI work for pull requests and `main` pushes.

→ Affects: **Everyone** — merge the integrated Track C branch rather than the three separate
track PRs. CI must be required on `main` in GitHub branch protection.

## 2026-08-30T01:19-0500 · store, api, supabase · nacho/track-c

Track C: `store/` and `api/` implemented, the RLS posture hardened, the world seeded.
`uv run pytest` 96 passed / 15 skipped / 2 xfailed, `ruff` and `mypy --strict` clean.

**`store/supabase.py` uses the async client, not `asyncio.to_thread`.** The stub and
BUILD_PLAN both said to run a sync client in a worker thread; that predates the pinned
version -- `supabase` 2.31 ships `AsyncClient` on `httpx.AsyncClient`. The thread route
queues every store call behind the default executor's `min(32, cpu+4)` cap while PostgREST's
own default timeout is 120s, and `to_thread` is not cancellable, so a caller that gives up
leaves the thread running. `vapi/webhook.py` has a hard 7.5s budget for one
`carrier_by_phone`; that is the wrong risk to take. Construction still touches no network, so
`main.py` can keep building the store at import time and `/health` still answers when the
database does not.

**`supabase/migrations/0002_rls_posture.sql`.** 0001 left `anon` -- the role behind the
publishable key that ships inside any browser -- holding DELETE, INSERT, SELECT and UPDATE on
every table including `calls` and `commitments`. Every row was denied, because RLS was on with
no policies, so the safety of recordings, transcripts and commitments rested entirely on
nobody ever writing `create policy ... using (true)`. 0002 revokes those privileges and
counter-declares the default privileges so new tables do not silently reopen it. No
behavioural change: the backend uses the service role. The migration also records what
policies should look like on the day an authenticated dashboard reads Supabase directly, and
why that day also requires moving the backend off `service_role` -- it holds BYPASSRLS, so RLS
constrains nothing our own code does. The append-only guarantee comes from GRANT, not RLS.

Five things found that belong to other people. None are edited here.

1. **`InMemoryStore.upsert_call` erases evidence.** It replaces the stored record wholesale,
   so the empty transcript a `status-update` carries wipes the real one written moments
   earlier by the `end-of-call-report`. `ports.py` gives *exactly that ordering* as the reason
   the method is an upsert. The Supabase implementation merges instead, which is why
   `test_store.py::test_a_redelivered_status_update_does_not_erase_the_transcript` passes
   there and is pinned `xfail(strict=True)` for the fake -- so fixing the fake fails the test
   and forces the marker's removal, the same booby-trap Phase 0 used for STATUS_CHECK.
2. **`InMemoryStore.resolve_approval` does not set `decided_at`.** The `decided_has_decider`
   check refuses any non-open approval without one, so code that passes the fake violates the
   database. `SupabaseStore` stamps it; the fake should too.
3. **The `Store` Protocol has no list reads.** The portal needs all orders, all carriers, and
   the calls for one order; `ports.py` was shaped around one call and one order at a time,
   which is what the phone needs. `api/routes.py` declares a local `PortalStore(Store,
   Protocol)` with `list_orders`, `list_carriers` and `calls_for` rather than widening a
   contract four tracks build against. They belong in `ports.py`; until they are there, every
   implementation grows them separately, which is the drift the shared suite exists to catch.
   (`list_orders`/`list_carriers` are named around `InMemoryStore.orders` and `.carriers`
   already being dict attributes.)
4. **`tools/market.py` has no renegotiate entry point.** `POST /api/orders/{id}/renegotiate`
   answers 501 naming Track E rather than pretending. No TODO; the test asserts the 501.
5. **`test_seam.py::test_implements_its_protocol` cannot catch signature drift.**
   `runtime_checkable` Protocols compare method *names* only, so it passes even if an
   implementation's arguments diverge from `ports.py`. Worth knowing before trusting it as the
   seam check. Not edited -- it is not Track C's file.

→ Affects: **Track E** -- `main.py` wiring is two lines and nothing else:
`from app.api import create_api_router`, then
`app.include_router(create_api_router(store, market=market, sweep=sweep, now=now_utc, settings=settings), prefix="/api")`.
`sweep` is `Callable[[], Awaitable[list[str]]]` -- bind `jobs.sweep_deadlines` with its deps,
because `api` may not import `jobs` under the layering contract. Also items 3 and 4 above.
**Phase 0** -- items 1, 2, 3, 5. **Everyone** -- 0002 is applied; the database is seeded with
four carriers and OP-MZO-0001, and the order carries **no mandate on purpose**: a human grants
the ceiling through `POST /api/orders/{id}/mandate`, which is the only cap writer there is.

---

## 2026-08-30T01:02-0500 · supabase, store · nacho/track-c

`0001_init.sql` is applied and verified against the live project. Phase 0 shipped it
unrun (Docker was down); this is the confirmation it was waiting on. **No syntax errors —
the DDL is correct as written.** Every other track's live path is unblocked.

Verified after apply, not assumed:

- 10 tables, RLS enabled on all 10, 0 policies — as designed; the backend's service role
  bypasses RLS and the dashboard has no direct path to call evidence.
- `quotes_one_award_per_order` and `commitments_one_live_per_order` exist with their exact
  partial predicates. `events.idempotency_key` is enforced by `events_idempotency_key_key`.
- **Grants landed.** `service_role` on `orders` has full CRUD; on `decisions` and `events`
  it has `INSERT, SELECT, REFERENCES, TRIGGER, TRUNCATE` and **no UPDATE, no DELETE**. The
  append-only revoke bites the backend, which was the point of writing it.

Two things worth knowing before anyone applies this elsewhere:

- The `revoke ... from anon, authenticated, service_role` lines need those roles to exist.
  On a vanilla `postgres:17` container for CI they do not, and the whole migration aborts.
  It is fine on Supabase and under `supabase start`.
- The file contains no `GRANT`. It works because Supabase's default privileges grant to
  `service_role` for objects created by `postgres`. Applied by any other role the tables
  would get no grants at all, the revokes would succeed as no-ops, and the backend would
  get `permission denied` on everything while the migration reported success. Silent, so
  worth naming.

The database now holds the ten-table schema and nothing else. The previous 30-table schema
from the old repo (`operations`, `offers`, `mandates`, `rfqs`, `counterparties`, ...) was
dropped to make room — it collided on `commitments` and `fx_rate_snapshots`, so the two
could not coexist. Before dropping I exported the four real recorded test calls (37 Spanish
transcript turns with their audio offsets) — see the Tracks A/D note below.

`SUPABASE_URL=https://hizwyjrjvzrdohuxklle.supabase.co`. Take `SUPABASE_SECRET_KEY` from the
project's API settings; it is not written down here.

→ Affects: **everyone** — the schema is live, set your `.env`. **Tracks A and D**: the
salvaged call evidence is committed at `supabase/legacy_call_evidence.json` — four real calls
covering an unparseable amount in a foreign currency, a mid-call demand for a human before
identity was established, and the agent restating a weekday as an explicit calendar date.
It is real speech, not synthetic. It sits under `supabase/` only because that is a
directory I own; move it into your fixtures dir and delete it from there. Note the
anchors are Twilio stream offsets; Vapi's equivalent is unconfirmed until CP4.
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
## 2026-08-30T00:59-0500 · domain/ports · tools · nacho/claude

Three signature changes, announced before the code. Tracks A and E start against them.

- **`app/domain/ports.py` — two additive `Store` reads.** No existing signature changed.
  - `orders_in_status(status) -> list[Order]`. The RFQ timeout sweep in `jobs.py` has to
    find markets still open in `quoting`, and `due_for_chase` only answers the
    delivery-deadline question.
  - `commitment(commitment_id) -> Commitment | None`. `Store` already had `quote`, `call`
    and `approval` getters and no commitment one; the recap gate and renegotiation both
    read a row *after* it has left the live slot, which `live_commitment` cannot return.

  Both implemented in `tests/fakes.py::InMemoryStore` in the same commit, so nothing is red
  while they wait. **I also added the two matching `raise NotImplementedError` stubs to
  `store/supabase.py`** — not the implementations, just the stubs, because
  `test_seam.py::test_implements_its_protocol` checks `SupabaseStore` against the Protocol
  by name and `main` would otherwise be red for everyone until Track C picked this up. No
  Track C logic was touched.
  → Affects: **Track C** — implement both in `store/supabase.py` before CP3.
- **`app/tools/commitments.py` — `send_recap_and_promote(commitment_id, message)`** now takes
  the rendered `OutboundMessage`. The recap gate stays in Track A; the wording stays in Track
  D's `notify/render.py`. Track A must not import a renderer, and the alternative was
  `tools/` reaching into `notify/` for prose.
  → Affects: **Track D** (supplies the message), **Track E** (the wiring in `main.py`).
- **`app/tools/model.py` — `ModelTools.__init__`** gains `ledger: CallLedger` and
  `commitments: CommitmentCoordinator`. `propose_quote` needs the server-side audio anchor,
  and `confirm_preagreement` must not be a second writer of the `commitments` table. Both are
  inside `tools/`, so no layering row moves.
  → Affects: **Track B** — `vapi/toolserver.py` constructs `ModelTools`.

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

# 2026-08-30 — Final market loop integration

- RFQ closure now observes every persisted call, including failed and quote-less attempts.
- One transcript-informed renegotiation round runs before the final deterministic comparison.
- The final comparison creates a dashboard-ready approval and a persisted manager email alert.
- The dashboard displays the recommended carrier, all alternatives, and policy reason codes.

→ Affects: Track B lifecycle phase preservation, Track C call listing/dashboard, Track D award
email, and Track E market/job orchestration.
