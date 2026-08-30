-- Volta: the whole schema, in one migration.
--
-- Ten tables. The rule is that a table exists only if a named writer fills it; anything
-- without one collapsed into a column or into jsonb. Three constraints in here do work that
-- would otherwise be application logic, and they are the reason the schema is worth reading:
--
--   1. quotes: unique (order_id) where status = 'accepted'
--      Exactly one award per order, enforced by the database. Two carriers confirming at the
--      same instant cannot both find an empty slot, which a read-then-write check would let
--      them do.
--   2. commitments.evidence_anchor_ms not null
--      A commitment cannot exist without the moment of audio at which it was agreed. This
--      one NOT NULL replaces a trigger: nothing has to police an absence that cannot occur.
--   3. events.idempotency_key unique, with `on conflict do nothing`
--      Vapi redelivers webhooks. This makes a second delivery atomic and free.
--
-- Money is bigint cents plus an explicit ISO 4217 code, never a float and never a bare
-- amount. Rounding has to be somebody's decision, not a property of binary fractions.
--
-- Two tables are append-only by grant rather than by convention: decisions and events.
-- The backend holds the service role, so "the backend could rewrite its own refusals" is a
-- real risk, and revoking the privilege is a real answer rather than a promise.
--
-- The others are deliberately still mutable, and it is worth being precise about why:
-- quotes and commitments get their immutability from superseded_by (a change is a new row
-- that points at the old one, so nothing is lost by an UPDATE that only sets a status), and
-- calls.transcript is written once when the call ends. Revoking UPDATE there would break
-- the status transitions those rows legitimately need.

-- ============================================================================ the world

create table public.carriers (
    id uuid primary key default gen_random_uuid(),
    name text not null check (length(trim(name)) > 0),
    -- E.164. The unique index is not housekeeping: it is how an inbound call is correlated
    -- to a carrier before the agent says anything beyond hello.
    phone text not null unique,
    contact_name text,
    email text,
    whatsapp text,
    -- False means the agent declines to quote. Volta onboards nobody by phone, and refusing
    -- is the correct behaviour rather than a limitation.
    is_on_file boolean not null default true,
    is_active boolean not null default true,
    -- Seed-only colour, never spoken: "cheap and slow", "never answers". Without carriers
    -- who behave differently, a comparison between them demonstrates nothing.
    persona text,
    created_at timestamptz not null default now()
);

create table public.orders (
    id uuid primary key default gen_random_uuid(),
    -- The folio. Also identity proof level 1 on an inbound call: only a real counterparty
    -- knows it, which is why the agent asks for it and never reads it out.
    reference text not null unique,
    status text not null default 'received' check (status in (
        'received', 'quoting', 'awaiting_approval', 'awarding', 'booked',
        'in_transit', 'at_risk', 'delivered', 'closed', 'cancelled'
    )),

    origin text,
    destination text,
    cargo text,
    equipment text,
    weight text,
    container_number text,

    -- Demurrage. Nobody decides when it starts; discharge starts it, and last_free_day is
    -- the countdown that makes everything downstream urgent.
    discharged_at timestamptz,
    free_days integer check (free_days is null or free_days >= 0),
    last_free_day date,

    -- The OUTBOUND 2 trigger.
    delivery_deadline timestamptz,

    -- --- the mandate: columns, not a table, because there is exactly one per order ---
    cap_amount bigint check (cap_amount is null or cap_amount > 0),
    cap_currency char(3),
    target_amount bigint check (target_amount is null or target_amount > 0),
    pickup_not_before timestamptz,
    pickup_not_after timestamptz,
    commitment_mode text not null default 'human_escalation'
        check (commitment_mode in ('autonomous', 'human_escalation')),
    -- 0 means no mandate has been granted. Bumped on every mandate write; decisions copy the
    -- ceiling by value, which is what makes versioning unnecessary here.
    mandate_version integer not null default 0 check (mandate_version >= 0),
    mandate_set_by text,
    mandate_set_at timestamptz,

    assigned_carrier_id uuid references public.carriers (id) on delete restrict,
    awarded_quote_id uuid,

    -- What a legitimate inbound caller can tell us. Checked against, never read out: a
    -- caller who is told the plate can repeat the plate, which verifies nothing.
    expected_driver text,
    expected_plate text,

    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    -- An amount without a currency is not a ceiling, it is an accident waiting for a call.
    constraint cap_has_currency check (
        (cap_amount is null and cap_currency is null)
        or (cap_amount is not null and cap_currency is not null)
    ),
    constraint window_is_ordered check (
        pickup_not_before is null
        or pickup_not_after is null
        or pickup_not_before <= pickup_not_after
    ),
    -- A mandate is a ceiling, a window and an approver, together or not at all. A partial
    -- one authorizes nothing but looks like it might.
    constraint mandate_is_complete check (
        mandate_version = 0
        or (cap_amount is not null and pickup_not_before is not null
            and pickup_not_after is not null and mandate_set_by is not null)
    )
);

