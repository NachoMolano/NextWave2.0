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
