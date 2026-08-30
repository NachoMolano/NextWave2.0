-- Intake becomes a gate rather than a label.
--
-- An order arrived, a mandate was granted and three carriers were dialled on a container
-- nobody had confirmed was released, with no demurrage clock and no cargo cutoff. Every one
-- of those is a fact a person holds and the system never asked for, so the first stage of
-- the process had nothing to do and nothing to refuse.
--
-- released_at is the one that changes behaviour: until it is set, no mandate may be granted
-- and no carrier may be dialled. It records *when a person confirmed* the container is
-- available to move -- not when the terminal released it, which is a fact we do not observe.
-- released_by is kept beside it for the same reason mandate_set_by is: an authorization with
-- no name on it is not an audit trail.
--
-- Nullable, with no default and no backfill. Every existing order is therefore unreleased,
-- which is the honest answer: nobody confirmed those, and inventing a release timestamp to
-- make old rows pass the new gate would defeat the gate on its first day.

alter table orders
  add column if not exists released_at  timestamptz,
  add column if not exists released_by  text,
  add column if not exists release_note text;

comment on column orders.released_at is
  'When a person confirmed the container is released and ready to move. Null blocks the '
  'mandate and blocks dialling. Never inferred from discharge or from a carrier''s claim.';

comment on column orders.released_by is
  'Who confirmed the release. From the portal credential, never from a request body.';

-- The intake queue: what is waiting on a person before Volta may do anything at all.
create index if not exists orders_unreleased_idx
  on orders (created_at desc)
  where released_at is null;
