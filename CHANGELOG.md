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
