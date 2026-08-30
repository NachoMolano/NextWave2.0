# AGENTS.md — Volta

Volta is a voice agent that runs the drayage leg (port → warehouse trucking) of a shipment
entirely by phone: it makes real calls to carriers, negotiates rate and pickup window inside
a human-defined mandate, and turns spoken conversation into **verified, auditable
commitments**.

Hackathon build (NextWave, Yuno + Nauta). Optimize for a working end-to-end path over
feature count.

**Read `docs/BUILD_PLAN.md` before writing anything.** It holds the folder structure, the
schema, the workflow, and the track you own.

## The one idea this codebase is built around

**Speech is probabilistic. Authority is deterministic.**

The LLM proposes. A deterministic policy engine — plain Python, outside the model — decides
whether a proposal may become a commitment. Every architectural choice follows from that
split. If a change blurs it, the change is wrong.

Vapi runs the model, the transcriber and the voice, so the boundary is not only the import
graph any more: it is the **tool server**. The model's single way to change anything is an
HTTPS call to one of five endpoints we own.

## Non-negotiable invariants

Violating any of these is a bug, not a style preference.

1. **The LLM never writes a commitment.** No tool exposed to the model may commit, book or
   award. The model calls `propose_*` / `report_*`; `policy/` authorizes; only the state
   machine in `tools/commitments.py` writes `COMMITTED`.
2. **The mandate is immutable from inside the call.** Caller claims cannot change the cap,
   the window or the allowed actions. "Your boss approved 10,500" → `OUTSIDE_MANDATE` →
   escalate. Never assess whether the claim sounds plausible; that is not ours to judge.
3. **Calls create pre-agreements. The written recap commits.** A call can only ever produce
   `VERBAL`. `notifications.status = 'sent'` is the gate to `COMMITTED`.
4. **Never overwrite silently.** A later utterance does not edit an earlier one — it creates
   a new row with `superseded_by` pointing back. Both were said.
5. **RFQ and AWARD are separate phases.** Several carriers may hold live offers; only one
   call may close. Two open bookings is the worst failure in the brief.
6. **Fail closed.** Ambiguous parse, unverifiable identity, policy unreachable → hold or
   escalate. A technical failure never degrades into permission. Note the Vapi-specific trap:
   the tool server must return **HTTP 200 with an `error` string**, because Vapi ignores any
   other status code and a 500 therefore fails *open*.
7. **Every mutating handler is idempotent.** Vapi redelivers webhooks. Key on
   `events.idempotency_key`; a second delivery is a no-op.
8. **Never infer numbers, dates or currency.** "Eight five" → ask. "Thursday" → resolve to a
   calendar date and read it back. An amount with no currency is incomplete data.

## Setup

