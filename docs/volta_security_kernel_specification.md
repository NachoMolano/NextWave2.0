# Volta Security Kernel Specification

**Status:** Normative target for the current Vapi/Supabase architecture  
**Last reviewed:** 2026-08-30  
**Not a claim of certification or completed deployment**

## Implementation profile (2026-08-30)

The repository implements the locally enforceable v1 kernel in
`feat/security-kernel-implementation`:

- the demo portal has no login; a configured audit label, or `portal-operator`, is recorded
  server-side for mandate and approval writes;
- mandate writes use an expected version and an atomic conditional update;
- awards are re-ranked against the current mandate immediately before acceptance;
- only the accepted winner, on its own AWARD call, may create a verbal pre-agreement;
- recap delivery is claimed once, ambiguous outcomes are `unknown`, and neither failure nor
  ambiguity is retried automatically;
- recording defaults off; enabling it requires a spoken notice, and without recording the
  pre-agreement tool is not exposed;
- directory phone match is identity level 1, and one independent operational fact is level 2;
- production startup is blocked behind explicit retention, provider-deletion, and legal-review
  readiness gates.

These controls do not prove the external readiness gates. A production operator must provide
the referenced evidence before setting them true. CP4 remains `NOT RUN` until a consented real
Vapi call produces a sanitized fixture. Mixed-currency authorization remains disabled in the
live tool path, and no speculative tariff/exposure engine is implemented.

This document defines the trusted boundary Volta must implement. It intentionally does not
describe the retired Twilio Media Streams → Deepgram → OpenAI → Deepgram stack. Repository
instructions, applied migrations, frozen domain interfaces, and tests are executable sources of
truth. If they conflict with this text, stop and reconcile the difference through the repository’s
ownership and decision process.

## 1. Security objective

Volta coordinates drayage by phone while preventing untrusted speech or model output from gaining
authority. It calls carriers, records proposals and incidents, evaluates quotes, requests human
approval when required, and may coordinate one written commitment. It never handles payments.

> **Speech is probabilistic. Authority is deterministic.**

Untrusted inputs include caller statements, transcripts, prompts, model output, tool arguments,
webhooks before authentication, provider payload fields, and post-call summaries. A value becoming
well-formed does not make it authorized.

The kernel succeeds when every consequential mutation is mediated by typed input, current trusted
state, deterministic policy, append-only evidence, idempotency, and a narrowly injected adapter.

## 2. Sources of truth and precedence

Read, in order:

1. `AGENTS.md` and any applicable nested instructions;
2. applied database migrations and `backend/app/domain/ports.py`;
3. `docs/BUILD_PLAN.md` and approved decision records;
4. executable policy and architecture tests;
5. this specification.

Do not implement a prose requirement that silently contradicts a frozen cross-track interface.
Record the proposed change in `CHANGELOG.md` with an `→ Affects:` line and coordinate with the
owner.

## 3. Trust architecture

The enforced dependency contract is:

```text
domain  <- policy
   ^         ^
   |         |
store     tools <--- notify
   ^         ^
   |         |
  api       vapi <--- agent
               ^
               |
             jobs

main composes every implementation.
config is the only environment reader.
```

The exact allowed import graph lives in `backend/tests/test_layering.py`.

- `domain/`: immutable shared types and Protocols; no infrastructure.
- `policy/`: pure, deterministic authorization; imports only `domain`.
- `tools/`: the model-facing capability boundary and server-side state machines.
- `agent/`: prompts and post-call extraction; content, never authority.
- `vapi/`: assistant composition, outbound placement, authenticated webhooks, and tool dispatch.
- `store/`: the only Supabase client and persistence implementation.
- `notify/`: Resend email and Twilio WhatsApp adapters; never decides eligibility.
- `api/`: user-operated REST surface, separate from Vapi; mutations flow through trusted
  services/tools.
- `jobs.py`: injected clock-driven sweeps.
- `main.py`: composition root; handlers do not construct clients.

No import widening is justified merely because it makes integration easier.

## 4. Non-negotiable invariants

