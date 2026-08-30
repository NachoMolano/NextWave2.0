-- Seed: three carriers who disagree with each other, one carrier who is not on file, and one
-- container with a demurrage clock already running.
--
-- The personalities are the point. Three carriers who all quote the same number prove nothing:
-- the comparison has to choose, and a human has to be able to see why it chose. So one is
-- cheap and slow, one is fast and expensive, and one does not answer the phone -- which is
-- not a failure to handle, it is the third outcome an RFQ actually has.
--
-- The fourth carrier is on nobody's approved list. `is_on_file = false` is what makes the
-- agent decline to quote them at all: Volta onboards nobody by phone, and refusing a stranger
-- who says the right things is a case worth being able to demonstrate on demand.
--
-- Deliberately NOT seeded: the mandate. `mandate_version` stays 0, so the order arrives
-- authorizing nothing. A human grants the ceiling through POST /api/orders/{id}/mandate, and
-- that being a separate, visible, attributable step is the whole shape of the system. Seeding
-- a cap would skip the only moment where authority enters.
--
-- Numbers are placeholders. scripts/seed.py overrides them from SEED_CARRIER_PHONE_1..3 so a
-- live demo dials real handsets without those numbers living in a public repository.

insert into public.carriers (name, phone, contact_name, email, is_on_file, is_active, persona)
values
    ('Fletes del Pacifico', '+523141000001', 'Luis Ramirez', 'luis@fletespacifico.test',
     true, true,
     'Cheap and slow. Quotes near 9,800 MXN and needs 48 hours notice. Wins on price, loses '
     'the window when the last free day is close.'),
    ('Autolineas Manzanillo', '+523141000003', 'Jorge Mendoza', 'jorge@autolineasmzo.test',
     true, true,
     'Fast and expensive. Quotes near 12,400 MXN and can be at the terminal in 12 hours. The '
     'right answer only when demurrage costs more than the difference.'),
    ('Transportes Colima', '+523141000002', 'Ana Beltran', 'ana@transportescolima.test',
     true, true,
     'Does not answer. Reliable when reached and quotes near 10,600 MXN, but the RFQ usually '
     'times out on this one. Three dials, two answers is the normal case, not the sad path.'),
    ('Transportes Fantasma', '+523141000009', 'Unknown', null,
     false, true,
     'Not on file. Says the right things and may quote a very good number. The agent must '
     'decline to quote and escalate: nobody is onboarded over the phone.')
on conflict (phone) do update set
    name         = excluded.name,
    contact_name = excluded.contact_name,
    email        = excluded.email,
    is_on_file   = excluded.is_on_file,
    is_active    = excluded.is_active,
    persona      = excluded.persona;

-- One container, mid-clock. The dates are relative to now() on purpose: a seed with a frozen
-- last free day is expired by the second day of a build, and a countdown that reads "-4 days"
-- teaches a judge nothing.
insert into public.orders (
    reference, status, origin, destination, cargo, equipment, weight, container_number,
    discharged_at, free_days, last_free_day, delivery_deadline, payload
)
values (
    'OP-MZO-0001', 'received',
    'Contecon Manzanillo', 'Av. Lopez Mateos 1200, Guadalajara, Jalisco',
    'Textiles', '40-foot container chassis', '18400 kg', 'MSCU1234566',
    now() - interval '2 days', 5, current_date + 3, now() + interval '4 days',
    jsonb_build_object(
        'bill_of_lading', 'MEDUMZ0099231',
        'vessel', 'MSC Rania',
        'voyage', 'FT534A',
        'ocean_carrier', 'MSC',
        'packages', 620,
        'destination_postal_code', '44940'
    )
)
on conflict (reference) do update set
    status            = 'received',
    origin            = excluded.origin,
    destination       = excluded.destination,
    cargo             = excluded.cargo,
    equipment         = excluded.equipment,
    weight            = excluded.weight,
    container_number  = excluded.container_number,
    discharged_at     = excluded.discharged_at,
    free_days         = excluded.free_days,
    last_free_day     = excluded.last_free_day,
    delivery_deadline = excluded.delivery_deadline,
    payload           = excluded.payload,
    updated_at        = now();