create index orders_status_deadline_idx
    on public.orders (status, delivery_deadline)
    where delivery_deadline is not null;

-- ===================================================================== during a call

create table public.calls (
    id uuid primary key default gen_random_uuid(),
    -- Unique so a redelivered webhook cannot create a second call.
    vapi_call_id text not null unique,
    direction text not null check (direction in ('inbound', 'outbound')),
    phase text not null check (phase in (
        'rfq', 'award', 'renegotiation', 'inbound', 'status_check'
    )),
    status text not null default 'queued'
        check (status in ('queued', 'ringing', 'active', 'ended', 'failed')),
    -- Null until an inbound call has been correlated to an order.
    order_id uuid references public.orders (id) on delete restrict,
    -- Null when the number is not on file, which is already information about the caller.
    carrier_id uuid references public.carriers (id) on delete restrict,
    from_number text,
    to_number text,
    started_at timestamptz,
    ended_at timestamptz,
    ended_reason text,
    recording_url text,
    -- Vapi returns the transcript once, at end of call, as one array. Nothing queries it
    -- line by line, so a whole utterances table would be a join nobody makes.
    transcript jsonb not null default '[]'::jsonb
        check (jsonb_typeof(transcript) = 'array'),
    -- The exact CallContext the prompt was composed from. Without it a call cannot be
    -- replayed, and a call that cannot be replayed is not evidence.
    context jsonb not null default '{}'::jsonb,
    identity_verified boolean not null default false,
    identity_level smallint not null default 0 check (identity_level between 0 and 3),
    cost_cents integer,
    created_at timestamptz not null default now()
);

create index calls_order_idx on public.calls (order_id, started_at desc);
create index calls_from_number_idx on public.calls (from_number);

create table public.quotes (
    id uuid primary key default gen_random_uuid(),
    order_id uuid not null references public.orders (id) on delete restrict,
    carrier_id uuid not null references public.carriers (id) on delete restrict,
    call_id uuid not null references public.calls (id) on delete restrict,
    -- The moment of the recording at which this was said. Measured by us when the tool
    -- fired, not read back out of a vendor transcript field.
    anchor_ms integer not null check (anchor_ms >= 0),

    amount_cents bigint not null check (amount_cents >= 0),
    currency char(3) not null,
    components jsonb not null default '[]'::jsonb
        check (jsonb_typeof(components) = 'array'),
    -- False by default so that silence blocks. "Plus tolls" means the total is not final,
    -- and a total that is not final cannot be authorized.
    cost_is_final boolean not null default false,

    pickup_at timestamptz not null,
    pickup_window_end timestamptz,
    equipment text not null,
    valid_until timestamptz not null,
    all_in_usd_cents bigint,

    status text not null default 'proposed' check (status in (
        'proposed', 'superseded', 'withdrawn', 'selected', 'accepted', 'rejected'
    )),
    -- They said 8,500 and then they said 9,200. Both were said. Overwriting the first
    -- deletes exactly the fact a judge is going to probe.
    superseded_by uuid references public.quotes (id) on delete restrict,

    carrier_confirmed_exact_recap boolean not null default false,
    confirmed_at timestamptz,
    -- Who they *said* they were. Never trusted, always kept.
    claimed_identity text,
    identity_level smallint not null default 0 check (identity_level between 0 and 3),
    created_at timestamptz not null default now()
);

