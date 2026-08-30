-- 0003 — who Volta works for, and where the cargo is going.
--
-- BUILD_PLAN section 2 put this under "Not tables: company_profile (one row -> config.py +
-- domain/company.py)", and for the prompt fields that was right: an agent name and a fallback
-- language are configuration, they change when someone redeploys, and a table for them is a
-- table with no writer.
--
-- The warehouse is not that. It is operational fact with a street address, a contact and
-- opening hours; it changes because the business changed and not because a deployment did,
-- and the person who knows it is an operator with a browser, not whoever holds the env vars.
-- Config that only a redeploy can change is the wrong home for something a dispatcher needs
-- to correct at four in the afternoon.
--
-- One row, and the database is what says so: `id smallint primary key check (id = 1)`. The
-- alternative is a convention that every reader has to remember -- `limit 1`, and hope. This
-- is the same argument as the partial unique indexes in 0001: an invariant worth having is
-- worth being unable to violate.

create table public.company_profile (
    id smallint primary key default 1 check (id = 1),

    -- The business. Rendered into every prompt, so it is read out loud.
    display_name text not null check (length(trim(display_name)) > 0),
    legal_name text,
    business_type text not null default 'importer',
    city text,
    country text,
    currency char(3) not null default 'USD',
    timezone text not null default 'America/Mexico_City',
    business_hours text,

    -- The agent's identity on the phone.
    agent_name text not null default 'Volta',
    agent_role text not null default 'transport coordinator',
    primary_language text not null default 'en',
    fallback_language text not null default 'es-MX',

    -- Where the cargo is delivered. The address is spoken to carriers; the contact and the
    -- hours are what a driver needs on arrival and what a dispatcher asks for on the phone.
    warehouse_name text,
    warehouse_address text,
    warehouse_city text,
    warehouse_state text,
    warehouse_postal_code text,
    warehouse_country text,
    warehouse_contact_name text,
    warehouse_phone text,
    warehouse_hours text,
    warehouse_notes text,

    updated_at timestamptz not null default now(),
    -- Who last changed it. Same reason the mandate records who granted it: a business fact
    -- the agent will say out loud should carry the name of whoever last said it was true.
    updated_by text
);

alter table public.company_profile enable row level security;

comment on table public.company_profile is
    'One row. The business Volta speaks for and the warehouse it delivers to.';

-- Seeded so the portal has something to show before anyone opens the form. Every value here
-- is a placeholder an operator is expected to correct; none of it is a secret, and secrets
-- stay in config.py where a redeploy is the right way to rotate them.
insert into public.company_profile (
    display_name, legal_name, business_type, city, country, currency, timezone,
    business_hours, agent_name, agent_role, primary_language, fallback_language,
    warehouse_name, warehouse_address, warehouse_city, warehouse_state,
    warehouse_postal_code, warehouse_country, warehouse_contact_name, warehouse_phone,
    warehouse_hours, warehouse_notes, updated_by
)
values (
    'Pacific Textiles', 'Pacific Textiles S.A. de C.V.', 'importer',
    'Guadalajara', 'Mexico', 'USD', 'America/Mexico_City',
    'Monday to Friday, 08:00-18:00',
    'Volta', 'transport coordinator', 'en', 'es-MX',
    'Tampa distribution centre', '4102 N 40th St', 'Tampa', 'Florida',
    '33610', 'United States', 'Marta Salinas', '+18135550142',
    'Monday to Saturday, 07:00-19:00. Appointment required for 40-foot chassis.',
    'Dock 4 for containers. Overheight loads use the west gate.',
    'seed'
)
on conflict (id) do nothing;
