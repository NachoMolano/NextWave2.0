-- 0002 — the row-security posture, made explicit and made durable.
--
-- 0001 enabled RLS on all ten tables and deliberately wrote no policies: RLS with no policy
-- denies every anon and authenticated request, and the backend uses the service role, which
-- bypasses RLS. That is correct, and this migration does not change it.
--
-- What it changes is what sits *underneath* that decision.
--
-- Two facts that are easy to miss and that together are the whole argument:
--
--   1. `service_role` holds the BYPASSRLS attribute. RLS therefore constrains nothing the
--      backend does. It is not, and cannot be, a control on our own code. The only thing
--      that constrains the backend is the GRANT layer -- which is exactly why
--      `revoke update, delete on decisions, events` in 0001 is load-bearing and why the
--      append-only claim survives contact with a bug in our own service.
--
--   2. Supabase's default privileges grant full CRUD on every new public table to `anon`
--      and `authenticated`. After 0001, `anon` -- the role behind the publishable key that
--      ships inside any browser -- held DELETE, INSERT, SELECT and UPDATE on `calls`,
--      `commitments`, `carriers`, `orders` and the rest. Every row was still denied, because
--      RLS was on and no policy existed. So the safety of call recordings, transcripts and
--      commitments rested entirely on nobody ever writing a permissive policy.
--
-- One `create policy ... using (true)`, added later by someone reasonably trying to let the
-- dashboard read its own data, would have turned that into public read and write access to
-- the evidence. The privilege was the loaded gun; RLS was the safety catch.
--
-- So: take the privileges away. A policy added later then grants nothing on its own, and
-- whoever adds one has to grant the privilege too -- deliberately, in a migration, in a diff
-- somebody reviews. Defence in depth, and no behavioural change: the backend uses the
-- service role and is untouched.

revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from anon, authenticated;

-- The same for anything created later, or 0003 silently reopens the hole. Supabase declares
-- its defaults FOR ROLE postgres, so the counter-declaration has to name that role too.
alter default privileges for role postgres in schema public
    revoke all on tables from anon, authenticated;
alter default privileges for role postgres in schema public
    revoke all on sequences from anon, authenticated;
alter default privileges for role postgres in schema public
    revoke all on functions from anon, authenticated;

-- `usage` on the schema is left in place. Without table privileges it grants nothing, and
-- removing it produces a confusing "schema does not exist" instead of an honest
-- "permission denied" if anyone ever points a client key at this database.

-- WHEN POLICIES BECOME NECESSARY, AND WHAT THEY SHOULD LOOK LIKE
--
-- Not now. Nothing but the backend talks to this database, no authentication exists, and the
-- dashboard reads through /api precisely so that evidence can be scoped and redacted by code
-- that policy has already seen. A policy written today would be a guess at a threat model we
-- do not have yet.
--
-- The trigger is a real one: an authenticated dashboard reading Supabase directly. On that
-- day, and not before:
--
--   * `carriers`, `orders`, `quotes`, `calls` get `for select` policies scoped to the
--     viewer's organisation, plus an explicit `grant select` -- and nothing else. No insert,
--     no update: every mutation keeps going through tools/, which is where policy lives.
--   * `decisions`, `events`, `commitments`, `call_reports` get no policies at all. Refusals,
--     the audit ledger and the evidence chain stay server-side, because what a person may
--     see of them is a redaction decision, not a row filter.
--   * `notifications` gets none either: it carries recipient addresses.
--   * The backend moves off `service_role` onto a dedicated non-BYPASSRLS role, or RLS still
--     will not constrain it and this whole exercise buys nothing against our own bugs.
--
-- Recorded here rather than in a doc because the next person to need it will be reading this
-- directory, not the wiki.