-- INVARIANT 1. Exactly one award per order. Two open bookings is the worst outcome in the
-- brief, and this is the line that prevents it.
create unique index quotes_one_award_per_order
    on public.quotes (order_id)
    where status = 'accepted';

create index quotes_order_status_idx on public.quotes (order_id, status);

alter table public.orders
    add constraint orders_awarded_quote_fk
    foreign key (awarded_quote_id) references public.quotes (id) on delete restrict;

create table public.decisions (
    id uuid primary key default gen_random_uuid(),
    order_id uuid not null references public.orders (id) on delete restrict,
    call_id uuid references public.calls (id) on delete restrict,
    quote_id uuid references public.quotes (id) on delete restrict,
    -- A copy of the input. Without it the decision cannot be reproduced, and a decision that
    -- cannot be reproduced cannot be defended to the person who asks why.
    proposal jsonb not null default '{}'::jsonb,
    outcome text not null check (outcome in ('allow', 'deny', 'escalate')),
    reason_code text not null,
    -- The ceiling copied by value. Someone raising the cap ten minutes from now must not be
    -- able to rewrite the explanation given for a refusal that already happened.
    cap_at_decision_cents bigint,
    cap_currency char(3),
    mandate_version integer not null default 0,
    decided_at timestamptz not null default now()
);

create index decisions_order_idx on public.decisions (order_id, decided_at desc);

create table public.events (
    id uuid primary key default gen_random_uuid(),
    order_id uuid references public.orders (id) on delete restrict,
    call_id uuid references public.calls (id) on delete restrict,
    type text not null,
    payload jsonb not null default '{}'::jsonb,
    -- INVARIANT 3. With `on conflict do nothing` this is atomic: a redelivered webhook has
    -- no window in which to slip a second write through.
    idempotency_key text not null unique,
    created_at timestamptz not null default now()
);

create index events_order_idx on public.events (order_id, created_at desc);

-- ====================================================================== after a call

create table public.call_reports (
    call_id uuid primary key references public.calls (id) on delete restrict,
    summary text not null,
    -- The inbound classification, and the outcome of a status-check call.
    subject text not null default 'other' check (subject in (
        'quote', 'accident', 'delay', 'request', 'delivered', 'other'
    )),
    severity text not null default 'low' check (severity in ('low', 'medium', 'high')),
    actions jsonb not null default '[]'::jsonb,
    mentions jsonb not null default '[]'::jsonb,
    quoted_prices jsonb not null default '[]'::jsonb,
    objections jsonb not null default '[]'::jsonb,
    conditions jsonb not null default '[]'::jsonb,
    -- Candidates only. The model proposes; policy decides whether one ever binds.
    agreement_candidates jsonb not null default '[]'::jsonb
        check (jsonb_typeof(agreement_candidates) = 'array'),
    model text,
    generated_at timestamptz not null default now()
);

create table public.approvals (
    id uuid primary key default gen_random_uuid(),
    order_id uuid references public.orders (id) on delete restrict,
    call_id uuid references public.calls (id) on delete restrict,
    -- One inbox for the three things that all mean "a person must look at this". From the
    -- portal's point of view an award decision, an escalation and a missed deadline are the
    -- same request, which is what makes the dashboard one screen instead of three.
    kind text not null check (kind in ('award_approval', 'escalation', 'incident')),
    reason text not null check (reason in (
        'award_selected', 'outside_mandate', 'direct_request', 'identity_unverified',
        'conflicting_information', 'policy_failure', 'deadline_breach',
        'carrier_reported_incident', 'no_eligible_candidate'
    )),
    -- For an award: the whole ranked comparison. For an escalation: enough for a human to
    -- take a live call without reading a transcript first.
    context jsonb not null default '{}'::jsonb,
    status text not null default 'open'
        check (status in ('open', 'approved', 'rejected', 'handled', 'expired')),
    raised_at timestamptz not null default now(),
    decided_at timestamptz,
    decided_by text,
    note text,

    constraint decided_has_decider check (
        status = 'open' or (decided_at is not null and decided_by is not null)
    )
);

