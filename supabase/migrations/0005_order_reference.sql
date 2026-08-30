-- The folio becomes something the system allocates, not something a person remembers to type.
--
-- orders.reference has always been `not null unique`, but nothing generated it: it arrived in
-- the POST /api/orders body, the seed script hardcoded one, and the portal had no way to
-- create an order at all. That was survivable while the folio was only a label on a screen.
--
-- It is not a label any more. On an inbound call the folio is what the caller states to prove
-- who they are, so uniqueness is now a security property rather than a tidiness one, and
-- "whoever typed it last picked a fresh one" is the wrong guarantee to rest that on. A
-- sequence cannot collide, cannot be reused after a delete, and hands out the same shape every
-- time -- which matters when a driver has to read it aloud from a creased dispatch sheet.
--
-- The prefix stays in application code: it is derived from the origin port (OP-MZO, OP-SFO)
-- and is presentation, not identity. The sequence is the part that must not race.

create sequence if not exists orders_reference_seq as bigint start with 1;

-- Allocation is a function rather than a column default so that the caller learns the folio
-- in the same round trip that reserves it. A default would only reveal it on the way back
-- from the insert, and the intake screen has to show the operator what to tell the carrier.
create or replace function next_order_reference(prefix text)
returns text
language sql
volatile
as $$
  select upper(trim(prefix)) || '-' || lpad(nextval('orders_reference_seq')::text, 4, '0');
$$;

comment on function next_order_reference(text) is
  'Allocate the next order folio, e.g. OP-MZO-0007. Volatile and sequence-backed: two '
  'concurrent intakes must never receive the same folio, because the folio authenticates '
  'an inbound caller.';

-- Start the sequence past the folios that were typed by hand, so the first generated one
-- cannot collide with an order already on the demo screen. Reads only the four-digit tail.
select setval(
  'orders_reference_seq',
  greatest(
    (select coalesce(max(substring(reference from '([0-9]+)$')::bigint), 0) from orders),
    1
  )
);

-- PostgREST reaches the function through the anon/authenticated roles it impersonates; the
-- secret key used server-side bypasses RLS but still needs execute. Granted narrowly: the
-- portal never calls this, only the backend does.
revoke all on function next_order_reference(text) from public;
grant execute on function next_order_reference(text) to service_role;
