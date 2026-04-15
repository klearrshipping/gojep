-- Migration: 004_email_updates_extracted_dates.sql
-- Adds extracted_dates column to gojep_email_updates so Path B date data
-- is persisted and available when triaging emails from previous runs.
-- Run once in Supabase SQL editor.

alter table gojep_email_updates
    add column if not exists extracted_dates jsonb;
