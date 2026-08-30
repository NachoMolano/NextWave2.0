# Volta on Vapi — structure, schema, workflow, and a parallel build plan

## Context

`CHALLENGE.md` asks for a voice agent that runs the drayage leg (port → warehouse) entirely
by phone: negotiate with ≥3 carriers in parallel inside a human mandate, take inbound calls,
turn speech into auditable commitments anchored to audio timestamps, and escalate mid-call
rather than exceed the mandate. Three concrete flows are in scope: **OUTBOUND 1** (quote →
compare → human approval → book), **INBOUND** (carrier calls us; identify, classify,
report), **OUTBOUND 2** (delivery deadline passed → chase the carrier → report).

`NextWave2.0` is a clean-slate rebuild. Today it holds only `CHALLENGE.md`, `DATABASE.md`,
`EVALUATION.md`, `STRUCTURE (2).md` and a copy of `prompts.py`. The sibling repo
`C:\Users\Nacho\code\nextwave` has working code on a different stack: a hand-rolled cascade
(Twilio Media Streams → Deepgram STT → OpenAI → Deepgram TTS) worth ~1,000 LOC across
`voice/` + `telephony/`, an empty `market/` package (so "3 carriers in parallel" has no live
path), no `operations`/`quotes`/`commitments` tables, and a dashboard that is a static
scaffold.

Moving to **Supabase + Vapi** deletes the entire voice pipeline as owned code — VAD, mu-law
frames, barge-in, TwiML, conference transfer all become Vapi configuration — and frees the
whole budget for the parts that are actually missing: the market, the mandate, the schema,
and the human approval loop.

**The idea that survives the stack change:** *speech is probabilistic, authority is
deterministic.* The model proposes; plain-Python policy decides. Under Vapi the model runs on
Vapi's side, so the boundary is no longer only an import graph — it is the **tool server**.
The model's single way to change anything is an HTTPS call to one of five
`propose_*`/`report_*` endpoints we own.

Sections 1–3 are the reference spec. **Section 4 is the build plan**: one blocking setup
phase, then five tracks that touch disjoint files and can run simultaneously.

---

## Scope of this session

**Building now: Phase 0 + Track A + Track E.** Tracks B (`vapi/`), C (`store/`, `api/`,
migrations beyond `0001`) and D (`agent/`, `notify/`) are left for teammates — their packages
get stubs with real signatures so nothing blocks on them.

Concretely, at the end of this session:

- The repo exists: `backend/` with all eight packages, `pyproject.toml`, `config.py`,
  `supabase/migrations/0001_init.sql`, `docs/BUILD_PLAN.md`.
- `domain/` is complete and frozen — including `ports.py`, the four Protocols the other three
  tracks code against.
- `policy/engine.py` is ported and passing its unit tests.
- `tools/` is complete: the five model-facing tools, `parse.py`, `commitments.py`,
  `market.py`, `calls.py`.
- `jobs.py` and `main.py` wire it together; `GET /health` boots.
- `tests/fakes.py`, `tests/test_layering.py`, `tests/test_policy.py`,
  `tests/test_ugly_cases.py`, `scripts/sim_tools.py`, `scripts/replay_webhook.py`.
- **CP2 is green**: `sim_tools --scenario boss_approved` passes end to end with no Vapi, no
  Supabase, no phone call.

Calling a real number, a live Supabase project and the dashboard endpoints are all out of
scope here — they arrive with Tracks B, C and D.

**One correction to §1 that this session applies:** `CallContext` moves from `agent/` to
`domain/context.py`. It is a shared type — `tools/` builds it, `agent/` renders it, `vapi/`
stores it — and under the `ALLOWED` map `tools` may not import `agent`. Track D's
`prompts.py` then imports it from `domain`.

---

## 1. Folder structure

Follows the seven-folder argument in `STRUCTURE (2).md`, adapted to the new stack. `voice/` +
`telephony/` collapse into a single vendor package `vapi/`. Two additions earn their place
under that document's own rule (*a directory exists only if it has a distinct reason to change
**and** a distinct trust level*): `api/` serves authenticated humans, `vapi/` serves a stranger
on a phone — merging them would put the mandate-write endpoint in the same package as an
unauthenticated webhook.

```
NextWave2.0/
├── README.md  AGENTS.md  CHANGELOG.md
├── docs/            ARCHITECTURE.md · DECISION_LOG.md · UGLY_CASES.md
│                    (+ CHALLENGE.md, DATABASE.md, EVALUATION.md moved in from root)
├── supabase/
│   ├── migrations/  0001_init.sql
│   └── seed.sql     3 carriers with conflicting personas + 1 order
├── dashboard/       existing frontend — untouched, consumes /api
└── backend/
    ├── pyproject.toml        uv, python 3.12
    ├── .env.example
    ├── app/
    │   ├── domain/    shared types + Protocols. imports nothing.
    │   ├── policy/    decides. imports only domain. no clock, no network, no model.
    │   ├── tools/     THE BOUNDARY. the only place a mutation meets policy.
    │   ├── agent/     prompts + post-call extraction. content, never authority.
    │   ├── vapi/      the phone: assistant composition, outbound client, webhooks.
    │   ├── store/     Supabase. persistence and evidence. obeys, never decides.
    │   ├── notify/    what goes out in writing: email + WhatsApp.
    │   ├── api/       the dashboard's REST surface (authenticated humans).
    │   ├── config.py  the only reader of os.environ.
    │   ├── jobs.py    the clock: deadline sweep, RFQ timeout.
    │   └── main.py    the wiring.
    ├── tests/         test_layering.py IS the architecture; fakes.py; fixtures/
    └── scripts/       seed.py · sim_tools.py · replay_webhook.py
```

**The sentence:** *Eight folders. One holds the types, one decides, one is the boundary every
mutation crosses, and the rest talk to somebody — the phone, the database, the outbox, the
dashboard. The one that decides cannot import any of the ones that listen, and a test proves
it.*

The contract, in `tests/test_layering.py` (extends the map in `STRUCTURE (2).md` §3):

```python
ALLOWED = {
    "domain": set(),
    "config": set(),
    "policy": {"domain"},
    "agent":  {"domain"},
    "store":  {"domain", "config"},
    "notify": {"domain", "config"},
    "tools":  {"domain", "policy", "store", "notify"},
    "vapi":   {"domain", "config", "agent", "tools"},
    "api":    {"domain", "config", "tools", "store"},
    "jobs":   {"domain", "config", "tools", "vapi"},
}
```

`policy/` is a sink: it cannot import `vapi/` (so it cannot reach a model), cannot import
`store/` or `notify/` (so it cannot reach the network), cannot import `agent/` (so no prompt
text reaches it). `vapi/` cannot import `store/` — every write from a webhook or tool call
goes through `tools/`, which is where policy lives. That one edge makes "the LLM never writes
a commitment" a property of the graph rather than a rule someone has to remember.

### Files inside the packages that carry the new work