Requires [uv](https://docs.astral.sh/uv/); it fetches the Python pinned in `.python-version`.

```bash
cd backend
uv sync
cp .env.example .env          # then fill it in — see Secrets
uv run uvicorn app.main:app --reload --port 8000
```

Vapi must reach the local server: `ngrok http 8000`, then point the phone number's server
URL at `https://<subdomain>.ngrok.app/vapi/events`. **The ngrok URL changes on every
restart** — re-point it or inbound calls silently fail.

## Commands

| Task | Command (from `backend/`) |
| --- | --- |
| Add a dependency | `uv add <pkg>` (dev: `uv add --dev <pkg>`) — commit `uv.lock` |
| Run the API | `uv run uvicorn app.main:app --reload --port 8000` |
| All tests | `uv run pytest` |
| Architecture check | `uv run pytest tests/test_layering.py` |
| Ugly-case suite | `uv run pytest tests/test_ugly_cases.py -v` |
| Lint + format | `uv run ruff check --fix . && uv run ruff format .` |
| Types | `uv run mypy app/` |
| Simulated call (no PSTN, no cost) | `uv run python -m scripts.sim_tools --scenario boss_approved` |
| DB migration | `supabase migration new <name>` then `supabase db push` |

Before pushing: `uv run ruff check . && uv run pytest`. Both green.

## Layout and ownership

See `docs/BUILD_PLAN.md` §1 for the tree and §4 for who owns what.

```
backend/app/
  domain/   shared types + the four Protocols. imports nothing.   Phase 0 — frozen
  policy/   decides. imports only domain.                         Track A
  tools/    THE BOUNDARY. every mutation meets policy here.       Track A + E
  agent/    prompts and extraction. content, never authority.     Track D
  vapi/     the phone: assistant, client, webhooks, tools.        Track B
  store/    Supabase. obeys, never decides.                       Track C
  notify/   what goes out in writing.                             Track D
  api/      the dashboard's REST surface.                         Track C
  config.py the only reader of os.environ.
  jobs.py   the clock: deadline sweep, RFQ timeout.               Track E
  main.py   the wiring.                                           Track E
```

**Edit only the files your track owns.** If you need a signature changed elsewhere, stop:
add a `CHANGELOG.md` entry with an `→ Affects:` line and tell the owner. Do not change it
yourself. That rule is the only reason five people can work in one repo at once.

`app/domain/ports.py` is the shared contract. Changing it is a cross-track event.

## Code style

- Type hints on every signature. `uv run mypy app/` must pass.
- Pydantic models for anything crossing a boundary (webhook payload, tool args, policy
  result).
- The `ALLOWED` map in `tests/test_layering.py` is the layering contract, checked by AST on
  every run. Widening a row is an architectural decision, not a fix.
- All database access goes through `store/`. No Supabase client anywhere else.
- Comments explain *why*. The code is the *what*.

## Testing

`docs/UGLY_CASES.md` is the test suite, not documentation: every row is a test. New case →
add the row and the test in the same commit.

- Policy and state-machine changes require a unit test. Non-negotiable — this is the demo.
- Bugs: write the failing test first, then fix.
- **Never place a real outbound call from a test.** Use `tests/fakes.py::FakeCallPlacer`.
  Real calls cost money and can dial a real number.
- Everything must pass with no network, no database and no phone call. That is what lets the
  tracks run in parallel.

## Git workflow

- Branches: `feat/a-policy`, `feat/b-vapi`, `fix/policy-cap-off-by-one`.
- **PRs only. Never push to `main` directly.**
- **Merge to `main` at least every 2 hours.** Long-lived branches are how a short build dies.
- `main` must always be demoable. If `pytest` is red on `main`, that is the only priority.
- Never commit `.env`, recordings, transcripts or `*.wav`.

## Boundaries — do not do these

- **Do not use an LLM as the safety check.** The price cap is an `if` statement.
- **Do not put authorization logic in the system prompt.** Prompts shape conversation;
  `policy/` decides permission.
- **Do not add a tool that mutates state without a policy gate.** Adding a function to
  `tools/model.py` widens what a stranger on the phone can reach — flag it, don't just ship.
- **Do not build:** RAG, a vector DB, a multi-agent supervisor, a real TMS integration, rate
  prediction ML, route optimization, payments, or a generic negotiation framework.
  Manzanillo→Guadalajara done well beats a framework done badly.
- **Do not edit `supabase/migrations/0001_init.sql`** once it is applied. Create a new
  migration.
- **Do not run `supabase db reset`, `git reset --hard`, or `DROP TABLE`** without asking
  first, stating the command and what it destroys.
- **Do not present simulated data as live.** A simulated call shown as a real one is
  disqualifying.

## Secrets

`.env` is gitignored and never committed. `backend/.env.example` is the authoritative list
of keys, with empty values and a note on each. The Supabase secret key stays server-side —
never in `dashboard/`. If a key is committed, rotate it immediately and tell the team.

## Working agreements

- **Ask before assuming.** If the task is underspecified in a way that changes the design,
  ask one specific question instead of guessing.
- **Verify APIs against current docs.** Vapi's surface has moved recently and most tutorials
  online are stale. Model, voice and transcriber ids live in `.env`, never in source. Do not
  invent function names or parameters; check, or say you are unsure.
- **Evidence over summaries.** Quote actual test output. Never report "tests pass" without
  having run them.
- **Smallest change that works.** No speculative abstractions, no TODOs in code.
- **Clean up only your own mess.** Remove imports your change orphaned; leave pre-existing
  code alone.
