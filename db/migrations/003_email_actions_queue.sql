-- Migration: 003_email_actions_queue.sql
--
-- gojep_email_actions is now a work queue populated BEFORE processing begins.
-- Adds action_status to track the primary action lifecycle separately from
-- the downstream extraction/analysis pipeline.
--
-- Run once in Supabase SQL editor.

-- ── 1. Add action_status column ──────────────────────────────────────────────

alter table gojep_email_actions
    add column if not exists action_status text not null default 'pending';

comment on column gojep_email_actions.action_status is
    'pending | completed | failed — tracks whether the primary action (download/patch) has run';

-- ── 2. Add queued status to gojep_email_updates ───────────────────────────────

comment on column gojep_email_updates.processing_status is
    'pending | queued | actioned | failed | skipped | discarded | manual_review';

-- Backfill: existing actioned rows in email_updates have completed actions
update gojep_email_actions
set    action_status = 'completed'
where  action_status = 'pending'
  and  id in (
      select a.id
      from   gojep_email_actions a
      join   gojep_email_updates u on u.email_message_id = a.email_message_id
      where  u.processing_status = 'actioned'
  );

-- ── 3. Add action URL columns (needed to re-run actions from the queue) ──────

alter table gojep_email_actions
    add column if not exists action_url      text,
    add column if not exists action_url_type text;

-- ── 4. Index on action_status ────────────────────────────────────────────────

create index if not exists idx_email_actions_action_status on gojep_email_actions (action_status);
