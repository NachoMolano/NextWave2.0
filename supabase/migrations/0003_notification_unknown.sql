-- A provider timeout or malformed success body does not prove delivery failed. The message
-- may have left, so this state blocks blind retry until a human reconciles the provider log.
alter table public.notifications
    drop constraint if exists notifications_status_check;

alter table public.notifications
    add constraint notifications_status_check
    check (status in ('pending', 'sent', 'failed', 'unknown'));
