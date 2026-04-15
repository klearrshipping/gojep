-- Migration: 001_create_email_updates.sql
-- Creates the gojep_email_updates table to track all incoming tender notification emails.
-- Run once in Supabase SQL editor.

create table if not exists gojep_email_updates (
    id                  bigserial primary key,

    -- Email identity
    email_message_id    text        not null unique,   -- Gmail message ID (dedup key)
    received_at         timestamptz,
    sender              text,
    subject             text,

    -- Parsed fields
    path                text,                          -- 'A' (system) or 'B' (entity)
    resource_id         text,
    tender_title        text,
    update_type         text,                          -- clarification_response, modifications, etc.
    action_url          text,
    action_url_type     text,                          -- list_clarification, prepare_view, etc.

    -- Processing lifecycle
    processing_status   text        not null default 'pending',   -- pending | processed | failed | skipped | discarded
    inserted_at         timestamptz not null default now(),
    processed_at        timestamptz,

    -- Outcome
    action_result       jsonb,                         -- what changed, files downloaded, fields patched
    error_message       text                           -- populated on failure
);

-- Index for fast lookups by resource_id and status
create index if not exists idx_email_updates_resource_id     on gojep_email_updates (resource_id);
create index if not exists idx_email_updates_status          on gojep_email_updates (processing_status);
create index if not exists idx_email_updates_update_type     on gojep_email_updates (update_type);
create index if not exists idx_email_updates_inserted_at     on gojep_email_updates (inserted_at desc);