create index approvals_open_idx on public.approvals (status, raised_at desc);

create table public.commitments (
    id uuid primary key default gen_random_uuid(),
    order_id uuid not null references public.orders (id) on delete restrict,
    quote_id uuid not null references public.quotes (id) on delete restrict,
    state text not null default 'verbal' check (state in (
        'verbal', 'recap_sent', 'committed', 'superseded', 'not_committed', 'executed'
    )),
    evidence_call_id uuid not null references public.calls (id) on delete restrict,
    -- INVARIANT 2. If a commitment cannot exist without the offset at which it was agreed,
    -- then nothing has to watch for one appearing without it.
    evidence_anchor_ms integer not null check (evidence_anchor_ms >= 0),
    terms jsonb not null default '{}'::jsonb,
    canonical_sha256 text,
    claimed_identity text,
    identity_level smallint not null default 0 check (identity_level between 0 and 3),
    -- Renegotiation creates a new row pointing back here. It never edits this one.
    superseded_by uuid references public.commitments (id) on delete restrict,
    approval_id uuid references public.approvals (id) on delete restrict,
    created_at timestamptz not null default now()
);

-- One live commitment per order. The dead states step out of the way so a renegotiation can
-- insert its replacement without the old row blocking it.
create unique index commitments_one_live_per_order
    on public.commitments (order_id)
    where state not in ('superseded', 'not_committed');

create table public.notifications (
    id uuid primary key default gen_random_uuid(),
    order_id uuid references public.orders (id) on delete restrict,
    call_id uuid references public.calls (id) on delete restrict,
    commitment_id uuid references public.commitments (id) on delete restrict,
    approval_id uuid references public.approvals (id) on delete restrict,
    channel text not null check (channel in ('email', 'whatsapp')),
    to_address text not null,
    subject text,
    body text not null,
    -- 'sent' is the gate that promotes a commitment to COMMITTED. 'failed' means there was
    -- no commitment, not that there was a defective one.
    status text not null default 'pending' check (status in ('pending', 'sent', 'failed')),
    provider_message_id text,
    error text,
    sent_at timestamptz,
    created_at timestamptz not null default now()
);

create index notifications_commitment_idx on public.notifications (commitment_id);

-- ============================================================ append-only, by grant

-- Convention is not a guarantee. The backend holds the service role, so "the backend could
-- rewrite its own refusals" is a real risk; revoking the privilege is a real answer.
revoke update, delete on public.decisions from anon, authenticated, service_role;
revoke update, delete on public.events from anon, authenticated, service_role;

-- ======================================================================== row security

alter table public.carriers enable row level security;
alter table public.orders enable row level security;
alter table public.calls enable row level security;
alter table public.quotes enable row level security;
alter table public.decisions enable row level security;
alter table public.events enable row level security;
alter table public.call_reports enable row level security;
alter table public.approvals enable row level security;
alter table public.commitments enable row level security;
alter table public.notifications enable row level security;

-- No policies are defined on purpose. RLS with no policy denies every anon and authenticated
-- request, and the backend uses the service role, which bypasses it. The dashboard therefore
-- has no direct path to call evidence: it reads through /api or not at all.

comment on table public.orders is
    'The shipment, its mandate and its clocks. One mandate per order; history lives in decisions.';
comment on table public.quotes is
    'What a carrier said it would do. A changed quote is a new row, never an edit.';
comment on table public.decisions is
    'Every policy evaluation including refusals. Append-only. The cap is copied by value.';
comment on table public.commitments is
    'An authorized obligation with evidence. No anchor, no commitment -- see the NOT NULL.';
comment on table public.approvals is
    'One human inbox: award decisions, mid-call escalations, and incidents.';
comment on table public.notifications is
    'The written recap. status = sent is what promotes a commitment to committed.';
