/**
 * A hand-maintained mirror of the backend's response models.
 *
 * Hand-maintained on purpose: the alternative is a codegen step in the build, and a
 * generated client that nobody reads is a worse trade than 200 lines somebody can diff
 * against `backend/app/api/schemas.py` when a field moves. Field names stay snake_case so
 * they match the JSON exactly -- renaming them here would put a translation layer between
 * the two halves of the system for no gain.
 */

export type OrderStatus =
  | 'received'
  | 'quoting'
  | 'awaiting_approval'
  | 'awarding'
  | 'booked'
  | 'in_transit'
  | 'at_risk'
  | 'delivered'
  | 'closed'
  | 'cancelled'

export type QuoteStatus =
  | 'proposed'
  | 'superseded'
  | 'withdrawn'
  | 'selected'
  | 'accepted'
  | 'rejected'

export type CommitmentState =
  | 'verbal'
  | 'recap_sent'
  | 'committed'
  | 'superseded'
  | 'not_committed'
  | 'executed'

export type ApprovalKind = 'award_approval' | 'escalation' | 'incident'
export type ApprovalStatus = 'open' | 'approved' | 'rejected' | 'handled' | 'expired'
export type CommitmentMode = 'autonomous' | 'human_escalation'
export type CallPhase = 'rfq' | 'award' | 'renegotiation' | 'inbound' | 'status_check'

/** Cents plus an explicit ISO 4217 code. Never separated, never a float. */
export interface Money {
  cents: number
  currency: string
}

export interface Turn {
  speaker: string
  text: string
  offset_ms: number | null
}

export interface Carrier {
  id: string
  name: string
  phone: string
  contact_name: string | null
  email: string | null
  whatsapp: string | null
  is_on_file: boolean
  is_active: boolean
  persona: string | null
}

/** The countdown. Computed server-side on the way out so it cannot go stale. */
export interface DemurrageView {
  discharged_at: string | null
  free_days: number | null
  last_free_day: string | null
  days_remaining: number | null
  is_overdue: boolean
}

/** `is_granted: false` means nothing is authorized -- not "no limit". */
export interface MandateView {
  version: number
  cap: Money | null
  target: Money | null
  pickup_not_before: string | null
  pickup_not_after: string | null
  commitment_mode: CommitmentMode
  set_by: string | null
  set_at: string | null
  is_granted: boolean
}

export interface OrderSummary {
  id: string
  reference: string
  status: OrderStatus
  origin: string | null
  destination: string | null
  container_number: string | null
  demurrage: DemurrageView
  mandate: MandateView
  open_approvals: number
}

export interface Order {
  id: string
  reference: string
  status: OrderStatus
  origin: string | null
  destination: string | null
  cargo: string | null
  equipment: string | null
  weight: string | null
  container_number: string | null
  discharged_at: string | null
  free_days: number | null
  last_free_day: string | null
  delivery_deadline: string | null
  cap: Money | null
  target: Money | null
  pickup_not_before: string | null
  pickup_not_after: string | null
  commitment_mode: CommitmentMode
  mandate_version: number
  mandate_set_by: string | null
  mandate_set_at: string | null
  assigned_carrier_id: string | null
  awarded_quote_id: string | null
  expected_driver: string | null
  expected_plate: string | null
  payload: Record<string, unknown>
}

/** A changed quote is a new row, never an edit. `superseded_by` is how they chain. */
export interface QuoteRow {
  id: string | null
  order_id: string
  carrier_id: string
  call_id: string
  anchor_ms: number
  amount: Money
  components: Record<string, unknown>[]
  cost_is_final: boolean
  pickup_at: string
  pickup_window_end: string | null
  equipment: string
  valid_until: string
  all_in_usd_cents: number | null
  status: QuoteStatus
  superseded_by: string | null
  carrier_confirmed_exact_recap: boolean
  confirmed_at: string | null
  claimed_identity: string | null
  identity_level: number
}

export interface CallRecord {
  id: string | null
  vapi_call_id: string
  direction: 'inbound' | 'outbound'
  phase: CallPhase | string
  status: 'queued' | 'ringing' | 'active' | 'ended' | 'failed'
  order_id: string | null
  carrier_id: string | null
  from_number: string | null
  to_number: string | null
  started_at: string | null
  ended_at: string | null
  ended_reason: string | null
  recording_url: string | null
  transcript: Turn[]
  context: Record<string, unknown>
  identity_verified: boolean
  identity_level: number
  cost_cents: number | null
}

/** What a model understood. Evidence for a human, never an authorization. */
export interface CallReport {
  call_id: string
  summary: string
  subject: 'quote' | 'accident' | 'delay' | 'request' | 'delivered' | 'other'
  severity: 'low' | 'medium' | 'high'
  actions: Record<string, unknown>[]
  mentions: Record<string, unknown>[]
  quoted_prices: Record<string, unknown>[]
  objections: string[]
  conditions: string[]
  agreement_candidates: Record<string, unknown>[]
  model: string | null
  generated_at: string | null
}

export interface CallDetail {
  call: CallRecord
  report: CallReport | null
  carrier: Carrier | null
}

/** `evidence_anchor_ms` is not nullable server-side: no anchor, no commitment. */
export interface Commitment {
  id: string | null
  order_id: string
  quote_id: string
  state: CommitmentState
  evidence_call_id: string
  evidence_anchor_ms: number
  terms: Record<string, unknown>
  canonical_sha256: string | null
  claimed_identity: string | null
  identity_level: number
  superseded_by: string | null
  approval_id: string | null
  created_at: string | null
}

/** One inbox for awards, escalations and incidents -- which is what makes it one screen. */
export interface Approval {
  id: string | null
  order_id: string | null
  call_id: string | null
  kind: ApprovalKind
  reason: string
  context: Record<string, unknown>
  status: ApprovalStatus
  raised_at: string | null
  decided_at: string | null
  decided_by: string | null
  note: string | null
}

/** Everything about one order, in one request. */
export interface OrderAggregate {
  order: Order
  mandate: MandateView
  demurrage: DemurrageView
  quotes: QuoteRow[]
  calls: CallRecord[]
  commitment: Commitment | null
  approvals: Approval[]
}

/** Keeps the losers and their reason codes. A comparison naming only the winner is not auditable. */
export interface ComparisonEntry {
  quote_id: string
  carrier_id: string
  carrier_name: string
  amount: Money
  all_in_usd_cents: number | null
  pickup_at: string
  equipment: string
  outcome: string
  reason_code: string
  is_winner: boolean
}

export interface Comparison {
  order_id: string
  entries: ComparisonEntry[]
  winner_quote_id: string | null
  cap_at_decision_cents: number | null
  cap_currency: string | null
  mandate_version: number
  built_at: string
}

export interface SweepResult {
  call_ids: string[]
}

export interface SetMandateRequest {
  cap_amount_cents: number
  cap_currency: string
  target_amount_cents: number | null
  pickup_not_before: string
  pickup_not_after: string
  delivery_deadline: string | null
  commitment_mode: CommitmentMode
  expected_version: number
}

export interface ApprovalDecisionRequest {
  status: 'approved' | 'rejected' | 'handled' | 'expired'
  note: string | null
}