```
tools/
  model.py        the five tools exposed to the LLM. propose/report/verify only.
  market.py       carrier selection, ranking, comparison, the single-award lock.
  calls.py        idempotent call-lifecycle writes from webhooks.
  commitments.py  the commitment state machine + the recap-delivery gate.
  parse.py        spoken money/date parsing, ported from conversation_guard.py.
vapi/
  assistant.py    composes a transient assistant from CompanyProfile + CallContext.
  client.py       POST https://api.vapi.ai/call — the only code that spends money.
  webhook.py      POST /vapi/events — one router, switch on message.type.
  toolserver.py   POST /vapi/tools — dispatch to tools/model.py, always HTTP 200.
  campaign.py     parallel dial fan-out (asyncio.gather + semaphore).
```

### What ports over from `nextwave` unchanged

| File | Why |
| --- | --- |
| `app/policy/engine.py` | The reference monitor: `evaluate_quote`, `require_preagreement_evidence`, `select_best`. Already pure, already `now`-injected. |
| `app/domain/security.py` | `Mandate`, `QuoteProposal`, `PolicyDecision`, `ReasonCode`, `CostComponent`, `PreparedCommitment`. |
| `app/domain/company.py` | `CompanyProfile` — the prompt composer's input. |
| `app/agent/prompts.py`, `agent/context.py` | 648 lines of tuned prompt + `CallPhase`. Needs one new phase (below). |
| `app/tools/conversation_guard.py` | Keep `_spoken_integer`, `_money`, `_date` as `tools/parse.py`; drop the token-stream filter (Vapi owns the stream). |
| `tests/test_ugly_cases.py` + `docs/UGLY_CASES.md` | 20 rows. Rows 1–7 are what the judge will try. |

### What is deleted

`voice/*` (session, vad, frames, llm, stt/, tts/, simline, latency, speech_budget),
`telephony/*` (router, stream, twiml, outbound, handoff, idempotency), the 30-table schema on
`origin/martin`, and decisions D9–D71 of the old log (FX percentile models, envelope
encryption, banking calendars — none of it reachable from the three flows).

### One change to `prompts.py`

Add `CallPhase.STATUS_CHECK` and a `_STATUS_CHECK` block for OUTBOUND 2, shaped like
`_RENEGOTIATION`: state what stands, ask what happened, require a new ETA as an explicit clock
time *and* calendar date, never accept a price change or approve detention, escalate anything
else. Add its two greetings to `_GREETINGS`. Five phases total.

---

## 2. Database schema

Ten tables. The rule from `DATABASE.md` §1 holds: **a table exists if a named writer fills
it**. Each table below names its writer; everything with no writer collapsed into a column or
JSONB.

Money is `bigint` cents plus an explicit `char(3)` currency — never float, never a bare
amount. `revoke update, delete` on the four append-only tables, so not even the backend can
rewrite its own refusals.

### Seeded before the first call

**`carriers`** — writer: `seed.sql` / dashboard
`id` · `name` · `phone text unique` (E.164 — **this unique index is how an inbound call is
correlated**) · `contact_name` · `email` · `whatsapp` · `is_on_file bool` (false ⇒ the agent
refuses to quote; Volta never onboards anyone by phone) · `persona text` (cheap-and-slow,
never-answers — without conflicting personalities the comparison proves nothing) ·
`is_active` · `created_at`

**`orders`** — writer: `POST /api/orders`, then the state machine in `tools/`
The shipment *and* its mandate *and* its clocks. Absorbs `mandates`, `operation_clocks`,
`rfqs`, `appointments`.

| Column | Note |
| --- | --- |
| `id` uuid pk | |
| `reference text unique` | The folio. Also identity proof level 1 on an inbound call: only a real counterparty knows it. |
| `status text` | `received · quoting · awaiting_approval · awarding · booked · in_transit · at_risk · delivered · closed · cancelled` |
| `origin` `destination` `cargo` `equipment` `weight` | Rendered into the prompt verbatim. |
| `container_number` | `check` — ISO 6346 check digit. |
| `discharged_at` `free_days` `last_free_day` | Demurrage. Starts by itself; nobody decides it. |
| `delivery_deadline timestamptz` | **The OUTBOUND 2 trigger.** |
| `cap_amount bigint` `cap_currency char(3)` | The mandate ceiling, in cents. |
| `target_amount bigint` | Negotiation target. Never spoken. |
| `pickup_not_before` / `_not_after` | The authorized window. Outside it there is no permission. |
| `commitment_mode text` | `autonomous` \| `human_escalation` |
| `mandate_version int` | Bumped on every mandate write. Decisions copy the cap by value, so an old decision stays explainable. |
| `mandate_set_by` `mandate_set_at` | Step 4 — who authorized, and when. |
| `assigned_carrier_id` → carriers | Set on award. The number OUTBOUND 2 dials. |
| `expected_driver` `expected_plate` | Inbound verification. Checked against, never read out. |
| `awarded_quote_id` → quotes | |
| `payload jsonb` | BL, pedimento, terminal, warehouse address. |
| `created_at` `updated_at` | |

No separate `mandates` table: one mandate per order, and `decisions.cap_at_decision` gives the
audit trail versioning would have given.

### Written during a call

**`calls`** — writer: `tools/calls.py`, from `vapi/webhook.py`
`id` · `vapi_call_id text unique` (**idempotency — a redelivered webhook cannot create a
second call**) · `direction inbound|outbound` · `phase rfq|award|renegotiation|inbound|status_check`
· `order_id` (null until an inbound call is correlated) · `carrier_id` (null if the number is
not on file — *that is already information*) · `from_number` `to_number` · `status
queued|ringing|active|ended|failed` · `ended_reason` · `started_at` `ended_at` ·
`recording_url` · `transcript jsonb` · `context jsonb` (the exact `CallContext` the prompt was
built from — a call is replayable only if this is stored) · `identity_verified bool`
`identity_level smallint 0-3` · `cost_cents`

`transcript` as JSONB replaces an `utterances` table: Vapi returns the transcript once, at end
of call, as one array. Nothing queries it line by line.

