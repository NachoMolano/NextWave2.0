import type {
  Approval,
  ApprovalDecisionRequest,
  CallDetail,
  Carrier,
  Comparison,
  OrderAggregate,
  OrderSummary,
  SetMandateRequest,
  SweepResult,
  TraceRow,
} from './types'

/**
 * The one place the portal talks to the backend.
 *
 * Every path is under `/api`, which is not decoration: `/vapi` is the surface a stranger on a
 * phone reaches and `/api` is the user-operated portal surface. The backend keeps them in
 * separate packages for exactly that reason. Nothing here should ever call `/vapi`.
 *
 * There is no Supabase client in this app and there must not be one. The database is reached
 * through the API or not at all -- the service key is server-side, and evidence is redacted
 * by code that policy has already seen rather than by a row filter in the browser.
 */

// Empty by default: in development Vite proxies /api to the backend, so the browser makes a
// same-origin request and no CORS policy needs to exist. Set VITE_API_BASE_URL only when the
// portal is deployed somewhere the API is not.
const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? ''

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
    ...init,
  })

  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null)
    const detail =
      typeof payload === 'object' && payload !== null && 'detail' in payload
        ? (payload as { detail?: unknown }).detail
        : null
    // FastAPI validation errors arrive as a list of objects rather than a string.
    const message =
      typeof detail === 'string' ? detail : `The portal API failed with ${response.status}.`
    throw new ApiError(message, response.status)
  }

  return response.json() as Promise<T>
}

export const voltaApi = {
  listOrders: (): Promise<OrderSummary[]> => request('/api/orders'),

  getOrder: (orderId: string): Promise<OrderAggregate> => request(`/api/orders/${orderId}`),

  /**
   * The only price-cap writer in the system. Nothing reachable from a phone call can call it,
   * and the backend versions the cap rather than overwriting it, so a refusal made under the
   * old ceiling stays explainable afterwards.
   */
  setMandate: (orderId: string, body: SetMandateRequest): Promise<OrderAggregate> =>
    request(`/api/orders/${orderId}/mandate`, { method: 'POST', body: JSON.stringify(body) }),

  startRfq: (orderId: string): Promise<OrderAggregate> =>
    request(`/api/orders/${orderId}/rfq`, { method: 'POST' }),

  getComparison: (orderId: string): Promise<Comparison> =>
    request(`/api/orders/${orderId}/comparison`),

  listApprovals: (orderId?: string): Promise<Approval[]> =>
    request(`/api/approvals${orderId ? `?order_id=${encodeURIComponent(orderId)}` : ''}`),

  /** Steps 9 and 10. Approving an award is what engages the single-award lock. */
  decideApproval: (approvalId: string, body: ApprovalDecisionRequest): Promise<Approval> =>
    request(`/api/approvals/${approvalId}/decision`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  listCalls: (orderId: string): Promise<CallDetail[]> =>
    request(`/api/calls?order_id=${encodeURIComponent(orderId)}`),

  getCall: (callId: string): Promise<CallDetail> => request(`/api/calls/${callId}`),

  /** The Decision Trace. Every row is a row in the ledger; nothing here is inferred. */
  getTrace: (callId: string): Promise<TraceRow[]> => request(`/api/calls/${callId}/trace`),

  listCarriers: (): Promise<Carrier[]> => request('/api/carriers'),

  /** The demo button. A second press dials nothing, which is the assertion worth showing. */
  runSweep: (): Promise<SweepResult> => request('/api/jobs/sweep', { method: 'POST' }),
}
