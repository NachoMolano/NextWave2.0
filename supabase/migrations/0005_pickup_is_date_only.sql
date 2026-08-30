-- A pickup the carrier gave as a day, not as a moment.
--
-- "September fourth" and "September fourth at two" are different utterances, and until now
-- both landed in pickup_at as a timestamp -- the first at midnight UTC, an hour nobody said.
-- Policy then judged that invented hour against a window an operator typed in local business
-- hours, so a carrier offering the FIRST day of the window was refused for being eight hours
-- early to a window their own words never addressed.
--
-- Stored rather than derived from a 00:00 timestamp on the way past: a midnight somebody said
-- and a midnight nobody said are different facts, and only the utterance can tell them apart.
-- Default false is the strict reading, so every row already on disk keeps the exact-instant
-- comparison it was judged under.

alter table public.quotes
    add column pickup_is_date_only boolean not null default false;

comment on column public.quotes.pickup_is_date_only is
    'The carrier named a day and no clock time. Policy compares the calendar day, never an invented hour.';
