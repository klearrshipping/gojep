-- Migration: 002_create_email_actions.sql
--
-- Adds the gojep_email_actions table which tracks the downstream pipeline
-- state for every email that triggered a concrete action (document download,
-- field patch, etc.).
--
-- Also renames the 'processed' status in gojep_email_updates to 'actioned'
-- to make it clear that an action was taken — not that downstream processing
-- (extraction, analysis) is complete.
--
-- Run once in Supabase SQL editor.

-- ── 1. Rename 'processed' → 'actioned' in gojep_email_updates ───────────────

update gojep_email_updates
set    processing_status = 'actioned'
where  processing_status = 'processed';

comment on column gojep_email_updates.processing_status is
  'pending | actioned | failed | skipped | discarded | manual_review';


-- ── 2. Create gojep_email_actions ────────────────────────────────────────────

create table if not exists gojep_email_actions (
    id                      bigserial    primary key,

    -- Link back to the audit log
    email_message_id        text         not null references gojep_email_updates(email_message_id),

    -- Tender context
    competition_unique_id   text,
    resource_id             text,
    tender_title            text,

    -- What the email was about
    update_type             text,        -- new_documents | clarification_response | addendum | modifications | ...

    -- What was actually done
    files_downloaded        jsonb,       -- ["filename1.pdf", ...] — null if no download
    fields_changed          jsonb,       -- {"deadline": "2026-05-01", ...} — null if no field patch

    -- Downstream pipeline state
    extraction_status       text         not null default 'pending',
                                         -- pending | completed | failed | not_required
    analysis_status         text         not null default 'pending',
                                         -- pending | completed | failed

    -- Timestamps
    actioned_at             timestamptz  not null default now(),
    extraction_completed_at timestamptz,
    analysis_completed_at   timestamptz,

    -- Error detail
    error_message           text
);

-- Indexes
create index if not exists idx_email_actions_competition   on gojep_email_actions (competition_unique_id);
create index if not exists idx_email_actions_resource_id   on gojep_email_actions (resource_id);
create index if not exists idx_email_actions_extraction    on gojep_email_actions (extraction_status);
create index if not exists idx_email_actions_analysis      on gojep_email_actions (analysis_status);
create index if not exists idx_email_actions_actioned_at   on gojep_email_actions (actioned_at desc);
