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