**`quotes`** — writer: `tools/model.py::propose_quote`
What a carrier said it would do and for how much. **A changed quote is a new row, never an
edit** (ugly case #2 — overwriting deletes exactly the fact the judge will probe).

`id` · `order_id` · `carrier_id` · `call_id` · **`anchor_ms int not null`** · `amount_cents`
`currency` · `components jsonb` · `cost_is_final bool default false` (default false so silence
blocks: "plus tolls" means the total is not final, and a non-final total cannot be authorized)
· `pickup_at` `pickup_window_end` · `equipment` · `valid_until` · `all_in_usd_cents`
`fx_margin_bps` · `status proposed|superseded|withdrawn|selected|accepted|rejected` ·
`superseded_by` → quotes · `carrier_confirmed_exact_recap bool` `confirmed_at` ·
`claimed_identity` `identity_level`

`unique (order_id) where status = 'accepted'` — **exactly one award, enforced by the
database.** Two open bookings is the worst failure in the brief.

**`decisions`** — writer: `tools/`, on every `policy.evaluate()`. Append-only.
Every evaluation *including the refusals* — literally what you show the jury when the agent
says no.
`id` · `order_id` `call_id` `quote_id` · `proposal jsonb` (copy of the input; without it the
decision cannot be reproduced) · `outcome allow|deny|escalate` · `reason_code` ·
**`cap_at_decision bigint` `cap_currency` `mandate_version`** (copied by value — someone
raising the cap ten minutes later does not rewrite this decision) · `decided_at`

**`events`** — writer: every mutating path. Append-only.
`id` · `order_id` `call_id` · `type` · `payload jsonb` · **`idempotency_key text unique`** ·
`created_at`. With `on conflict do nothing` this is atomic: a redelivered webhook has no
window to slip through.

### Written after a call

**`call_reports`** — writer: `agent/report.py`, post-call. One per call.
Separate from `calls` because a different writer fills it at a different confidence level:
`calls` holds what the vendor reported, this holds what a model *understood*.
`call_id pk` · `summary` · **`subject`** (`quote · accident · delay · request · delivered ·
other` — the INBOUND classification and the OUTBOUND 2 outcome) · `severity low|medium|high` ·
`actions` `mentions` jsonb (the brief) · `quoted_prices` `objections` `conditions` jsonb ·
`agreement_candidates jsonb` (the model proposes; policy decides) · `model` · `generated_at`

**`commitments`** — writer: `tools/commitments.py` state machine only.
`id` · `order_id` `quote_id` · `state verbal|recap_sent|committed|superseded|not_committed|executed`
· `evidence_call_id` · **`evidence_anchor_ms int NOT NULL`** — this one `NOT NULL` replaces a
fifteen-line trigger: if a commitment cannot exist without an audio offset, nothing has to
police its absence · `terms jsonb` · `canonical_sha256` · `claimed_identity` `identity_level` ·
`superseded_by` → commitments (a renegotiation creates a new one; it never edits the old) ·
`approval_id` · `created_at`

`unique (order_id) where state not in ('superseded','not_committed')` — one live commitment.

**`approvals`** — writer: `tools/`, resolved by the dashboard.
**One human inbox for three things** that all mean "a person must look at this": the award
decision (step 9), a mid-call escalation, and a deadline breach. Unifying them is what makes
the portal one screen instead of three.
`id` · `order_id` `call_id` · `kind award_approval|escalation|incident` · `reason`
(`outside_mandate`, `direct_request`, `identity_unverified`, `conflicting_information`,
`policy_failure`, `deadline_breach`, `carrier_reported_incident`) · `context jsonb` (for an
award: the full ranked comparison; for an escalation: enough for a human to take a live call
without reading a transcript) · `status open|approved|rejected|handled|expired` · `raised_at`
`decided_at` `decided_by` `note`

**`notifications`** — writer: `notify/`.
The written recap. **Its delivery is what promotes a commitment to `committed`.**
`id` · `order_id` `call_id` `commitment_id` `approval_id` · `channel email|whatsapp` ·
`to_address` · `subject` `body` · `status pending|sent|failed` (`failed` means **there was no
commitment**, not that there was a defective one) · `provider_message_id` · `error` · `sent_at`

> Not tables: `company_profile` (one row → `config.py` + `domain/company.py`), `utterances`
> (→ `calls.transcript`), `fx_rate_snapshots` (only if the mandate stops being single-currency),
> `commitment_transitions` (→ `events` rows), `handoffs` (→ `approvals`).

RLS on every table; the backend uses the service key; the dashboard reads only through `/api`.

---

## 3. System workflow

### How Volta talks to Vapi

Four decisions, all with a doc-verified basis:

1. **The assistant is always transient, composed server-side.** Every call posts a full
   `assistant` object built by `vapi/assistant.py` from `build_runtime_system_prompt(profile,
   context)`. Not `assistantId` + `variableValues`: our composer is already the single source
   of prompt truth, templating adds an injection surface, and Vapi does not persist
   `variableValues` anyway. Nothing about a call depends on dashboard config that can drift.
2. **One server URL, `POST /vapi/events`**, authenticated by `X-Vapi-Secret`; switches on
   `message.type` for `assistant-request`, `status-update`, `end-of-call-report`,
   `transfer-destination-request`. Custom tools carry their own `server.url` →
   `POST /vapi/tools` (tool-level URL takes precedence over assistant-level).
3. **The tool server always answers HTTP 200.** Vapi ignores any other status code
   completely — a 500 fails *open*, the exact opposite of invariant #6. Every handler is
   wrapped: on any internal error it returns 200 with a single-line `error` string that makes
   the agent hold and escalate. `result`/`error` must be strings, single-line, and
   `toolCallId` must match exactly.
4. **The audio anchor is captured server-side when the tool fires**, as
   `now - calls.started_at`, not reconstructed afterwards from the transcript.
   `artifact.messages[].secondsFromStart` is *probable but not doc-confirmed* (and has a
   reported epoch-value bug), so evidence must not depend on it. The end-of-call report is
   reconciled against our anchor when it arrives; a mismatch is an event, not an overwrite.

**The five tools exposed to the model** — the complete mutation surface a stranger on the
phone can reach:

| Tool | Phase | What the server does | What the model gets back |
| --- | --- | --- | --- |
| `propose_quote` | rfq | parse (reject ambiguous amounts) → `policy.evaluate_quote` → write `quotes` + `decisions` | `"Recorded. Nothing is booked."` / `"That is something a person from the team has to look at."` — **never the cap, never a number the model did not hear**, never "approved" |
| `confirm_preagreement` | award | `require_preagreement_evidence` → `commitments` state `verbal` | `"Noted as a pre-agreement, subject to written confirmation."` |
| `verify_caller` | inbound | compares a claimed fact against `orders.reference` / `expected_plate` / `container_number`; sets `calls.identity_verified` | `"matches"` / `"does not match"` — **never echoes the expected value** |
| `lookup_order` | inbound, status_check | read-only; returns nothing unless `identity_verified` | operational fields, or `"I need to verify who I am speaking with first."` |
| `report_incident` | inbound, status_check | writes `events` + `call_reports.subject`; moves `orders.status` only along a whitelisted transition | `"Recorded. I cannot approve that on this call."` |

Escalation uses Vapi's built-in `transferCall`, but the **destination is decided by us**: the
`transfer-destination-request` server message hits `/vapi/events`, where we write the
`approvals` row and return the manager's number — or refuse. `transferPlan.mode =
"warm-transfer-say-summary"` so the human hears the context before being bridged in, which is
the challenge's "a human takes over and receives the context of everything already said". The
call is never hung up.

3 parallel carrier calls fit comfortably in Vapi's default 10 concurrent slots; excess calls
queue (`concurrencyBlocked: true`) rather than error.

### Flow A — OUTBOUND 1: quote, compare, approve, book

```
cargo received ─► mandate ─► RFQ (3 calls in parallel) ─► rank ─► HUMAN ─► award call ─► email
```

1. **Cargo received.** `POST /api/orders` (external integration, or a seeded row). Inserts
   `orders` with `status=received`, `discharged_at`, `last_free_day`. Idempotent on
   `reference`. Writes `events(order.received)`. The portal shows a demurrage countdown —
   which is what makes everything downstream urgent.
2. **The system proposes; the human sets the mandate.** The portal offers "start quoting".
   `POST /api/orders/{id}/mandate` with `cap_amount`, `cap_currency`,
   `pickup_not_before/after`, `delivery_deadline`, `commitment_mode`. Sets `status=quoting`,
   `mandate_version=1`, records who authorized it. **This is the only place a cap is ever
   written** — no path from a phone call reaches it.
3. **Pull the market.** `tools/market.py::plan_rfq` selects carriers: `is_on_file`,
   `is_active`, a phone on file, ≥3 of them. Creates one `calls` row per carrier
   (`phase=rfq`, `status=queued`) with the exact `CallContext` stored in `calls.context`.
4. **Dial in parallel.** `vapi/campaign.py` runs `asyncio.gather` over the plan. Each call's
   context carries `quotes_in_hand` and `best_rate_so_far` **as of dial time** — the fifth
   call negotiates with four numbers behind it, the first with none. The ceiling and target go
   into the prompt under `FIGURES YOU MUST NEVER SAY OUT LOUD`; policy still decides every
   proposal, so a leaked figure is an embarrassment, not an authorization.
5. **During each call** the model calls `propose_quote`. Policy evaluates against the cap
   *copied at that instant*. Above the cap → `escalate / outside_mandate` in `decisions`, the
   agent says a person has to look at it, negotiation continues normally. A re-quote later in
   the same call → a **new** `quotes` row with `superseded_by` set on the old one. Both
   survive.
6. **After each call**, `end-of-call-report` → store recording + transcript → `agent/report.py`
   generates the brief and recap → `call_reports` row. **The portal shows it immediately**
   (step 7): summary, prices quoted, objections, conditions, actions taken, and each agreement
   candidate with its audio offset.
7. **Rank.** When every RFQ call has ended (or `jobs.py` hits the RFQ timeout),
   `tools/market.py::rank` re-runs `policy.evaluate_quote` with a fresh `now`, then
   `select_best` (lowest all-in USD; deterministic tie-breaks on pickup, confirmation time,
   id). It builds the comparison: every quote, its all-in cost, its window fit, and the policy
   verdict *with reason code* — including the ones that lost and why.
8. **Ask the human.** Writes `approvals(kind=award_approval, context=<comparison>)`, sets
   `status=awaiting_approval`, fires `notify` (email + WhatsApp). The comparison is auditable:
   it names the loser and the reason, not just the winner.
9. **Human approves** — `POST /api/approvals/{id}/decision`. Order moves to `awarding` and the
   single-award lock engages: the partial unique index on `quotes` means a second award
   attempt fails in the database, not in application logic.
10. **Award call.** One call, `phase=award`, to the winner. The agent restates the exact terms
    and asks for an explicit yes. `confirm_preagreement` runs `require_preagreement_evidence` —
    no exact-recap confirmation, no `confirmed_at`, no anchor ⇒ `EVIDENCE_MISSING`, no
    commitment. On success: `commitments` state `verbal`, `evidence_anchor_ms` set.
11. **The written commitment.** `notify/` sends the official email: the confirmation-call
    brief, plus **the commitment register — every agreed term with its audio timestamp** and a
    link to the recording. `notifications.status='sent'` is the gate: only then does the
    commitment become `committed` and the order `booked`. A send failure means *there is no
    commitment* — it writes `approvals(kind=escalation, reason=policy_failure)`, and never
    auto-resends (an ambiguous send stays `UNKNOWN`).

**Renegotiation** (`POST /api/orders/{id}/renegotiate`) reuses the same path at
`phase=renegotiation`: what stands is stated first, the new terms are a fresh `quotes` row,
policy re-evaluates against the *current* mandate, and success creates a new `commitments` row
with `superseded_by` pointing at the old one. The old one is never edited.

### Flow B — INBOUND: a carrier calls us

1. Carrier dials the Vapi number. Vapi POSTs `assistant-request` to `/vapi/events` with the
   caller's number at `message.call.from.phoneNumber`.
2. **We have 7.5 seconds, fixed and not configurable.** One indexed lookup on `carriers.phone`
   (unique), one on `orders` by `assigned_carrier_id`. If it does not resolve in ~2s, return
   the unverified-caller assistant and hydrate mid-call via `lookup_order`.
3. Compose a `phase=inbound` transient assistant. If the number is not on file, `carrier_id`
   stays null and the context carries no order — the agent gives nothing away, records every
   claim as unverified, and escalates. If it resolves, the context carries `expected_driver` /
   `expected_plate` / `reference`, which the prompt already forbids reading out: *"a caller who
   is told the plate can repeat the plate."*
4. **Identity.** The agent asks for a name, a company, and the **shipment reference** — the
   folio on the caller's dispatch paperwork. Each attempt goes through `verify_caller`, which
   compares server-side and returns only match / no match, setting `calls.identity_verified`
   and `identity_level`. `lookup_order` returns nothing until that flips. Identity is a datum
   that can only *demand more*, never concede more.

   **Changed 30 Aug.** This was two factors: the folio correlated the call and a second
   operational fact authenticated it. But reaching level 2 also required `call.carrier_id`,
   i.e. the caller's number already in the `carriers` directory — and a driver rings from
   their own mobile, so no real inbound call could ever verify. Every one of them died
   unidentified. A matched fact of any kind now verifies.

   That makes the folio a password, so the second factor is replaced by a metered one:
   `_IDENTITY_ATTEMPT_BUDGET` wrong answers (3) and the call stops answering identity
   questions entirely, raising one `escalation / identity_unverified`. The budget is counted
   from `identity.attempt` events rather than a column, so a redelivered tool call cannot
   inflate it. `UGLY_CASES.md` rows 22 and 23 are the pair; neither may be relaxed alone.
5. **Subject.** The agent calls `report_incident` with `subject ∈ accident | delay | request |
   delivered | other`, the details, and any new ETA as an explicit clock time and calendar date
   (never a weekday, never an inferred number). The server writes `events` and moves
   `orders.status` only along whitelisted transitions — a claimed *delivered* raises an
   approval rather than closing the order; a claimed delay records the claim without moving
   `delivery_deadline`. The agent approves nothing: not detention, not extra cost, not a new
   window, not a cancellation. An above-cap "today only" offer becomes a `decisions` row with
   `outside_mandate` and nothing else.
6. **Report and elevate.** `end-of-call-report` → `call_reports` with `subject` and `severity`.
   High severity, an unverified caller, or a direct request for a person creates
   `approvals(kind=incident|escalation)`, and `notify/` sends the manager the report by email
   and WhatsApp. The portal shows the verified summary with the recording and the anchors.

   Two things this depended on that it must not. The approval used to be raised only when the
   call had correlated to an order, so an incident on an unknown load reached nobody — it is
   now raised either way, with `order_id` null. And the notification used to be a hostage of
   the extraction model: a raised `reporter.report` returned early and `after_report` never
   ran, which on 30 Aug was every call. The brief now degrades to a stub built from the
   caller's own words, marked `HIGH`, and the manager is still told. Not knowing what was said
   is a reason to escalate faster, not slower.

   **Where it surfaces.** Inbound calls appear in the order's own call list beside the
   outbound ones, from the moment the folio correlates them; there is no separate inbound
   screen, because an inbound call is an event on a case. A call that never correlated is
   reachable only through its approval card in the Approvals inbox, which is why that card
   renders the full `approval.context` and links to the transcript.

### Flow C — OUTBOUND 2: the deadline passed

1. `jobs.py` sweeps every 60s inside the FastAPI process:
   ```sql
   select * from orders
    where delivery_deadline < now()
      and status not in ('in_transit','delivered','closed','cancelled')
   ```
   Idempotency comes from `events.idempotency_key = 'chase:{order_id}:{deadline}'` with
   `on conflict do nothing`, so a restart cannot double-dial. `POST /api/jobs/sweep` triggers
   the same code by hand for the demo.
2. One call, `phase=status_check`, to `orders.assigned_carrier_id`'s phone, with the standing
   commitment terms in context.
3. The agent states what was agreed, asks what happened, and requires a new ETA as an explicit
   date and time. It cannot accept a price change or approve detention — those go to a person.
   Tools: `lookup_order`, `report_incident`, `transferCall`.
4. Post-call: `call_reports` (`subject`, `severity`), `orders.status → at_risk`, and
   `approvals(kind=incident, reason=deadline_breach)` with the summary, elevated to the manager
   by email and WhatsApp and shown on the dashboard.

### API surface

**Vapi-facing** (untrusted; `X-Vapi-Secret` verified before anything else):

| | |
| --- | --- |
| `POST /vapi/events` | `assistant-request` · `status-update` · `end-of-call-report` · `transfer-destination-request` |
| `POST /vapi/tools` | the five custom tools. Always 200. |

**Dashboard-facing** (`/api`, authenticated):

| | |
| --- | --- |
| `POST /api/orders` | a cargo was received at port (external integration or manual) |
| `GET /api/orders` · `GET /api/orders/{id}` | list + full aggregate: quotes, calls, commitments, approvals, demurrage countdown |
| `POST /api/orders/{id}/mandate` | **step 4** — cap, window, deadline, mode. The only cap writer. |
| `POST /api/orders/{id}/rfq` | start the market |
| `GET /api/orders/{id}/comparison` | the auditable ranked comparison |
| `POST /api/orders/{id}/renegotiate` | move something already agreed |
| `GET /api/approvals?status=open` | the human inbox — awards, escalations, incidents |
| `POST /api/approvals/{id}/decision` | **steps 9–10** — approve or reject |
| `GET /api/calls?order_id=` · `GET /api/calls/{id}` | brief, transcript, recording, anchors |
| `GET /api/carriers` | |
| `POST /api/jobs/sweep` | manual deadline sweep (demo button) |
| `GET /health` | |

### Environment

`VAPI_API_KEY` · `VAPI_PHONE_NUMBER_ID` · `VAPI_SERVER_SECRET` · `VAPI_MODEL` / `VAPI_VOICE_ID`
/ `VAPI_TRANSCRIBER` (ids verified against current docs, never hardcoded) · `SUPABASE_URL` ·
`SUPABASE_SECRET_KEY` · `OPENAI_API_KEY` + `OPENAI_REPORT_MODEL` (post-call extraction only) ·
`RESEND_API_KEY` + `NOTIFY_FROM_EMAIL` · `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` /
`TWILIO_WHATSAPP_FROM` · `MANAGER_EMAIL` · `ESCALATION_PHONE_NUMBER` · `PUBLIC_BASE_URL`

---

## 4. Build plan — one blocking phase, then five parallel tracks

The tracks are partitioned **by file ownership, not by feature**, so two people never edit the
same file. The mechanism that makes this work is Phase 0: it freezes the types, the Protocols,
the SQL and every function signature, so each track codes against a stub and a fake instead of
against another person's unfinished work.

```
        ┌──────────────────────────────────────────────┐
        │  PHASE 0 — the seam (blocks everyone, ~90m)  │
        └───────────────────────┬──────────────────────┘
                                │  merge to main before anything else starts
   ┌────────────┬───────────────┼───────────────┬────────────────┐
   ▼            ▼               ▼               ▼                ▼
 TRACK A     TRACK B         TRACK C         TRACK D          TRACK E
 Authority   Telephony       Data & API      Language         Market & clock
 domain/     vapi/           supabase/       agent/           tools/market.py
 policy/     (5 files)       store/          notify/          tools/calls.py
 tools/model                 api/                             jobs.py  main.py
 tools/parse                 scripts/seed                     scripts/sim_tools
 tools/commitments
```

### Ground rules for all tracks

- **Never edit a file you do not own.** If you need a signature changed, post it in
  `CHANGELOG.md` with `→ Affects:` and tell the owner. Do not change it yourself.
- **PRs only, merge to `main` every ≤2 hours.** `main` must stay demoable.
- `uv run ruff check . && uv run pytest` green before every push.
- Every track ships with tests that pass **with no network, no DB, and no phone call** — that
  is what lets five people work at once.
- Nothing is a `TODO` in code. If it is not built, its stub raises and its test is skipped with
  a reason.

---

### PHASE 0 — Freeze the seams · one person · ~90 min · everyone is blocked

**Deliverable:** a repo where `uv run pytest` is green, every package exists, every public
function has its real signature and raises `NotImplementedError`, and all ten tables are
migrated.

1. `backend/pyproject.toml` — uv, Python 3.12. Deps: `fastapi`, `uvicorn[standard]`,
   `pydantic`, `pydantic-settings`, `httpx`, `supabase`, `structlog`, `openai`. Dev: `pytest`,
   `pytest-asyncio`, `ruff`, `mypy`.
2. Package skeleton — all eight folders + `config.py`, `jobs.py`, `main.py`, each with a
   docstring naming what it may import.
3. **`app/domain/`** — copy `security.py` and `company.py` from `nextwave` verbatim; add
   `models.py` with `Order`, `Carrier`, `CallRecord`, `QuoteRow`, `CallReport`, `Commitment`,
   `Approval`, `OutboundMessage`, `Turn`, and the enums (`OrderStatus`, `CallPhase`,
   `ApprovalKind`, `IncidentSubject`).
4. **`app/domain/ports.py`** — the Protocols that decouple the tracks. This file is the
   contract; changing it after Phase 0 requires telling everyone.
   ```python
   class Store(Protocol):                       # implemented by Track C
       async def order(self, order_id: str) -> Order | None: ...
       async def order_by_reference(self, ref: str) -> Order | None: ...
       async def carrier_by_phone(self, phone: str) -> Carrier | None: ...
       async def carriers_for_rfq(self, limit: int) -> list[Carrier]: ...
       async def upsert_call(self, call: CallRecord) -> str: ...
       async def call_by_vapi_id(self, vapi_call_id: str) -> CallRecord | None: ...
       async def add_quote(self, quote: QuoteRow) -> str: ...
       async def quotes_for(self, order_id: str) -> list[QuoteRow]: ...
       async def supersede_quote(self, old_id: str, new_id: str) -> None: ...
       async def record_decision(self, d: DecisionRow) -> None: ...
       async def append_event(self, e: EventRow) -> bool: ...   # False = key already seen
       async def raise_approval(self, a: Approval) -> str: ...
       async def save_report(self, r: CallReport) -> None: ...
       async def save_commitment(self, c: Commitment) -> str: ...
       async def due_for_chase(self, now: datetime) -> list[Order]: ...
       async def set_order_status(self, order_id: str, status: OrderStatus) -> None: ...

   class CallPlacer(Protocol):                  # implemented by Track B
       async def place(self, assistant: dict, to_number: str) -> str: ...   # vapi_call_id

   class Notifier(Protocol):                    # implemented by Track D
       async def send(self, message: OutboundMessage) -> DeliveryResult: ...

   class ReportModel(Protocol):                 # implemented by Track D
       async def report(self, turns: list[Turn], context: CallContext) -> CallReport: ...
   ```
5. **`supabase/migrations/0001_init.sql`** — all ten tables from §2, with the three
   load-bearing constraints (`unique(order_id) where status='accepted'`,
   `evidence_anchor_ms not null`, `unique(idempotency_key)`), `revoke update, delete` on the
   four append-only tables, and RLS on everything. **Track C owns every migration after this
   one.**
6. **`app/config.py`** — every env key from §3, all defaulting to `""`.
7. **`tests/test_layering.py`** — the `ALLOWED` map from §1, checked by AST against every
   `app.*` import, plus "a new package must declare its contract" and "the graph is acyclic".
8. **`tests/fakes.py`** — `InMemoryStore`, `FakeCallPlacer` (records dials, returns a fake id),
   `NullNotifier`, `ScriptedReportModel`. **This is the file that makes the tracks
   independent** — everyone develops against it.
9. **`tests/fixtures/vapi/`** — hand-written envelopes for `tool-calls`, `status-update`,
   `end-of-call-report`, `assistant-request`. Marked `PROVISIONAL`; Track B replaces them with
   real payloads at CP4.
10. Stub every function named in the track sections below with its real signature and
    `raise NotImplementedError`.

**Done when:** `uv run pytest` green, `uv run mypy app/` clean, `supabase db push` applied,
merged to `main`.

---

### TRACK A — Authority: policy, the tool surface, the ugly cases

**Owns:** `app/domain/security.py` · `app/policy/` · `app/tools/model.py` · `app/tools/parse.py`
· `app/tools/commitments.py` · `tests/test_policy.py` · `tests/test_ugly_cases.py` ·
`tests/fixtures/hostile/`
**Depends on:** nothing but Phase 0. Develops entirely against `InMemoryStore`.
**This is the track the jury actually scores.** Lens 2 and Lens 4 both land here.

1. Port `policy/engine.py` from `nextwave` verbatim — `evaluate_quote`,
   `require_preagreement_evidence`, `select_best`. Do not add a clock, a network call, or an
   import outside `domain/`. `test_layering.py` will fail if you do.
2. Port the money/date grammar out of `nextwave/app/tools/conversation_guard.py` into
   `tools/parse.py`: keep `_spoken_integer`, `_money`, `_date`; **drop `filter_model_chunk` and
   `input_directive`** (Vapi owns the token stream now). Export
   `parse_amount(text) -> Decimal | Ambiguous` and `parse_date(text, today) -> date | None`.
3. Build `tools/model.py` — the five tools from §3. Each one:
   - takes a validated Pydantic args model,
   - parses (ambiguous ⇒ return the clarification string, write nothing),
   - calls `policy.evaluate_*` with the cap read at that instant,
   - writes `quotes` + `decisions` + `events` through the `Store` Protocol,
   - returns a **single-line string** that never contains the cap, never contains a figure the
     counterparty did not say, and never says "approved" or "booked".
4. Build `tools/commitments.py` — the state machine
   `verbal → recap_sent → committed`, plus `superseded` / `not_committed`. The only writer of
   the `commitments` table. `promote_on_delivery(notification_id)` is the recap gate: a
   `failed` notification leaves the commitment un-promoted and raises an approval.
5. Write all 20 rows of `docs/UGLY_CASES.md` as tests. Priority order: rows 1, 2, 6, 5, 14, 13,
   11 — those are what the judge will try live.

**Definition of done:** `uv run pytest tests/test_ugly_cases.py -v` is 20/20 green with
`InMemoryStore`, no network, no Vapi. A quote of 10,500 against a 9,000 cap produces a
`decisions` row `escalate/outside_mandate`, an `approvals` row, and **no** `commitments` row.

---

### TRACK B — Telephony: everything Vapi

**Owns:** `app/vapi/` (all five files) · `tests/test_vapi_webhook.py` ·
`tests/test_toolserver.py`
**Depends on:** Phase 0 stubs of `tools/` and `agent/`. Develops against
`FakeCallPlacer` + the provisional fixtures.

1. `vapi/assistant.py` — `build_assistant(profile, context) -> dict`. Composes the transient
   assistant: `model` (messages[0] = `build_runtime_system_prompt(...)`), `firstMessage` =
   `build_greeting(...)`, `voice`, `transcriber`, `server.url` + secret, the five
   `functions` with their JSON schemas, `transferCall` with
   `transferPlan.mode="warm-transfer-say-summary"`, and `artifactPlan.recordingEnabled=true`.
   Model/voice/transcriber ids come from `config.py`, **never hardcoded** — verify each against
   current Vapi docs before filling `.env`.
2. `vapi/client.py` — `POST https://api.vapi.ai/call` with `assistant` (transient),
   `phoneNumberId`, `customer.number`. The only code in the repo that spends money. **Never
   call it from a test.**
3. `vapi/toolserver.py` — `POST /vapi/tools`. Parse `message.toolCallList[]`, dispatch by
   `name` to `tools/model.py`, return `{"results":[{"toolCallId": <exact>, "result": <single-line str>}]}`.
   **Wrap every handler: any exception returns HTTP 200 with an `error` string that makes the
   agent hold and escalate.** A non-200 is silently ignored by Vapi and fails open — write the
   test for this first.
4. `vapi/webhook.py` — `POST /vapi/events`, verify `X-Vapi-Secret` before anything else, then
   switch on `message.type`:
   - `assistant-request` → carrier lookup by `message.call.from.phoneNumber` → build the
     inbound assistant. **Hard budget 7.5s**: put a ~2s timeout on the lookup and fall back to
     the unverified-caller assistant.
   - `status-update` → `tools/calls.py::upsert`
   - `end-of-call-report` → store recording + transcript, then hand off to `agent/report.py`
   - `transfer-destination-request` → write the `approvals` row, return the manager's number,
     or refuse.
5. `vapi/campaign.py` — `asyncio.gather` over a plan from `tools/market.py`, with a semaphore
   at 8 (Vapi's default is 10 concurrent slots). Handle `status:"queued"` +
   `concurrencyBlocked:true` as a retry, not an error.

**Definition of done:** `pytest tests/test_toolserver.py` proves a raising handler still
returns 200 with an `error` string; replaying the same `end-of-call-report` fixture twice is a
no-op; `FakeCallPlacer` records three dials from one `campaign.run()`.

---

### TRACK C — Data and the portal API

**Owns:** `supabase/migrations/` (after 0001) · `supabase/seed.sql` · `app/store/` · `app/api/`
· `scripts/seed.py` · `tests/test_store.py` · `tests/test_api.py`
**Depends on:** the `Store` Protocol and the domain types from Phase 0. Nothing else.

1. `store/supabase.py` — implement every method of the `Store` Protocol. The Supabase client is
   sync; run it in a worker thread so it never blocks the event loop (`asyncio.to_thread`).
   **This is the only file in the repo that imports the Supabase client.**
   - `append_event` returns `False` on idempotency-key conflict — use `on conflict do nothing`
     and check the returned row count. Every caller depends on this.
   - `add_quote` + `supersede_quote` must be one transaction.
   - Catch the unique-violation on the accepted-quote partial index and surface it as a typed
     `AwardConflict`, not a raw Postgres error.
2. `app/api/` — all thirteen endpoints from §3. Reads go straight to `store/`; **every mutation
   goes through `tools/`** (that is why `api` may not import `policy`). Response models are
   Pydantic, so the dashboard has five stable JSON shapes.
   - `POST /api/orders/{id}/mandate` is the only cap writer in the system. Bump
     `mandate_version`, record `mandate_set_by`, write an `events` row.
   - `GET /api/orders/{id}` returns the aggregate the portal needs in one call: order, mandate,
     quotes, calls with briefs, commitments, open approvals, demurrage countdown.
3. `supabase/seed.sql` + `scripts/seed.py` — **three carriers with conflicting personas** (one
   cheap and slow, one fast and expensive, one that does not answer) plus one order in
   `received`. Without conflicting personalities the comparison proves nothing.
4. `tests/test_store.py` runs the same suite against `InMemoryStore` and the real client, so
   the fake and the real thing cannot drift.

**Definition of done:** `scripts/seed.py` populates a live Supabase project; `GET
/api/orders/{id}` returns the full aggregate; a duplicate `append_event` returns `False`; a
second award attempt raises `AwardConflict`.

---

### TRACK D — Language and the outbox

**Owns:** `app/agent/` · `app/notify/` · `tests/test_prompts.py` · `tests/test_notify.py`
**Depends on:** `CompanyProfile`, `CallContext`, `CallReport`, `OutboundMessage`,
`DeliveryResult` from Phase 0.

1. Move `prompts.py` into `app/agent/`, add `CallPhase.STATUS_CHECK` and the `_STATUS_CHECK`
   block (§1), plus its two `_GREETINGS` entries and its runtime one-liner in
   `build_runtime_system_prompt`. **Do not move authorization into this file** — it shapes a
   conversation, it decides nothing.
2. Replace `DEMO_PROFILE` / `DEMO_CONTEXT` with a `CompanyProfile` built from `config.py`, and
   a `context_from_order(order, carrier, phase, market_state) -> CallContext` mapper. Keep the
   mapping explicit so adding a mandate field does not silently change what the agent says.
3. `agent/report.py` — `report(turns, context) -> CallReport` using structured outputs against
   `OPENAI_REPORT_MODEL`. Fills `summary`, `subject`, `severity`, `actions`, `mentions`,
   `quoted_prices`, `objections`, `conditions`, `agreement_candidates` — **every candidate
   carries its offset**. This runs post-call with no latency budget. It is evidence, never
   authorization.
4. `notify/` — two senders behind the `Notifier` Protocol: Resend for email, Twilio for
   WhatsApp. **A send failure returns `DeliveryResult(status=FAILED)` and never raises**, so
   the caller can leave a commitment un-promoted.
5. `notify/render.py` — three templates: the **official commitment email** (the confirmation
   brief + the commitment register: every agreed term with its audio timestamp + a recording
   link), the **award approval request** (the ranked comparison), and the **incident report**
   (inbound or deadline breach), with a short WhatsApp variant of the last two.

**Definition of done:** `pytest tests/test_prompts.py` proves all five phases compose and that
no cap figure appears in any greeting; `ScriptedReportModel` fixtures produce a `CallReport`
with anchored candidates; a `NullNotifier` returns `FAILED` rather than raising.

---

### TRACK E — Market, clock, and integration

**Owns:** `app/tools/market.py` · `app/tools/calls.py` · `app/jobs.py` · `app/main.py` ·
`scripts/sim_tools.py` · `scripts/replay_webhook.py` · `tests/test_market.py` ·
`tests/test_jobs.py`
**Depends on:** `policy.select_best` (Track A) and the `Store` Protocol. Develops against
`InMemoryStore` + `FakeCallPlacer`.
This is the **integration owner** — the person who keeps `main.py` wiring the other four
tracks together and keeps `main` demoable.

1. `tools/market.py`:
   - `plan_rfq(order) -> list[DialPlan]` — carrier selection (`is_on_file`, `is_active`, phone
     present, ≥3), one `calls` row each, `CallContext` stored per call.
   - `rank(order) -> Comparison` — re-run `policy.evaluate_quote` with a fresh `now`, then
     `select_best`. The comparison keeps **the losers and their reason codes**, not just the
     winner.
   - `request_award_approval(order, comparison)` — the `approvals` row + `status=awaiting_approval`.
   - `award(order, approval)` — the single-award lock. Set the winning quote to `accepted` and
     let the partial unique index be the enforcement; catch `AwardConflict` and raise an
     approval rather than retrying.
2. `tools/calls.py` — idempotent lifecycle writes from webhooks, keyed on `vapi_call_id` and
   `events.idempotency_key`. Also `anchor_ms(call_id, now)` — the server-side offset that every
   quote and commitment records at the instant its tool fires.
3. `jobs.py` — one asyncio loop started in `main.py`: the 60s deadline sweep (§3 Flow C) and
   the RFQ timeout that closes a market whose last call never ended. Both idempotent via
   `events.idempotency_key`.
4. `main.py` — the composition root. Build the real `Store`, `CallPlacer`, `Notifier`,
   `ReportModel` from `config.py`, mount `api` and `vapi` routers, start the `jobs` loop. Every
   dependency is injected; nothing constructs a client inside a handler.
5. `scripts/sim_tools.py` — **the workhorse of the whole build.** POSTs synthetic Vapi
   `tool-calls` envelopes at `/vapi/tools` and asserts the resulting rows. One scenario per
   ugly case: `boss_approved`, `agreed_then_changed`, `eight_five`, `two_carriers_accept`,
   `silence`, `refusal`. No PSTN, no cost, runs in CI.
6. `scripts/replay_webhook.py` — posts a fixture twice and asserts the second is a no-op.

**Definition of done:** `scripts/sim_tools.py --scenario boss_approved` passes end-to-end
against `InMemoryStore` with no Vapi and no DB; three fake carriers produce a ranked comparison
with a reason code per quote; the sweep dials once and a second sweep dials nothing.

---

### Integration checkpoints

| CP | What must be true | Whose |
| --- | --- | --- |
| **CP1** | `uv run pytest` green on stubs; migrations applied; merged to `main` | Phase 0 |
| **CP2** | `sim_tools --scenario boss_approved` green with `InMemoryStore` — **no Vapi, no DB** | A + E |
| **CP3** | Same scenario against real Supabase; `seed.py` populates | + C |
| **CP4** | One real outbound call reaches `/vapi/events`. **Dump the raw `end-of-call-report` into `tests/fixtures/vapi/` and replace every PROVISIONAL fixture.** Confirm whether `artifact.messages[].secondsFromStart` exists and in what units. | + B |
| **CP5** | Commitment email + WhatsApp actually arrive; the email contains each term with its audio timestamp | + D |
| **CP6** | **Flow A live**: 3 real phones → comparison → approve in the portal → award call → email. Order reaches `booked`. | all |
| **CP7** | **Flows B and C live**, plus the mid-call escalation | all |

CP4 is the one to schedule early even if nothing else is ready — everything downstream of it
is built on assumptions about a payload nobody has seen yet.

---

## 5. Verification

**Offline — no PSTN, no cost, runs in CI**

```bash
cd backend
uv run pytest                                  # everything
uv run pytest tests/test_layering.py           # the ALLOWED map IS the architecture
uv run pytest tests/test_ugly_cases.py -v      # the 20 rows of docs/UGLY_CASES.md
uv run python -m scripts.seed
uv run python -m scripts.sim_tools --scenario boss_approved
uv run python -m scripts.replay_webhook end_of_call --twice
```

Key assertions: `boss_approved` (cap 9,000, caller says 10,500 → a `decisions` row
`escalate/outside_mandate` **and** an `approvals` row, and **no** `commitments` row);
`agreed_then_changed` (two `quotes` rows linked by `superseded_by`, not one edited row);
`eight_five` (the tool returns the clarification string and writes nothing);
`two_carriers_accept` (the partial unique index raises, exactly one award stands);
`/vapi/tools` returns **HTTP 200 with an `error` string** when a handler raises.

**Against the live stack**

1. `uv run uvicorn app.main:app --reload --port 8000` + `ngrok http 8000`. Point the Vapi phone
   number's server URL at `https://<sub>.ngrok.app/vapi/events`. **The ngrok URL changes on
   every restart** — re-point it or inbound calls silently fail.
2. CP4 first: dump a real `end-of-call-report` before building anything on its field names.
3. Seed → mandate → RFQ with three real phones. Assert three parallel `calls`, three
   `call_reports`, a comparison with a reason code per quote, one `approvals` row.
4. Approve in the portal → exactly one award call, one `commitments` row with a non-null
   `evidence_anchor_ms`, one `notifications` row reaching `sent`, order at `booked`. Check the
   email actually contains each commitment with its audio timestamp.
5. Inbound from a seeded carrier's phone and from an unknown phone: `identity_verified` behaves
   differently, `lookup_order` reveals nothing before verification, manager email + WhatsApp
   arrive.
6. OUTBOUND 2: put `delivery_deadline` in the past, `POST /api/jobs/sweep` → one call, one
   report, one `approvals(reason=deadline_breach)`; a second sweep dials nothing.
7. Escalation: "I want to talk to a person" mid-call → the call is *not* hung up,
   `transfer-destination-request` reached `/vapi/events`, an `approvals` row exists, and the
   human hears the summary before being bridged in.

**Deliverables `EVALUATION.md` §3 names and the old repo was missing:** a committed
architecture diagram, a `DECISION_LOG.md` restarted at the ~10 decisions that actually describe
this system, and a `README.md` someone who wasn't there can read.

---

## Open items to settle during implementation

- **Unverified in the Vapi docs** (reference pages exceeded fetch limits): the complete `POST
  /call` body schema and the full set of overridable `assistantOverrides` keys; the default
  `server.timeoutSeconds` for tools (max is 300s — set it explicitly); whether
  `artifact.messages[].secondsFromStart` is official; whether `transferPlan` has its own
  `summaryPlan` object. **CP4 resolves the ones that matter** — schedule it early.
- **Currency.** Policy computes in a single currency. The old repo's FX machinery (snapshots,
  margins, percentiles) is deliberately dropped. If the demo mixes MXN quotes against a USD
  cap, `fx_rate_snapshots` comes back and it is mandatory, not optional — a converted figure
  without the rate that produced it is not verifiable.
- **Track count vs. team size.** With four people, fold Track E into Track A (both live in
  `tools/`) and give `main.py` to whoever owns integration. With three, fold D into C.

---

## 6. How to hand this out

### Step 0 — put the plan where the team can read it

This plan currently lives outside the repo, so nobody else can see it. **First commit of the
project:**

```bash
cd NextWave2.0
git checkout -b chore/phase-0
mkdir docs && git mv CHALLENGE.md DATABASE.md EVALUATION.md docs/
cp "<this plan>" docs/BUILD_PLAN.md
git mv "STRUCTURE (2).md" docs/STRUCTURE.md      # the filename has a space in it today
```

Every instruction below refers to `docs/BUILD_PLAN.md`. Nobody starts until it is on `main`.

### Step 1 — one person does Phase 0 alone

Do not parallelize this. Five people creating `domain/` at once produces five different
`Order` types. Whoever does it announces in the team channel: **"Phase 0 merged, pull main."**

### Step 2 — assign one track per person and send them this

Each teammate gets **one message**. Replace the bracketed parts; everything else is identical.

> **You own TRACK [A].**
>
> ```bash
> git pull origin main
> git checkout -b feat/[a-policy]
> cd backend && uv sync
> uv run pytest        # must be green before you write anything
> ```
>
> Read `docs/BUILD_PLAN.md`. Read **§4 Ground rules**, then **your track section only**, then
> §1 (the folder structure) and §2 (the schema) for reference. You do not need to read the
> other tracks.
>
> **Rules that are not negotiable:**
> 1. Edit only the files under **"Owns"** in your section. Nothing else, not even a typo.
> 2. If you need a signature changed in a file you don't own: **stop**, add an entry to
>    `CHANGELOG.md` with a `→ Affects:` line, and message the owner. Do not edit it yourself.
> 3. Everything you build must pass with **no network, no database, and no phone call** — use
>    `tests/fakes.py`. That is what lets five of us work at once.
> 4. Before every push: `uv run ruff check --fix . && uv run pytest`. Both green.
> 5. PR to `main` at least every 2 hours, even if unfinished. Long-lived branches are how a
>    24-hour build dies.
> 6. No `TODO` comments. If it isn't built, the stub raises and its test is skipped with a
>    reason.
>
> **You are done when** the "Definition of done" line at the bottom of your section is true.
> Then stop and pick up the next unclaimed item — do not expand your own scope.
>
> **Do not:** run `supabase db reset`, edit `supabase/migrations/0001_init.sql`, edit
> `app/domain/ports.py`, or place a real outbound call from a test. Real calls cost money and
> can dial a real number.

If your teammates are driving Claude Code, that message works verbatim as the opening prompt —
it names the file to read, the boundary, and the exit condition, which is everything an agent
needs to not wander.

### Step 3 — the one thing to schedule against the clock

**CP4 is a dependency, not a milestone.** Track B should place one throwaway real call and
commit the raw `end-of-call-report` to `tests/fixtures/vapi/` *as early as possible* — before
its own package is finished. Until that payload exists, every fixture in the repo is a guess,
and Tracks A, D and E are all writing assertions against guesses.

Announce it when it lands: **"real fixtures in, delete your PROVISIONAL ones."**

### Step 4 — the standing meeting is the checkpoint table

Do not run status meetings. Run the CP table in §4: someone reads out which checkpoint is
green, and the room's only decision is what unblocks the next one. CP2 (`sim_tools
--scenario boss_approved` green with fakes) is the moment the project stops being five
separate things.

### If someone finishes early

In priority order, because this is what the jury scores:
1. More rows in `docs/UGLY_CASES.md` + their tests — especially any new way a person finds to
   break the agent by voice.
2. `docs/DECISION_LOG.md` in the *decided / beat / why / would change if* format. A decision
   recorded without the alternative it beat is worth much less.
3. The architecture diagram — `EVALUATION.md` §3 names it as a deliverable and the old repo
   never had one.
4. Rehearsing the trial by fire out loud, with someone playing an uncooperative dispatcher.

Not: a new feature. `EVALUATION.md` §4 says the number of features scores nothing.