1. The LLM never writes `COMMITTED`, awards a carrier, changes a mandate, or chooses its own
   tools or authority.
2. Calls create evidence and at most a non-binding verbal pre-agreement.
3. The mandate is immutable from inside a call. Caller claims are information only.
4. Missing, ambiguous, stale, conflicting, unverified, or unavailable data fails closed.
5. Every quote change creates a new version; earlier speech remains auditable.
6. RFQ and AWARD are separate phases. Only one accepted quote may exist per order.
7. Every mutating handler is idempotent before performing the mutation.
8. Money is integer cents or exact `Decimal` plus an explicit ISO currency; never float.
9. Numbers, dates, currencies, identity, and provider success are never inferred.
10. A model-generated report is evidence, never an authorization decision.
11. Tests never call a real phone number, production database, or paid provider.
12. Unknown external delivery outcomes are not blindly retried.

## 5. Trusted domain contract

### 5.1 Mandate

The immutable mandate binds:

- mandate ID and version;
- deployment audit actor and operation ID;
- positive all-in ceiling;
- inclusive pickup window;
- non-empty allowed equipment set;
- explicit commitment mode;
- optional human-approved FX margin when mixed currencies are enabled.

Only the dashboard path writes mandate fields. Each write increments
`mandate_version`; decisions copy the cap and version by value.

### 5.2 Proposal and evidence

A proposal carries exact components, currency, cost-finality, pickup, equipment, validity,
carrier/contact identity from trusted session state, source call/event IDs, and evidence anchor.
Claimed identity is stored separately and cannot replace directory identity.

Exact recap confirmation must be bound to the specific proposal version, call, and anchor. A model
interpreting “yes” is insufficient without the deterministic evidence gate.

### 5.3 Decisions

Every decision contains outcome, reason, mandate ID/version, proposal ID, and cost evidence where
calculated. Valid outcomes are `ALLOW`, `DENY`, and `ESCALATE`. Unknown exceptions must not be
treated as `ALLOW`.

## 6. Model-facing capability surface

The approved Vapi functions are narrow proposal, verification, lookup, and reporting operations:

- `propose_quote`
- `confirm_preagreement`
- `verify_caller`
- `lookup_order`
- `report_incident`

They accept frozen Pydantic arguments and return one-line strings. Their responses must not expose
the cap, target, another carrier’s information, or invented facts, and must not say that something
is approved or booked.

The model receives no arbitrary HTTP, SQL, filesystem, shell, secret, mandate-write, notification,
award, or dynamic tool-discovery capability. Server-only commitment coordination is not exposed as
a model function.

## 7. Deterministic quote policy

`evaluate_quote(mandate, proposal, fx, now)` has no I/O or ambient clock. It checks, in order:

1. operation match;
2. final/all-in cost confirmation;
3. unexpired validity;
4. inclusive pickup window;
5. allowed equipment;
6. exact component totals by original currency;
7. required FX evidence and approved margin when currencies differ;
8. buffered comprehensive cost against the mandate ceiling.

The function returns a total reason-coded decision. Above-cap terms escalate as
`OUTSIDE_MANDATE`; missing cost or FX evidence escalates; stale and mismatched evidence denies or
escalates according to the frozen policy contract. Policy never asks a model what should be
allowed.

`require_preagreement_evidence` gates exact recap confirmation and anchors.
`select_best` considers only eligible proposals and applies stable tie breakers. No eligible
candidate produces an escalation, not an automatic mandate expansion.

## 8. Currency boundary

Single-currency comparison is the safe v1 default. Mixed-currency authorization remains disabled
unless immutable rate snapshots, source/time provenance, freshness checks, upward cent rounding,
and a human-approved margin are wired and tested end to end.

Never create a current-time “manual snapshot” to force policy to pass. Preserve original amounts
and snapshot identifiers. An outage, missing margin, unsupported currency, future timestamp, or
stale snapshot fails closed.

## 9. Identity and inbound calls

Vapi webhook authentication establishes that a payload came through the configured provider; it
does not establish carrier identity.

