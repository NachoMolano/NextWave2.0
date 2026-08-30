# Drayage Procurement and Carrier-Selection Specification

**Status:** Target contract for the Volta v1 build  
**Last reviewed:** 2026-08-30  
**Authority:** Subordinate to `AGENTS.md`, `docs/BUILD_PLAN.md`, the frozen domain types, and
the applied database migrations. When this document conflicts with executable policy or an
approved decision record, the executable contract and newer decision win.

## Implemented v1 decisions (2026-08-30)

- The live tool path is USD/single-currency only. It supplies no FX snapshots, so mixed-currency
  proposals fail closed and escalate.
- Human escalation is the release mode for v1. `autonomous` remains a schema value but does not
  bypass approval or deterministic policy.
- An award approval is bound to its mandate version and winner. The server recalculates the
  comparison immediately before accepting the quote.
- A verbal pre-agreement can reference only the database-accepted winner and the matching carrier
  call during the AWARD phase.
- The unfinished renegotiation endpoint is not advertised. Historical supersession primitives
  remain available to a future, separately specified workflow.
- Operational exposure stays advisory and unknown unless sourced. No universal tariff,
  demurrage, detention, depot-divergence, or travel-time formula is present.

External tariff feeds, a live FX snapshot writer, and CP4 provider evidence remain outside this
implementation and must not be represented as completed.

## 1. Purpose and safety boundary

Volta collects drayage quotations, preserves what each carrier said, evaluates each proposal
against a human-authored mandate, produces an auditable comparison, and coordinates a single
award path. It does not autonomously invent rates, widen a mandate, treat a late or over-budget
quote as authorized, or convert a phone conversation directly into a booking.

The controlling principle is:

> Speech is probabilistic. Authority is deterministic.

The language model may ask questions, negotiate conversationally, and submit typed proposals.
Only server-side policy may determine eligibility. A carrier can offer terms; it cannot grant
Volta additional authority by claiming urgency, scarcity, or management approval.

This specification distinguishes three values that must not be conflated:

- **Quoted cost:** payable components explicitly stated by the carrier.
- **Projected operational exposure:** an advisory estimate based on sourced tariffs and a
  declared scenario; it is not part of a quote unless the carrier confirms it is payable.
- **Authorized ceiling:** the mandate cap. It is never disclosed to a carrier and is never
  raised by the procurement engine.

## 2. Normative inputs

### 2.1 Order and mandate

The current order record supplies the shipment and the active mandate:

| Field | Required for authorization | Meaning |
| --- | --- | --- |
| `id`, `reference` | Yes | Stable operation identity and human-facing folio. |
| `origin`, `destination`, `cargo`, `equipment` | Yes | Shipment facts used in call context and eligibility. |
| `cap` | Yes | Whole cents plus ISO 4217 currency; no bare amount or float. |
| `pickup_not_before`, `pickup_not_after` | Yes | Inclusive authorized pickup window. |
| `commitment_mode` | Yes | `autonomous` or `human_escalation`; neither bypasses policy. |
| `mandate_version` | Yes | Copied into each decision so later mandate changes cannot rewrite history. |
| `delivery_deadline` | No for quote evaluation | Drives the separate status-check workflow. |
| `discharged_at`, `free_days`, `last_free_day` | Advisory unless contractually sourced | Supports a demurrage/storage countdown, not invented fees. |

A missing mandate means no authority. The system must not interpret a missing cap as unlimited
authority or supply a default pickup window.

### 2.2 Carrier eligibility

A carrier may be called only when it is active, on file, and has a usable E.164 phone number.
Directory identity, contact channels, and `is_on_file` come from trusted stored data, not from
what a caller says. The RFQ plan should include at least three eligible carriers when available;
if fewer exist, the system records the shortage and escalates rather than fabricating candidates.

### 2.3 Quote proposal

Each proposal version must preserve:

- operation, carrier, contact, call, and source-event identifiers;
- one or more cost components, each with an explicit name, exact decimal amount, and ISO
  currency;
- whether the carrier explicitly stated that the cost is final/all-in;
- exact pickup date/time, equipment, and validity deadline;
- a non-negative recording/transcript anchor when the provider supplied one;
- claimed identity separately from trusted identity;
- exact-recap confirmation evidence when present.

A changed amount, component, currency, date, equipment item, condition, or validity deadline
creates a new proposal row. The earlier row is marked superseded and remains readable.

## 3. Parsing and completeness

Model output is not accepted as typed data until deterministic parsing succeeds.

- An ambiguous amount such as “eight five” produces one clarification and no write.
- An amount without an explicit currency is incomplete.
- “Plus tolls,” “fuel extra,” or any unresolved payable component means `cost_is_final=false`.
- A weekday without an unambiguous calendar date must be resolved and read back, or rejected.
- Missing pickup, equipment, or validity information blocks authorization.
- The system never fills a gap with a typical market value, a previous quote, or another
  carrier’s terms.

Clarification responses must not reveal the mandate cap, target, or another carrier’s price.

## 4. Deterministic eligibility evaluation

`evaluate_quote(mandate, proposal, fx_snapshots, now)` is pure: no network, model, database, or
ambient clock. Checks run in this order so the recorded reason is reproducible:

1. Proposal operation equals mandate operation; otherwise `DENY / MANDATE_MISMATCH`.
2. Cost is explicitly final; otherwise `ESCALATE / INCOMPLETE_COST`.
3. Proposal validity has not expired at injected `now`; otherwise `DENY / STALE_EVIDENCE`.
4. Pickup is within the inclusive mandate window; otherwise `DENY / INVALID_WINDOW`.
5. Equipment exactly matches an allowed canonical value; otherwise fail closed with the
   approved outside-mandate reason.
6. Sum every component by original currency using `Decimal`.
7. Compare the comprehensive authorized cost against the cap under Section 5.
8. A cost above the ceiling produces `ESCALATE / OUTSIDE_MANDATE`, never automatic selection.
9. A compliant proposal produces `ALLOW / ALLOWED`; it still is not a booking.

Every decision records the proposal identifier, mandate identifier/version, outcome, reason,
and cost evidence used at that instant.

## 5. Currency policy

The v1 demo is single-currency unless the FX path is deliberately enabled and tested. When the
quote and mandate currencies match, compare exact cents directly.

For mixed currencies, authorization is forbidden unless all of the following exist:

- an immutable snapshot per non-USD currency with source and observation time;
- a supported pair and positive `usd_per_unit` rate;
- a freshness bound enforced against injected `now`;
- a human-approved non-negative margin in basis points;
- upward cent rounding and preserved original totals.

Missing, future-dated, stale, or mismatched FX evidence escalates. A model, caller, or developer
must not invent a current rate merely to make a test or demo pass.

## 6. Ranking and award

Only proposals with `ALLOW` decisions and complete pre-agreement evidence are eligible to win.
The deterministic ranking order is:

1. lowest policy-computed all-in cost;
2. earlier valid pickup time;
3. earlier exact-recap confirmation time;
4. lexicographically stable proposal identifier.

The comparison retains winners, losers, policy outcomes, and reason codes. If no proposal is
eligible, Volta raises `NO_ELIGIBLE_CANDIDATE`; it does not choose the least-bad late or
over-cap option.

RFQ and AWARD remain separate phases. Several carriers may hold proposals, but the database
partial unique constraint permits only one accepted quote per order. A race that attempts a
second award surfaces as `AwardConflict` and escalates.

## 7. Negotiation behavior

Negotiation is conversational guidance, not a pricing authority.

