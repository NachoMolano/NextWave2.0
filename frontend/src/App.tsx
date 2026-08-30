import { useCallback, useEffect, useState } from 'react'

import { ApiError, voltaApi } from './api'
import type {
  Approval,
  BusinessProfile,
  BusinessProfileUpdate,
  CallDetail,
  CallRecord,
  Carrier,
  Commitment,
  Comparison,
  MandateView,
  Money,
  OrderAggregate,
  AnchoredNote,
  CallReport,
  NextAction,
  OrderSummary,
  QuoteRow,
  Session,
  SetMandateRequest,
  TraceCategory,
  TraceResult,
  TraceRow,
} from './types'

/* ------------------------------------------------------------------ routing */

type Route =
  | { name: 'orders' }
  | { name: 'order'; orderId: string }
  | { name: 'call'; orderId: string; callId: string }
  | { name: 'approvals' }
  | { name: 'carriers' }
  | { name: 'profile' }

function parseRoute(): Route {
  const path = window.location.hash.replace(/^#/, '') || '/'
  const parts = path.split('/').filter(Boolean)
  if (parts[0] === 'orders' && parts[1] && parts[2] === 'calls' && parts[3]) {
    return { name: 'call', orderId: parts[1], callId: parts[3] }
  }
  if (parts[0] === 'orders' && parts[1]) return { name: 'order', orderId: parts[1] }
  if (parts[0] === 'approvals') return { name: 'approvals' }
  if (parts[0] === 'carriers') return { name: 'carriers' }
  if (parts[0] === 'profile') return { name: 'profile' }
  return { name: 'orders' }
}

function useRoute(): [Route, (path: string) => void] {
  const [route, setRoute] = useState<Route>(parseRoute)
  useEffect(() => {
    const onChange = () => setRoute(parseRoute())
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  const navigate = useCallback((path: string) => {
    window.location.hash = path
  }, [])
  return [route, navigate]
}

/* ---------------------------------------------------------------- formatting */

function formatMoney(money: Money | null): string {
  if (!money) return '—'
  return `${(money.cents / 100).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${money.currency}`
}

function formatDate(value: string | null): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString('en-GB', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatDay(value: string | null): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })
}

/**
 * A `datetime-local` input speaks the operator's wall clock and nothing else -- no zone, no
 * offset. `toISOString` on the way back stamps the zone the browser is actually in, so a
 * pickup window typed in Tampa arrives at the API as the instant that person meant.
 */
function toLocalInput(value: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}` +
    `T${pad(value.getHours())}:${pad(value.getMinutes())}`
  )
}

/** Anchors are what make a claim checkable, so they are rendered, not hidden. */
function formatOffset(offsetMs: number | null): string {
  if (offsetMs === null) return '--:--'
  const total = Math.floor(offsetMs / 1000)
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
}

function statusClass(value: string): string {
  return `status-badge status-${value.replace(/_/g, '-')}`
}

function humanise(value: string): string {
  return value.replace(/_/g, ' ')
}

/* --------------------------------------------------------------------- shell */

export default function App() {
  const [route, navigate] = useRoute()
  const [session, setSession] = useState<Session | null>(null)

  // Whose name the next approval will carry. Worth stating rather than leaving an operator
  // to find out afterwards from the audit trail.
  useEffect(() => {
    voltaApi
      .getSession()
      .then(setSession)
      .catch(() => setSession(null))
  }, [])

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#/">
          <span className="brand-mark">V</span>
          Volta
        </a>
        <p className="eyebrow sidebar-eyebrow">Drayage control</p>
        <nav className="navigation">
          <NavItem
            active={route.name === 'orders' || route.name === 'order' || route.name === 'call'}
            label="Operations"
            onClick={() => navigate('/')}
          />
          <NavItem
            active={route.name === 'approvals'}
            label="Approvals"
            onClick={() => navigate('/approvals')}
          />
          <NavItem
            active={route.name === 'carriers'}
            label="Carriers"
            onClick={() => navigate('/carriers')}
          />
          <NavItem
            active={route.name === 'profile'}
            label="Business"
            onClick={() => navigate('/profile')}
          />
        </nav>
        <div className="sidebar-footer">
          <span className="source-dot" />
          <span>Live</span>
          <small>Every figure on this screen came from the API. Nothing here is fixture data.</small>
          {session && (
            <>
              <span className="source-dot source-dot-actor" />
              <span>Acting as {session.actor}</span>
            </>
          )}
        </div>
      </aside>

      <div className="content-shell">
        <header className="topbar">
          <p className="topbar-copy">Speech proposes. Policy decides.</p>
          <span className="source-label">source · /api</span>
        </header>
        {route.name === 'orders' && <OrdersPage onOpen={(id) => navigate(`/orders/${id}`)} />}
        {route.name === 'order' && (
          <OrderDetail
            orderId={route.orderId}
            carrierCount={session?.rfq_carrier_count ?? null}
            onBack={() => navigate('/')}
            onOpenCall={(callId) => navigate(`/orders/${route.orderId}/calls/${callId}`)}
          />
        )}
        {route.name === 'call' && (
          <CallEvidencePage
            callId={route.callId}
            onBack={() => navigate(`/orders/${route.orderId}`)}
          />
        )}
        {route.name === 'approvals' && <ApprovalsPage onOpen={(id) => navigate(`/orders/${id}`)} />}
        {route.name === 'carriers' && <CarriersPage />}
        {route.name === 'profile' && <ProfilePage />}
      </div>
    </div>
  )
}

function NavItem({
  active,
  label,
  onClick,
}: {
  active: boolean
  label: string
  onClick: () => void
}) {
  return (
    <button className={active ? 'nav-item nav-item-active' : 'nav-item'} onClick={onClick}>
      <span className="nav-indicator" />
      {label}
    </button>
  )
}

function Loading({ what }: { what: string }) {
  return (
    <div className="loading-state">
      <span className="loading-mark" />
      loading {what}
    </div>
  )
}

function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="page">
      <div className="error-state">
        <h2>That did not work</h2>
        <p>{message}</p>
        {onRetry && (
          <button className="secondary-button" onClick={onRetry}>
            Try again
          </button>
        )}
      </div>
    </div>
  )
}


/* ------------------------------------------------------- where are we, and whose move */

/**
 * The stage strip. "Where are we" answered by position rather than by reading a status word
 * and knowing what it implies -- `quoting` and `awaiting_approval` are both "in progress" to
 * the schema and opposite things to a person.
 */
function StageStrip({ action }: { action: NextAction }) {
  const stages = ['Received', 'Mandate', 'Market', 'Comparison', 'Award', 'In transit']
  return (
    <ol className="stage-strip">
      {stages.map((stage, index) => (
        <li
          key={stage}
          className={
            index < action.stage_index
              ? 'stage stage-done'
              : index === action.stage_index
                ? 'stage stage-current'
                : 'stage'
          }
        >
          <span className="stage-dot" />
          <span className="stage-name">{stage}</span>
        </li>
      ))}
    </ol>
  )
}

/** Who is holding this, said in one word rather than inferred from a status. */
function ActorBadge({ action }: { action: NextAction }) {
  if (action.actor === 'nobody') return <span className="actor actor-nobody">Closed</span>
  return (
    <span className={action.actor === 'operator' ? 'actor actor-you' : 'actor actor-volta'}>
      {action.actor === 'operator' ? 'Your move' : 'Volta is working'}
    </span>
  )
}

/** The one thing to do next. A button when it is ours, a status when it is not. */
function NextActionPanel({ action, onAct }: { action: NextAction; onAct?: () => void }) {
  const mine = action.actor === 'operator'
  return (
    <div className={`next-action next-action-${action.urgency}`}>
      <div className="next-action-copy">
        <div className="next-action-head">
          <ActorBadge action={action} />
          <span className="next-action-stage">{action.stage}</span>
        </div>
        <strong>{action.label}</strong>
        <span>{action.detail}</span>
      </div>
      {mine && onAct && (
        <button className="primary-button next-action-button" onClick={onAct}>
          {action.label}
        </button>
      )}
    </div>
  )
}

/* -------------------------------------------------------------- the queue */

function OrdersPage({ onOpen }: { onOpen: (orderId: string) => void }) {
  const [orders, setOrders] = useState<OrderSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    voltaApi
      .listOrders()
      .then((value) => {
        setOrders(value)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(load, [load])

  if (error) return <ErrorState message={error} onRetry={load} />
  if (!orders) return <Loading what="operations" />

  return (
    <div className="page">
      <div className="page-heading">
        <p className="eyebrow">Operations</p>
        <h1>
          The clock is <em>already running</em>
        </h1>
        <p>
          Demurrage starts at discharge. Nobody decides it and nothing pauses it, which is why
          the countdown is the first thing on the row.
        </p>
      </div>

      <section className="queue-section">
        <div className="queue-heading">
          <div>
            <p className="eyebrow">Queue</p>
            <h2>Containers on the ground</h2>
          </div>
          <span className="count">{orders.length}</span>
        </div>

        {orders.length === 0 ? (
          <div className="empty-market">
            <strong>Nothing received yet.</strong>
            <p>Run the seed, or POST an order to /api/orders.</p>
          </div>
        ) : (
          <div className="operation-list">
            {orders.map((order) => (
              <button
                className={`operation-row operation-${order.next_action.urgency}`}
                key={order.id}
                onClick={() => onOpen(order.id)}
              >
                <div className="operation-route">
                  <span className="reference">{order.reference}</span>
                  <strong>
                    {order.origin ?? '—'} → {order.destination ?? '—'}
                  </strong>
                  <span>{order.container_number ?? 'no container number'}</span>
                </div>
                <div className="operation-stage">
                  <ActorBadge action={order.next_action} />
                  <strong className="operation-action">{order.next_action.label}</strong>
                  <span>{order.next_action.detail}</span>
                </div>
                <div
                  className={
                    order.demurrage.is_overdue || (order.demurrage.days_remaining ?? 99) <= 1
                      ? 'operation-clock operation-clock-urgent'
                      : 'operation-clock'
                  }
                >
                  <strong>
                    {order.demurrage.days_remaining === null
                      ? '—'
                      : `${order.demurrage.days_remaining}d`}
                  </strong>
                  <small>
                    {order.demurrage.is_overdue ? 'past last free day' : 'until demurrage'}
                  </small>
                </div>
                <span className="row-arrow">→</span>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

/* ------------------------------------------------------------ order detail */

function OrderDetail({
  orderId,
  carrierCount,
  onBack,
  onOpenCall,
}: {
  orderId: string
  carrierCount: number | null
  onBack: () => void
  onOpenCall: (callId: string) => void
}) {
  const [data, setData] = useState<OrderAggregate | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ text: string; bad: boolean } | null>(null)
  const [busy, setBusy] = useState(false)
  // Lifted out of the panel so the next-action button at the top of the page can open the
  // same form. Two buttons saying "Grant a mandate" that open different things is worse than
  // one form with two ways in.
  const [mandateFormOpen, setMandateFormOpen] = useState(false)

  const load = useCallback(() => {
    voltaApi
      .getOrder(orderId)
      .then((value) => {
        setData(value)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [orderId])

  useEffect(load, [load])

  // The market moves while a person is looking at it: calls end, quotes arrive, an approval
  // is raised. Polling keeps the screen honest without the operator reaching for reload.
  useEffect(() => {
    const poll = window.setInterval(load, 5000)
    return () => window.clearInterval(poll)
  }, [load])

  const act = useCallback(
    async (what: string, run: () => Promise<unknown>) => {
      setBusy(true)
      setNotice(null)
      try {
        await run()
        setNotice({ text: what, bad: false })
      } catch (e) {
        const err = e as ApiError
        setNotice({ text: err.message, bad: true })
      } finally {
        setBusy(false)
        // Reloaded even when it threw. Granting a mandate and opening the market is two
        // requests in one gesture, so a failure on the second leaves real state the first
        // one wrote -- and a screen still showing "nothing is authorized" would be lying.
        load()
      }
    },
    [load],
  )

  if (error) return <ErrorState message={error} onRetry={load} />
  if (!data) return <Loading what="the operation" />

  const { order, mandate, demurrage, quotes, calls, commitment, approvals } = data

  const openMarket = () =>
    act('The market is open. Carriers are being dialled.', () => voltaApi.startRfq(orderId))

  /**
   * Step 4 and step 5 in one press, which is how an operator thinks about it: *quote this at
   * this ceiling, for this window*. They stay two requests because they are two different
   * acts in the ledger -- the mandate is authority and the RFQ is spending it -- and because
   * a dial that fails must not take the granted mandate down with it.
   */
  const grantMandate = (body: SetMandateRequest, dial: boolean) => {
    setMandateFormOpen(false)
    const told = dial
      ? `Mandate v${mandate.version + 1} recorded. ` +
        `${carrierCount ?? 'The'} carriers are being dialled.`
      : `Mandate v${mandate.version + 1} recorded. Nobody was dialled.`
    return act(told, async () => {
      await voltaApi.setMandate(orderId, body)
      if (dial) await voltaApi.startRfq(orderId)
    })
  }

  // The panel's own button is the only one wired for actions we can actually perform here.
  // An approval is decided in its own card lower down, so offering a second button for it
  // would be a control that scrolls somewhere rather than doing something.
  const nextActionHandler =
    data.next_action.label === 'Grant a mandate'
      ? () => setMandateFormOpen(true)
      : data.next_action.label === 'Open the market'
        ? openMarket
        : undefined

  return (
    <div className="page">
      <button className="back-button" onClick={onBack}>
        ← All operations
      </button>

      {notice && (
        <div className={notice.bad ? 'command-notice command-denied' : 'command-notice'}>
          <span>{notice.bad ? '!' : '✓'}</span>
          <p>{notice.text}</p>
          <button onClick={() => setNotice(null)}>×</button>
        </div>
      )}

      <div className="operation-hero">
        <div>
          <p className="eyebrow reference">{order.reference}</p>
          <h1>
            {order.origin ?? '—'} → {order.destination ?? '—'}
          </h1>
          <p>
            {order.cargo ?? 'cargo unstated'} · {order.equipment ?? 'equipment unstated'} ·{' '}
            {order.container_number ?? 'no container number'}
          </p>
        </div>
        <div className="hero-state">
          <span className={statusClass(order.status)}>{humanise(order.status)}</span>
          <span className={demurrage.is_overdue ? 'countdown urgent' : 'countdown'}>
            {demurrage.days_remaining === null
              ? 'no clock'
              : `${demurrage.days_remaining}d to last free day`}
          </span>
          <small>Last free day {formatDay(demurrage.last_free_day)}</small>
        </div>
      </div>

      <StageStrip action={data.next_action} />
      <NextActionPanel action={data.next_action} onAct={nextActionHandler} />

      <div className="detail-grid">
        <div className="detail-main">
          <MarketPanel
            quotes={quotes}
            mandateGranted={mandate.is_granted}
            marketOpen={order.status !== 'received'}
            busy={busy}
            onStart={openMarket}
          />

          <section className="surface call-section">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Evidence</p>
                <h2>Calls</h2>
              </div>
              <span className="count">{calls.length}</span>
            </div>
            {calls.length === 0 ? (
              <p className="empty-copy">No calls yet.</p>
            ) : (
              <div className="compact-calls">
                {calls.map((call) => (
                  <CallRow key={call.id} call={call} onOpen={() => onOpenCall(String(call.id))} />
                ))}
              </div>
            )}
          </section>
        </div>

        <div className="detail-rail">
          <MandatePanel
            mandate={mandate}
            busy={busy}
            open={mandateFormOpen}
            carrierCount={carrierCount}
            onOpenChange={setMandateFormOpen}
            onSubmit={grantMandate}
          />

          {approvals.length > 0 && (
            <section className="surface assignment-card">
              <p className="eyebrow">Waiting on a person</p>
              <h2>{approvals.length} to decide</h2>
              {approvals.map((approval) => (
                <ApprovalCard
                  key={approval.id}
                  approval={approval}
                  busy={busy}
                  onDecide={(status) =>
                    act(`Approval ${status}.`, () =>
                      voltaApi.decideApproval(String(approval.id), {
                        status,
                        note: null,
                      }),
                    )
                  }
                />
              ))}
            </section>
          )}

          <CommitmentCard commitment={commitment} />
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ mandate */

/**
 * Where authority enters the system.
 *
 * Nothing else in Volta can write a price ceiling, and no carrier on a phone can reach this
 * form. That is the whole architecture in one panel: the ceiling and the window are typed by
 * a named person here, and every later refusal on a recorded line traces back to this row.
 *
 * The window is typed, not assumed. It used to be hardcoded to *now until two days from now*,
 * which quietly authorized a pickup window nobody chose -- the operator saying "quote me for
 * Thursday" had no way to say Thursday. A mandate carrying a window its grantor did not pick
 * is not a mandate.
 */
/** Tomorrow morning to the evening after. A starting point to edit, never a default to accept. */
function defaultWindow(mandate: MandateView): { from: string; until: string } {
  const day = 24 * 3600 * 1000
  const morning = new Date(Date.now() + day)
  morning.setHours(8, 0, 0, 0)
  const evening = new Date(Date.now() + 2 * day)
  evening.setHours(18, 0, 0, 0)
  return {
    from: toLocalInput(
      mandate.pickup_not_before ? new Date(mandate.pickup_not_before) : morning,
    ),
    until: toLocalInput(mandate.pickup_not_after ? new Date(mandate.pickup_not_after) : evening),
  }
}

/**
 * The fields themselves. Mounted only while the form is open, which is what lets every
 * `useState` initialize straight from the mandate: opening the form *is* the event that
 * seeds it, so there is no effect here synchronizing one piece of React state with another.
 */
function MandateForm({
  mandate,
  busy,
  firstGrant,
  carrierCount,
  onCancel,
  onSubmit,
}: {
  mandate: MandateView
  busy: boolean
  firstGrant: boolean
  carrierCount: number | null
  onCancel: () => void
  onSubmit: (body: SetMandateRequest, dial: boolean) => void
}) {
  const seed = defaultWindow(mandate)
  const [cap, setCap] = useState(() => (mandate.cap ? String(mandate.cap.cents / 100) : '9000'))
  const [currency, setCurrency] = useState(() => mandate.cap?.currency ?? 'USD')
  const [target, setTarget] = useState(() =>
    mandate.target ? String(mandate.target.cents / 100) : '',
  )
  const [from, setFrom] = useState(seed.from)
  const [until, setUntil] = useState(seed.until)
  const [problem, setProblem] = useState<string | null>(null)

  const submit = () => {
    const capCents = Math.round(Number(cap) * 100)
    const targetCents = target.trim() ? Math.round(Number(target) * 100) : null
    const startsAt = new Date(from)
    const endsAt = new Date(until)

    // Checked here so the operator is told which field is wrong instead of being handed a 422.
    // The server validates the same things again; this is a courtesy, never the guard.
    if (!Number.isFinite(capCents) || capCents <= 0) {
      return setProblem('The ceiling has to be an amount above zero.')
    }
    if (targetCents !== null && (!Number.isFinite(targetCents) || targetCents <= 0)) {
      return setProblem('A target has to be an amount above zero, or be left empty.')
    }
    if (targetCents !== null && targetCents > capCents) {
      return setProblem('The target cannot sit above the ceiling it negotiates under.')
    }
    if (Number.isNaN(startsAt.getTime()) || Number.isNaN(endsAt.getTime())) {
      return setProblem('Both ends of the pickup window need a date and a time.')
    }
    if (endsAt <= startsAt) {
      return setProblem('The window has to end after it starts.')
    }

    setProblem(null)
    onSubmit(
      {
        cap_amount_cents: capCents,
        cap_currency: currency.toUpperCase(),
        target_amount_cents: targetCents,
        pickup_not_before: startsAt.toISOString(),
        pickup_not_after: endsAt.toISOString(),
        delivery_deadline: null,
        commitment_mode: 'human_escalation',
        expected_version: mandate.version,
      },
      firstGrant,
    )
  }

  const confirmLabel = !firstGrant
    ? 'Raise the ceiling'
    : carrierCount === null
      ? 'Authorize and open the market'
      : `Authorize and dial ${carrierCount} carriers`

  return (
    <>
      <div className="mandate-form">
        <div className="mandate-field">
          <label className="eyebrow" htmlFor="cap">
            Ceiling &mdash; never said out loud
          </label>
          <div className="mandate-row">
            <input
              id="cap"
              className="field"
              value={cap}
              inputMode="decimal"
              onChange={(e) => setCap(e.target.value)}
            />
            <input
              id="currency"
              className="field"
              aria-label="Currency"
              value={currency}
              maxLength={3}
              onChange={(e) => setCurrency(e.target.value)}
            />
          </div>
        </div>

        <div className="mandate-field">
          <label className="eyebrow" htmlFor="target">
            Target &mdash; what to aim for (optional)
          </label>
          <input
            id="target"
            className="field"
            value={target}
            inputMode="decimal"
            placeholder="leave empty to only cap"
            onChange={(e) => setTarget(e.target.value)}
          />
        </div>

        <div className="mandate-field">
          <label className="eyebrow" htmlFor="pickup-from">
            Pickup no earlier than
          </label>
          <input
            id="pickup-from"
            className="field"
            type="datetime-local"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
          />
        </div>

        <div className="mandate-field">
          <label className="eyebrow" htmlFor="pickup-until">
            Pickup no later than
          </label>
          <input
            id="pickup-until"
            className="field"
            type="datetime-local"
            value={until}
            onChange={(e) => setUntil(e.target.value)}
          />
        </div>
      </div>

      {problem && <p className="mandate-problem">{problem}</p>}

      <p className="action-note">
        {firstGrant
          ? 'Granting this opens the market in the same gesture: the carriers are dialled ' +
            'straight away and told the window, never the ceiling.'
          : 'Raising the ceiling records a new version and dials nobody. Open the market ' +
            'yourself when you want the new figure taken back out.'}
      </p>

      <div className="dialog-actions">
        <button className="secondary-button" onClick={onCancel}>
          Cancel
        </button>
        <button className="primary-button" disabled={busy || !cap.trim()} onClick={submit}>
          {confirmLabel}
        </button>
      </div>
    </>
  )
}

/**
 * Where authority enters the system.
 *
 * Nothing else in Volta can write a price ceiling, and no carrier on a phone can reach this
 * form. That is the whole architecture in one panel: the ceiling and the window are typed by
 * a named person here, and every later refusal on a recorded line traces back to this row.
 *
 * The window is typed, not assumed. It used to be hardcoded to *now until two days from now*,
 * which quietly authorized a pickup window nobody chose -- the operator saying "quote me for
 * Thursday" had no way to say Thursday. A mandate carrying a window its grantor did not pick
 * is not a mandate.
 */
function MandatePanel({
  mandate,
  busy,
  open,
  carrierCount,
  onOpenChange,
  onSubmit,
}: {
  mandate: MandateView
  busy: boolean
  open: boolean
  /** `null` until /api/session answers -- the button must not guess a number it will dial. */
  carrierCount: number | null
  onOpenChange: (open: boolean) => void
  onSubmit: (body: SetMandateRequest, dial: boolean) => void
}) {
  // A first grant opens the market in the same gesture; a raise does not. Re-dialling carriers
  // who are mid-call because somebody moved the ceiling is a worse default than one more click.
  const firstGrant = !mandate.is_granted

  return (
    <section className="action-panel">
      <p className="eyebrow">Authority</p>
      <h2>{mandate.is_granted ? 'Mandate granted' : 'Nothing is authorized'}</h2>

      {mandate.is_granted ? (
        <>
          <div className="mandate-card">
            <span className="eyebrow">Ceiling · version {mandate.version}</span>
            <strong>{formatMoney(mandate.cap)}</strong>
            <span>
              Pickup {formatDate(mandate.pickup_not_before)} &mdash;{' '}
              {formatDate(mandate.pickup_not_after)}
            </span>
            <span>
              Granted by {mandate.set_by} · {formatDate(mandate.set_at)}
            </span>
          </div>
          <p className="action-note">
            The agent never says this figure out loud. Policy compares against it on every
            proposal and copies it into the decision by value, so raising it later cannot
            rewrite an earlier refusal.
          </p>
        </>
      ) : (
        <p className="action-note">
          No mandate is not &ldquo;no limit&rdquo;. It is a permission nobody granted, and until
          a person sets a ceiling and a pickup window the agent cannot open a market or agree to
          anything.
        </p>
      )}

      {open ? (
        <MandateForm
          mandate={mandate}
          busy={busy}
          firstGrant={firstGrant}
          carrierCount={carrierCount}
          onCancel={() => onOpenChange(false)}
          onSubmit={onSubmit}
        />
      ) : (
        <button className="primary-button" disabled={busy} onClick={() => onOpenChange(true)}>
          {mandate.is_granted ? 'Raise the ceiling' : 'Grant a mandate'}
        </button>
      )}
    </section>
  )
}

/* ------------------------------------------------------------------- market */

function MarketPanel({
  quotes,
  mandateGranted,
  marketOpen,
  busy,
  onStart,
}: {
  quotes: QuoteRow[]
  mandateGranted: boolean
  /** Carriers have been dialled for this order. Nothing to do but wait for them to answer. */
  marketOpen: boolean
  busy: boolean
  onStart: () => void
}) {
  // Superseded rows are shown, never hidden: they said 8,500 and then they said 9,200, and
  // both were said. A market that displays only the current number has deleted the evidence.
  const live = quotes.filter((q) => q.status !== 'superseded')
  const superseded = quotes.filter((q) => q.status === 'superseded')

  return (
    <section className="surface">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Market</p>
          <h2>Quotes</h2>
        </div>
        <span className="count">{quotes.length}</span>
      </div>

      {quotes.length === 0 && (
        <div className="empty-market">
          <strong>No quotes yet.</strong>
          <p>
            {!mandateGranted
              ? 'A mandate has to exist before anyone is called.'
              : marketOpen
                ? 'The market is open. Quotes appear here as carriers answer.'
                : 'Open the market to dial carriers in parallel.'}
          </p>
          {!marketOpen && (
            <div className="offer-action">
              <button
                className="secondary-button"
                disabled={!mandateGranted || busy}
                onClick={onStart}
              >
                Start quoting
              </button>
            </div>
          )}
        </div>
      )}

      {live.length > 0 && (
        <div className="offer-list">
          {live.map((quote) => (
            <QuoteCard key={quote.id} quote={quote} />
          ))}
        </div>
      )}

      {superseded.length > 0 && (
        <>
          <p className="eyebrow" style={{ marginTop: 24 }}>
            Superseded — kept on purpose
          </p>
          <div className="offer-list">
            {superseded.map((quote) => (
              <QuoteCard key={quote.id} quote={quote} />
            ))}
          </div>
        </>
      )}
    </section>
  )
}

function QuoteCard({ quote }: { quote: QuoteRow }) {
  return (
    <article className={quote.status === 'accepted' ? 'offer-card offer-recommended' : 'offer-card'}>
      <div className="offer-heading">
        <div>
          <p className="eyebrow">Quote</p>
          <h3>{formatMoney(quote.amount)}</h3>
        </div>
        {quote.status === 'accepted' && <span className="recommendation">Awarded</span>}
      </div>

      <div className="offer-metrics">
        <div>
          <span>Pickup</span>
          <strong>{formatDate(quote.pickup_at)}</strong>
        </div>
        <div>
          <span>Equipment</span>
          <strong>{quote.equipment}</strong>
        </div>
        <div>
          <span>Total is final</span>
          <strong>{quote.cost_is_final ? 'Yes' : 'No'}</strong>
        </div>
      </div>

      {!quote.cost_is_final && (
        <p>
          The carrier did not confirm this is everything. A total that is not final cannot be
          authorized — &ldquo;plus tolls&rdquo; is an open number.
        </p>
      )}

      <div className="offer-footer">
        <span className={statusClass(quote.status)}>{humanise(quote.status)}</span>
        <span>said at {formatOffset(quote.anchor_ms)} · valid to {formatDate(quote.valid_until)}</span>
      </div>
    </article>
  )
}

/* --------------------------------------------------------------- approvals */

function ApprovalCard({
  approval,
  busy,
  onDecide,
}: {
  approval: Approval
  busy: boolean
  onDecide: (status: 'approved' | 'rejected') => void
}) {
  const comparison = approval.kind === 'award_approval' ? approvalComparison(approval) : null
  return (
    <div className="snapshot">
      <strong>{humanise(approval.kind)}</strong>
      <span>{humanise(approval.reason)}</span>
      <small>Raised {formatDate(approval.raised_at)}</small>
      {comparison && (
        <div className="approval-comparison">
          <strong>Policy-ranked carrier comparison</strong>
          {comparison.entries.map((entry) => (
            <div
              className={entry.is_winner ? 'approval-option approval-option-winner' : 'approval-option'}
              key={entry.quote_id}
            >
              <span>{entry.is_winner ? 'Recommended · ' : ''}{entry.carrier_name}</span>
              <b>{formatMoney(entry.amount)}</b>
              <small>{humanise(entry.outcome)} · {humanise(entry.reason_code)} · pickup {formatDate(entry.pickup_at)}</small>
            </div>
          ))}
          <small>Mandate version {comparison.mandate_version} · recommendation is revalidated when approved</small>
        </div>
      )}
      <div className="dialog-actions">
        <button
          className="secondary-button"
          disabled={busy}
          onClick={() => onDecide('rejected')}
        >
          Reject
        </button>
        <button
          className="primary-button"
          disabled={busy}
          onClick={() => onDecide('approved')}
        >
          Approve
        </button>
      </div>
    </div>
  )
}

function ApprovalsPage({ onOpen }: { onOpen: (orderId: string) => void }) {
  const [approvals, setApprovals] = useState<Approval[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    voltaApi
      .listApprovals()
      .then((value) => {
        setApprovals(value)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(load, [load])

  useEffect(() => {
    const poll = window.setInterval(load, 5000)
    return () => window.clearInterval(poll)
  }, [load])

  if (error) return <ErrorState message={error} onRetry={load} />
  if (!approvals) return <Loading what="the inbox" />

  return (
    <div className="page">
      <div className="page-heading">
        <p className="eyebrow">Approvals</p>
        <h1>
          What a person <em>must</em> look at
        </h1>
        <p>
          Award decisions, mid-call escalations and incidents are the same request from here:
          somebody has to decide. That is why they are one queue and not three.
        </p>
      </div>

      {approvals.length === 0 ? (
        <div className="empty-market">
          <strong>Nothing is waiting.</strong>
          <p>The agent has not needed a person since you last looked.</p>
        </div>
      ) : (
        <div className="operation-list">
          {approvals.map((approval) => (
            <button
              className="operation-row"
              key={approval.id}
              onClick={() => approval.order_id && onOpen(approval.order_id)}
            >
              <div className="operation-route">
                <span className="reference">{humanise(approval.kind)}</span>
                <strong>{humanise(approval.reason)}</strong>
                <span>Raised {formatDate(approval.raised_at)}</span>
              </div>
              <div className="operation-stage">
                <span className={statusClass(approval.status)}>{humanise(approval.status)}</span>
              </div>
              <div className="operation-clock">
                <small>open</small>
              </div>
              <span className="row-arrow">→</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function approvalComparison(approval: Approval): Comparison | null {
  const value = approval.context
  if (!Array.isArray(value.entries) || typeof value.order_id !== 'string') return null
  return value as unknown as Comparison
}

/* -------------------------------------------------------------- commitment */

function CommitmentCard({ commitment }: { commitment: Commitment | null }) {
  if (!commitment) {
    return (
      <section className="surface assignment-card">
        <p className="eyebrow">Commitment</p>
        <h2>Nothing is booked</h2>
        <p className="assignment-pending">
          No commitment exists for this operation. Until one does, nobody has been told they have
          the load.
        </p>
      </section>
    )
  }

  const booked = commitment.state === 'committed' || commitment.state === 'executed'

  return (
    <section className="surface assignment-card">
      <p className="eyebrow">Commitment</p>
      <h2>{booked ? 'Booked' : 'Not booked'}</h2>
      <span className={statusClass(commitment.state)}>{humanise(commitment.state)}</span>
      <p className="commitment-copy">
        {booked
          ? 'The written recap was delivered. That delivery is what promoted this commitment.'
          : 'A verbal agreement is on record, but the recap has not been confirmed delivered. Until it is, this is not a booking.'}
      </p>
      <dl>
        <div>
          <dt>Agreed at</dt>
          <dd>{formatOffset(commitment.evidence_anchor_ms)}</dd>
        </div>
        <div>
          <dt>Evidence call</dt>
          <dd>{commitment.evidence_call_id.slice(0, 8)}</dd>
        </div>
      </dl>
      <span className="recap-status">anchor required · no offset, no commitment</span>
    </section>
  )
}

/* ------------------------------------------------------------------- calls */

function CallRow({ call, onOpen }: { call: CallRecord; onOpen: () => void }) {
  return (
    <button className="call-row" onClick={onOpen}>
      <div>
        <strong>
          {humanise(String(call.phase))} · {call.direction}
        </strong>
        <small>{call.to_number ?? call.from_number ?? 'unknown number'}</small>
      </div>
      <div className="call-meta">
        <span className={statusClass(call.status)}>{humanise(call.status)}</span>
        <span>{formatDate(call.started_at)}</span>
      </div>
    </button>
  )
}



/* -------------------------------------------------------------- the model's report */

/** A note the model made, with the moment it heard it. */
function AnchoredList({ items }: { items: AnchoredNote[] }) {
  return (
    <ul className="anchored-list">
      {items.map((item, index) => (
        <li key={index}>
          <span className="anchored-time">{formatOffset(item.offset_ms ?? null)}</span>
          <span>{item.text ?? '—'}</span>
        </li>
      ))}
    </ul>
  )
}

function ReportBlock({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="report-block">
      <p className="eyebrow">{title}</p>
      {children}
    </div>
  )
}

/**
 * Everything the model produced about the call, and nothing more.
 *
 * The panel is visually separated from the Decision Trace on purpose. The trace is the
 * ledger — rows we wrote when the thing happened. This is one model's reading of a
 * recording, generated afterwards with no latency budget, and it is wrong sometimes. It is
 * useful for the same reason a colleague's notes are useful, and it binds nothing.
 *
 * Agreement candidates get the most room and the loudest caveat, because they are the part
 * a reader is most likely to mistake for a booking. A commitment exists in `commitments`,
 * with an anchor the server measured, or it does not exist.
 */
function CallReportPanel({ report }: { report: CallReport }) {
  const has = (list: unknown[] | undefined) => Array.isArray(list) && list.length > 0

  return (
    <div className="report">
      <div className="report-heading">
        <p className="eyebrow">What a model understood</p>
        <div className="report-badges">
          <span className={statusClass(report.subject)}>{humanise(report.subject)}</span>
          <span className={statusClass(report.severity)}>{report.severity}</span>
        </div>
      </div>

      <p className="report-summary">{report.summary}</p>

      {has(report.agreement_candidates) && (
        <div className="report-candidates">
          <p className="eyebrow">Agreement candidates — proposals, not bookings</p>
          <p className="report-caveat">
            Terms the model believes it heard. Nothing here is agreed: policy decides whether
            anything binds, and a commitment exists only with an offset the server measured.
          </p>
          {report.agreement_candidates.map((candidate, index) => (
            <article className="candidate" key={index}>
              <div className="candidate-head">
                <span className="anchored-time">{formatOffset(candidate.offset_ms ?? null)}</span>
                {candidate.counterparty && <strong>{candidate.counterparty}</strong>}
              </div>
              <ul className="brief-list">
                {(candidate.terms ?? []).map((term) => (
                  <li key={term}>{term}</li>
                ))}
              </ul>
            </article>
          ))}
        </div>
      )}

      <div className="report-grid">
        {has(report.quoted_prices) && (
          <ReportBlock title="Prices heard">
            <ul className="anchored-list">
              {report.quoted_prices.map((price, index) => (
                <li key={index}>
                  <span className="anchored-time">{formatOffset(price.offset_ms ?? null)}</span>
                  <span>
                    {price.amount ?? '—'} {price.currency ?? ''}
                  </span>
                </li>
              ))}
            </ul>
          </ReportBlock>
        )}

        {has(report.actions) && (
          <ReportBlock title="What Volta did">
            <AnchoredList items={report.actions} />
          </ReportBlock>
        )}

        {has(report.mentions) && (
          <ReportBlock title="Mentioned">
            <AnchoredList items={report.mentions} />
          </ReportBlock>
        )}

        {has(report.conditions) && (
          <ReportBlock title="Conditions">
            <ul className="brief-list">
              {report.conditions.map((condition) => (
                <li key={condition}>{condition}</li>
              ))}
            </ul>
          </ReportBlock>
        )}

        {has(report.objections) && (
          <ReportBlock title="Objections">
            <ul className="brief-list">
              {report.objections.map((objection) => (
                <li key={objection}>{objection}</li>
              ))}
            </ul>
          </ReportBlock>
        )}
      </div>

      {/* Which model, and when. Without it a reader cannot tell a fresh reading from one
          produced by a model we have since replaced. */}
      <p className="report-provenance">
        {report.model ?? 'unknown model'} · generated {formatDate(report.generated_at)} ·
        model output is never authority
      </p>
    </div>
  )
}

/* --------------------------------------------------------------- decision trace */

const TRACE_FILTERS: { key: 'all' | TraceCategory; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'conversation', label: 'Conversation' },
  { key: 'decision', label: 'Decision' },
  { key: 'tool', label: 'Tool' },
  { key: 'action', label: 'Action' },
]

// Quote and Policy stay categories without earning a filter button. They are the rows a
// reader wants in place, in order, next to what caused them -- pulling them out of the
// sequence would break the story the trace exists to tell.

const RESULT_TONE: Record<TraceResult, string> = {
  continue: 'neutral',
  proposed: 'neutral',
  allowed: 'teal',
  authorized: 'teal',
  clarify: 'amber',
  escalate: 'amber',
  in_progress: 'amber',
  denied: 'red',
  failed: 'red',
  completed: 'green',
  not_executed: 'dim',
  unknown: 'dim',
}

const CATEGORY_MARK: Record<TraceCategory, string> = {
  conversation: '💬',
  quote: '🏷',
  policy: '🛡',
  decision: '⚖',
  tool: '🔧',
  action: '▶',
}

function DecisionTrace({ callId }: { callId: string }) {
  const [rows, setRows] = useState<TraceRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<'all' | TraceCategory>('all')
  const [query, setQuery] = useState('')

  const load = useCallback(() => {
    voltaApi
      .getTrace(callId)
      .then((value) => {
        setRows(value)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [callId])

  useEffect(load, [load])

  if (error) return <p className="empty-copy">{error}</p>
  if (!rows) return <Loading what="the trace" />

  const needle = query.trim().toLowerCase()
  const shown = rows.filter((row) => {
    if (filter !== 'all' && row.category !== filter) return false
    if (!needle) return true
    return `${row.counterparty} ${row.volta} ${row.reason_code ?? ''}`.toLowerCase().includes(needle)
  })

  return (
    <section className="trace">
      <div className="trace-controls">
        <div className="trace-filters">
          {TRACE_FILTERS.map((option) => (
            <button
              key={option.key}
              className={filter === option.key ? 'trace-filter trace-filter-active' : 'trace-filter'}
              onClick={() => setFilter(option.key)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <input
          className="field trace-search"
          value={query}
          placeholder="Search trace"
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {shown.length === 0 ? (
        <p className="empty-copy">Nothing recorded under that filter.</p>
      ) : (
        <table className="trace-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Category</th>
              <th>Counterparty</th>
              <th>Volta</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((row, index) => (
              <tr key={index}>
                {/* The row shows call-relative time; the exact clock time is on hover,
                    because "00:34" is what you compare against a recording. */}
                <td className="trace-time" title={new Date(row.at).toISOString()}>
                  {formatOffset(row.offset_ms)}
                </td>
                <td className="trace-category">
                  <span aria-hidden="true">{CATEGORY_MARK[row.category]}</span>
                  {row.category}
                </td>
                <td>{row.counterparty}</td>
                <td>
                  {row.volta}
                  {row.reason_code && (
                    <span className="trace-reason">Policy: {row.reason_code}</span>
                  )}
                  {row.provenance && <span className="trace-provenance">{row.provenance}</span>}
                </td>
                <td>
                  <span className={`trace-result trace-${RESULT_TONE[row.result]}`}>
                    {humanise(row.result)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <p className="trace-footer">Append-only evidence · Model output is never authority</p>
    </section>
  )
}

function CallEvidencePage({ callId, onBack }: { callId: string; onBack: () => void }) {
  const [detail, setDetail] = useState<CallDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    voltaApi
      .getCall(callId)
      .then((value) => {
        setDetail(value)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [callId])

  useEffect(load, [load])

  if (error) return <ErrorState message={error} onRetry={load} />
  if (!detail) return <Loading what="the call" />

  const { call, report, carrier } = detail
  // Defence in depth. The ingestion fix in vapi/webhook.py keeps the composed prompt out of
  // the stored transcript; this makes sure a row written before that fix -- or by anything
  // else that learns to write here -- cannot put it back on screen. The prompt states the
  // mandate ceiling under a heading telling the agent never to say it out loud.
  const conversation = call.transcript.filter(
    (turn) => turn.speaker === 'agent' || turn.speaker === 'caller',
  )

  return (
    <div className="page">
      <button className="back-button" onClick={onBack}>
        ← Back to the operation
      </button>

      <div className="evidence-detail surface">
        <p className="eyebrow reference">
          {humanise(String(call.phase))} · {call.direction}
        </p>
        <h2>{carrier?.name ?? call.to_number ?? call.from_number ?? 'Unknown counterparty'}</h2>
        <p className="commitment-copy">
          {formatDate(call.started_at)} · identity level {call.identity_level} ·{' '}
          {call.identity_verified ? 'verified' : 'unverified'}
        </p>

        {call.recording_url && (
          <div className="evidence-pointer">
            <p>Recording</p>
            <p>
              <a href={call.recording_url} target="_blank" rel="noreferrer">
                {call.recording_url}
              </a>
            </p>
          </div>
        )}

        {report && (
          <section>
            <CallReportPanel report={report} />
          </section>
        )}

        <section>
          <p className="eyebrow">Decision trace</p>
          <DecisionTrace callId={callId} />
        </section>

        <section>
          <p className="eyebrow">Transcript</p>
          {conversation.length === 0 ? (
            <p className="empty-copy">No transcript was stored for this call.</p>
          ) : (
            <div className="transcript">
              {conversation.map((turn, index) => (
                <div className="transcript-line" key={index}>
                  <time>{formatOffset(turn.offset_ms)}</time>
                  <div>
                    <strong>{turn.speaker}</strong>
                    <p>{turn.text}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}


/* ---------------------------------------------------------------------- business */

/** Sections, so the form reads as three things rather than twenty-two fields. */
const PROFILE_SECTIONS: {
  title: string
  blurb: string
  wide?: boolean
  fields: [keyof BusinessProfile, string][]
}[] = [
    {
      title: 'The business',
      blurb: 'Rendered into every prompt. The agent says this out loud.',
      fields: [
        ['display_name', 'Trading name'],
        ['legal_name', 'Legal name'],
        ['business_type', 'Business type'],
        ['city', 'City'],
        ['country', 'Country'],
        ['currency', 'Currency'],
        ['timezone', 'Timezone'],
        ['business_hours', 'Business hours'],
      ],
    },
    {
      title: 'On the phone',
      blurb: 'Who the agent says it is, and which language it opens in.',
      fields: [
        ['agent_name', 'Agent name'],
        ['agent_role', 'Agent role'],
        ['primary_language', 'Primary language'],
        ['fallback_language', 'Fallback language'],
      ],
    },
    {
      title: 'Warehouse',
      wide: true,
      blurb:
        'Where the cargo is delivered. The address is spoken to carriers; the contact and ' +
        'the hours are what a driver needs on arrival.',
      fields: [
        ['warehouse_name', 'Site name'],
        ['warehouse_address', 'Street address'],
        ['warehouse_city', 'City'],
        ['warehouse_state', 'State'],
        ['warehouse_postal_code', 'Postal code'],
        ['warehouse_country', 'Country'],
        ['warehouse_contact_name', 'Contact'],
        ['warehouse_phone', 'Phone'],
        ['warehouse_hours', 'Receiving hours'],
        ['warehouse_notes', 'Notes for drivers'],
      ],
    },
  ]

function ProfilePage() {
  const [profile, setProfile] = useState<BusinessProfile | null>(null)
  const [draft, setDraft] = useState<Partial<BusinessProfile>>({})
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    voltaApi
      .getProfile()
      .then((value) => {
        setProfile(value)
        setDraft({})
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(load, [load])

  if (error) return <ErrorState message={error} onRetry={load} />
  if (!profile) return <Loading what="the business profile" />

  const dirty = Object.keys(draft).length > 0

  const save = async () => {
    setBusy(true)
    setNotice(null)
    try {
      const body = { ...draft } as BusinessProfileUpdate
      const saved = await voltaApi.updateProfile(body)
      setProfile(saved)
      setDraft({})
      setNotice('Saved.')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page">
      <div className="page-heading">
        <p className="eyebrow">Business</p>
        <h1>
          What Volta says <em>it is</em>
        </h1>
        <p>
          Every value here is read from the database and rendered into the prompt. Change one
          and the next call says something different, which is why a change carries a name.
        </p>
      </div>

      {notice && (
        <div className="command-notice">
          <span>✓</span>
          <p>{notice}</p>
          <button onClick={() => setNotice(null)}>×</button>
        </div>
      )}

      <div className="configuration-grid">
        {PROFILE_SECTIONS.map((section) => (
          <section
            className={
              section.wide
                ? 'surface configuration-section profile-wide'
                : 'surface configuration-section'
            }
            key={section.title}
          >
            <p className="eyebrow">{section.title}</p>
            <p className="configuration-note">{section.blurb}</p>
            <div className="profile-fields">
              {section.fields.map(([key, label]) => (
                <label className="profile-field" key={key}>
                  <span className="eyebrow">{label}</span>
                  <input
                    className="field"
                    value={String(draft[key] ?? profile[key] ?? '')}
                    onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
                  />
                </label>
              ))}
            </div>
          </section>
        ))}
      </div>

      <section className="surface configuration-section profile-save">
        <p className="eyebrow">Saving</p>
        <p className="configuration-note">
          The warehouse address is spoken to carriers and the agent&rsquo;s name is how it
          introduces itself. A change to either changes what the system says on a recorded
          line, so it is recorded against the configured portal actor rather than a text box.
        </p>
        <div className="dialog-actions">
          <button className="secondary-button" disabled={!dirty || busy} onClick={load}>
            Discard
          </button>
          <button className="primary-button" disabled={!dirty || busy} onClick={save}>
            {dirty ? `Save ${Object.keys(draft).length} change(s)` : 'No changes'}
          </button>
        </div>
        {profile.updated_by && (
          <p className="configuration-note">
            Last changed by {profile.updated_by} · {formatDate(profile.updated_at)}
          </p>
        )}
      </section>
    </div>
  )
}

/* ---------------------------------------------------------------- carriers */

function CarriersPage() {
  const [carriers, setCarriers] = useState<Carrier[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    voltaApi
      .listCarriers()
      .then((value) => {
        setCarriers(value)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(load, [load])

  if (error) return <ErrorState message={error} onRetry={load} />
  if (!carriers) return <Loading what="carriers" />

  return (
    <div className="page">
      <div className="page-heading">
        <p className="eyebrow">Carriers</p>
        <h1>
          Who Volta is <em>allowed</em> to call
        </h1>
        <p>
          Being on file is a decision made here, never on the phone. A caller who is not on file
          can say all the right things and still gets nothing.
        </p>
      </div>

      <div className="operation-list">
        {carriers.map((carrier) => (
          <div className="operation-row" key={carrier.id}>
            <div className="operation-route">
              <strong>{carrier.name}</strong>
              <span>{carrier.phone}</span>
            </div>
            <div className="operation-stage">
              <span className={carrier.is_on_file ? 'status-badge status-ready' : 'status-badge status-deny'}>
                {carrier.is_on_file ? 'on file' : 'not on file'}
              </span>
              <span>{carrier.persona ?? ''}</span>
            </div>
            <div className="operation-clock">
              <small>{carrier.is_active ? 'active' : 'inactive'}</small>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