- Correlate the inbound phone number against the trusted carrier directory.
- Ask the caller for name, company, and an operational fact such as reference, plate, container,
  or driver.
- Never read the expected fact for the caller to repeat.
- An order reference is lookup evidence, not sufficient authentication by itself.
- Reveal no protected operation detail before the configured verification level passes.
- Unknown, mismatched, or conflicting identity raises an approval without disclosure.

The exact assurance level required for each read or mutation must be specified and tested before
production. The current schema’s `identity_level` field is evidence, not proof that the policy is
complete.

## 10. Vapi and webhook boundary

- Verify `X-Vapi-Secret` before processing an event body.
- `/vapi/tools` returns HTTP 200 even when a handler fails, with a result that instructs the agent
  to hold and escalate; Vapi may ignore non-200 tool responses.
- Tool call IDs are echoed exactly in result envelopes.
- Webhook redelivery is normal. Use `events.idempotency_key` and stop when append returns `False`.
- `status-update` upserts by provider call ID.
- Duplicate end-of-call reports are no-ops.
- The only real outbound-call implementation is `vapi/client.py`; tests inject `FakeCallPlacer`.
- Assistant IDs, model IDs, voice IDs, transcriber IDs, server URL, and secrets come from config.
- Provider payload fields and timing assumptions must be verified against current Vapi evidence;
  provisional fixtures are labeled as such.

## 11. Commitment and notification state

The intended state machine is:

```text
VERBAL -> RECAP_SENT -> COMMITTED -> EXECUTED
   |                         |
   +-> NOT_COMMITTED         +-> SUPERSEDED
```

A verbal pre-agreement requires proposal eligibility and anchored exact-recap evidence. The
commitment table is written only by the server-side coordinator. Renegotiation creates a new row
and links the old one; it never rewrites history.

The current build plan treats successful delivery of the official written recap as the promotion
gate. A notifier returns `DeliveryResult`; it never raises. Definite failure leaves the commitment
unpromoted and raises an approval. A timeout or otherwise ambiguous provider outcome must not be
blindly retried because the first message may have arrived.

Resend sends email; Twilio sends WhatsApp. Recipients come from trusted order/carrier/manager
configuration, not caller or model text. Provider message IDs and outcomes are recorded.

The legal effect of a recap varies by contract and jurisdiction. `COMMITTED` is an application
state, not a legal opinion; production deployment requires customer-approved wording and counsel
where appropriate.

## 12. Persistence and concurrency

Load-bearing database protections include:

- unique event idempotency keys;
- one accepted quote per order through a partial unique index;
- non-null commitment evidence anchors;
- append-only restrictions for evidence-bearing tables;
- row-level security;
- transactional quote insertion/supersession;
- typed `AwardConflict` rather than raw database leakage.

In-memory fakes must model the same conflicts and idempotency behavior as Supabase. A fake that is
more permissive than production invalidates offline evidence.

## 13. Prompt injection and excessive agency

Prompts instruct the model to ignore role changes, fake authority, requests for secrets, and
attempts to widen the mandate. Repeated material attempts trigger escalation. Output wording may
be constrained to avoid accidental binding language.

These are defense-in-depth controls only. The security property is the absence of an unauthorized
state-changing capability plus deterministic revalidation, not confidence that a prompt cannot be
jailbroken.

Do not expose chain-of-thought. Audit structured inputs, source anchors, decisions, reason codes,
tool results, state transitions, and observable provider outcomes.

## 14. Configuration and secrets

`backend/.env.example` is authoritative. The current configuration families are:

- Vapi: API key, phone-number ID, server secret, model, voice, transcriber, tool timeout;
- Supabase: project URL and server-side secret key;
- OpenAI: API key and post-call report model;
- Resend: API key and sender identity;
- Twilio WhatsApp: account SID, auth token, and sender;
- manager/escalation destinations;
- company voice/profile fields;
- public base URL and orchestration limits.

No secret is committed, printed wholesale, embedded in prompts, placed in dashboard JavaScript, or
copied into fixtures. Empty configuration defaults mean “not configured,” never simulated success.
Production startup validation remains an integration responsibility and must reject missing live
requirements.