- Let the carrier state a rate first.
- Collect every inclusion, exclusion, pickup term, equipment item, and validity limit.
- Push on price at most once with a non-deceptive operational reason.
- Never disclose the ceiling, target, competing carrier identity, or competing amount.
- Never fabricate a market median, competing bid, congestion statistic, or “standard” fee.
- A counter-offer may be spoken only when it is derived from an explicitly authorized strategy
  and remains within the current mandate. The v1 prompt does not grant the model a general
  formula for generating binding counter-offers.
- A late or over-cap proposal may be recorded and shown to a human, but never auto-awarded.

The original proposal’s percentage-haircut formulas are intentionally excluded: they had no
evidence basis, could reveal the mandate indirectly, and could produce economically irrational
or unauthorized offers.

## 8. Operational exposure estimates

Operational estimates may help a human compare risk, but they are not authoritative quote
components unless the carrier confirms them. Every estimate must carry its source, tariff
version, timezone, calendar convention, assumptions, and confidence/unknown fields.

Use careful terminology because contracts and regions vary:

- **Terminal storage:** charge for occupying terminal ground after terminal free time.
- **Container demurrage:** commonly a carrier/terminal charge while import equipment remains
  inside the terminal beyond free time.
- **Container detention/per diem:** commonly a charge while equipment remains outside the
  terminal beyond free time.
- **Driver waiting:** payable only under the quoted or governing tariff’s free-time and rounding
  rules.
- **Chassis, split, pre-pull, yard, overweight, reefer, hazmat, toll, and fuel charges:** include
  only when explicitly quoted or supported by a governing tariff.

No universal equation can safely infer these costs. Calendar-day versus business-day counting,
partial-day rounding, weekends, holidays, port closure relief, dual transactions, free-time
extensions, and empty-return location are contract-specific. The applicable tariff controls.

Accordingly:

```text
projected_exposure = sum(sourced_scenario_components)
```

Each component is either a quoted payable amount or an advisory scenario amount with provenance.
Unknown values remain unknown; they are not zero.

Hard-coded corrections such as a universal `$150` depot-divergence fee or `2.5 hour` travel
extension are forbidden. A different empty-return depot triggers a route/tariff lookup or human
review, not a fabricated charge.

## 9. Failure modes

| Condition | Required result |
| --- | --- |
| Ambiguous number/date/currency | Clarify; write nothing until resolved. |
| Incomplete all-in cost | Escalate; proposal is ineligible. |
| Pickup outside mandate | Deny or escalate under policy; never auto-select. |
| All candidates late or over cap | Raise approval with full comparison; no winner. |
| Quote changes after recap | New proposal version; preserve and supersede the old one. |
| Two concurrent award attempts | Exactly one database-enforced success. |
| Missing tariff/calendar data | Mark estimate unknown and require review. |
| Provider/network/database unavailable | Fail closed; do not claim booking or delivery. |
| Carrier asks to change detention or price during status check | Record and escalate; do not approve. |

## 10. Verification contract

Offline tests must cover at least:

- exact cap and one cent over cap;
- multi-component totals and missing all-in confirmation;
- ambiguous spoken amounts and missing currency;
- pickup-window boundaries, wrong equipment, and expired validity;
- changed quotes preserved as separate versions;
- deterministic tie breakers and no eligible candidate;
- mixed-currency missing/stale evidence;
- two-carrier award conflict;
- no network, database, or PSTN use from unit tests.

Live operational estimates, tariff lookups, and provider delivery are not proven by fake tests.
They must be labeled `NOT RUN`, `SIMULATED`, `UNKNOWN`, or `COMPLETED` based on actual evidence.

## 11. Known limits and open decisions

- The repository does not yet define a production tariff/calendar feed for storage, demurrage,
  detention, chassis, or congestion projections.
- Mixed-currency authorization is conditional and must remain disabled unless snapshots and the
  approved margin path are wired end to end.
- The commercial/legal effect of the written recap must be reviewed for the deployment
  jurisdiction and customer contract; software state names are not legal conclusions.
- Route optimization, market-price prediction, payments, and automatic mandate expansion are
  outside the v1 scope.
