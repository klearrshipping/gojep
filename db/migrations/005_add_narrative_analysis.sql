-- Migration 005: Add narrative_analysis column to gojep_analysis_results
--
-- Stores the LLM-generated human-readable markdown analysis for each tender.
-- Null for records analysed before this migration — use the backfill script
-- (tools/backfill_narratives.py) to populate existing rows.

ALTER TABLE gojep_analysis_results
    ADD COLUMN IF NOT EXISTS narrative_analysis TEXT;