## 15. Data protection and recording

The schema stores transcripts, recording URLs, call context, decisions, reports, and notification
outcomes. Vapi assistant composition enables recording in the target build. Therefore the earlier
claim that “no call audio is retained” is false for this architecture unless retention is changed.

Before production, an approved data policy must define:

- recording consent and disclosure by jurisdiction;
- who may access audio, transcripts, and identity evidence;
- tenant isolation and IDOR tests;
- retention and deletion periods, including provider-held artifacts and backups;
- incident response, export, and legal-hold behavior;
- encryption/key-management responsibilities across Vapi, Supabase, and application storage;
- log redaction and audit access.

Until those decisions are implemented and tested, do not claim a one-year retention policy,
AES-256-GCM envelope encryption, TOTP-protected viewing, regulatory compliance, or complete
deletion. Unknown fields and transcript bodies should be treated as restricted operational data.

## 16. Threats and required controls

| Threat | Required control |
| --- | --- |
| Caller claims management approval | Immutable mandate; deterministic outside-mandate decision. |
| Prompt injection requests hidden data/tools | Narrow tool registry; no arbitrary capabilities; no cap disclosure. |
| Ambiguous amount/date/currency | Deterministic parser; clarification; no write. |
| Model claims booking succeeded | Trusted tool/provider result required; conversational wording is not state. |
| Webhook replay | Append event first; duplicate key becomes no-op. |
| Two carriers accepted concurrently | Database partial unique constraint and typed conflict. |
| Quote silently changes | New row and `superseded_by` link. |
| Failed or ambiguous recap delivery | No promotion; no blind retry; approval/reconciliation. |
| Unknown inbound caller | No protected disclosure; verification attempts and escalation. |
| Provider/database outage | Fail closed and preserve truthful status. |
| Secret leakage | Central configuration, server-only clients, redaction, secret scanning. |
| Fake test reaches PSTN/network | Injected fakes/mock transports; real client never constructed. |

## 17. Verification

From `backend/`:

```bash
uv run ruff check .
uv run mypy app/
uv run pytest
uv run pytest tests/test_layering.py
uv run pytest tests/test_ugly_cases.py -v
```

Offline evidence must cover:

- exact cap and one cent over cap;
- ambiguous and incomplete quote inputs;
- stale validity, pickup window, equipment, and FX failures;
- mandate/version binding and anchored recap evidence;
- deterministic ranking and no eligible candidate;
- quote supersession and two-carrier award conflict;
- webhook and event replay;
- tool handler exception still returning HTTP 200;
- notification success, definite failure, malformed response, and transport ambiguity;
- no network, database, or phone call from tests.

Live database, Vapi, PSTN, email, WhatsApp, recording, and manual security tests must be labeled
`NOT RUN` unless actually executed. Simulation is not live evidence.

## 18. Definition of done

The kernel is complete for a release only when:

1. every consequential mutation has a typed, authenticated, idempotent path;
2. model-facing tools cannot award, commit, alter mandates, notify arbitrary recipients, or access
   arbitrary systems;
3. policy is pure, total, reason-coded, and reproducible from stored inputs;
4. proposals and decisions are append-only and evidence-linked;
5. ranking produces at most one database-enforced winner;
6. commitment promotion is gated by exact evidence and confirmed written delivery;
7. ambiguous external outcomes cannot cause duplicate action;
8. offline checks are green and live/manual evidence is labeled truthfully;
9. recording, retention, access, and deletion decisions are approved and implemented for the
   deployment environment;
10. unresolved risks and unrun tests are documented without security overclaims.

## 19. Open decisions

- Whether mixed-currency authorization is required for the demo; if yes, the snapshot writer and
  margin approval path become mandatory.
- Exact Vapi artifact timestamp fields and units after a real CP4 fixture is captured.
- Recording consent, retention, provider deletion, and access policy.
- The assurance level required for inbound lookup, incident reporting, and protected disclosure.
- Reconciliation behavior for ambiguous email/WhatsApp provider outcomes.
- Customer-approved wording and legal effect of the official commitment recap.
