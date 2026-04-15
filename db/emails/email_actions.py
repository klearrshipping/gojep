"""
DB helpers for the gojep_email_actions table (work queue).

Rows are inserted DURING TRIAGE — before any action is taken — so the table
acts as a queue. The primary action lifecycle is tracked via action_status.
Downstream extraction and analysis are tracked separately.

Action lifecycle:
  insert_queued(...)              — called during triage when email is actionable
  mark_action_completed(id, ...)  — primary action succeeded (files downloaded / fields patched)
  mark_action_failed(id, error)   — primary action failed

Downstream lifecycle (called from _run_reanalysis_queue):
  mark_extraction_done(id)
  mark_extraction_failed(id, error)
  mark_analysis_done(id)
  mark_analysis_failed(id, error)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from config import settings as config

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Triage: queue an actionable email ────────────────────────────────────────

def insert_queued(
    db,
    email_message_id: str,
    competition_unique_id: str | None,
    resource_id: str | None,
    tender_title: str | None,
    update_type: str | None,
    action_url: str | None = None,
    action_url_type: str | None = None,
    needs_extraction: bool = True,
) -> int | None:
    """
    Insert a row into gojep_email_actions during triage.
    action_status starts as 'pending' — the action has not yet run.
    Returns the new row id, or None on failure.
    """
    extraction_status = "pending" if needs_extraction else "not_required"

    row = {
        "email_message_id":      email_message_id,
        "competition_unique_id": competition_unique_id,
        "resource_id":           resource_id,
        "tender_title":          tender_title,
        "update_type":           update_type,
        "action_url":            action_url,
        "action_url_type":       action_url_type,
        "action_status":         "pending",
        "extraction_status":     extraction_status,
        "analysis_status":       "pending",
        "actioned_at":           _now_utc(),
    }

    try:
        result = (
            db.supabase.table(config.SUPABASE_TABLE_EMAIL_ACTIONS)
            .insert(row)
            .execute()
        )
        rows = result.data
        if rows:
            return rows[0].get("id")
    except Exception as e:
        logger.warning("Failed to queue action for %s: %s", email_message_id, e)

    return None


# ── Action execution ──────────────────────────────────────────────────────────

def _update_action(db, action_id: int, patch: dict[str, Any]) -> None:
    try:
        db.supabase.table(config.SUPABASE_TABLE_EMAIL_ACTIONS).update(patch).eq(
            "id", action_id
        ).execute()
    except Exception as e:
        logger.warning("Failed to update action %s: %s", action_id, e)


def mark_action_completed(
    db,
    action_id: int,
    files_downloaded: list[str] | None = None,
    fields_changed: dict[str, Any] | None = None,
) -> None:
    """Primary action succeeded — record what was downloaded/changed."""
    patch: dict[str, Any] = {"action_status": "completed"}
    if files_downloaded:
        patch["files_downloaded"] = json.dumps(files_downloaded)
    if fields_changed:
        patch["fields_changed"] = json.dumps(fields_changed)
    _update_action(db, action_id, patch)


def mark_action_failed(db, action_id: int, error: str) -> None:
    _update_action(db, action_id, {
        "action_status": "failed",
        "error_message": error,
    })


# ── Downstream pipeline ───────────────────────────────────────────────────────

def mark_extraction_done(db, action_id: int) -> None:
    _update_action(db, action_id, {
        "extraction_status":       "completed",
        "extraction_completed_at": _now_utc(),
    })


def mark_extraction_failed(db, action_id: int, error: str) -> None:
    _update_action(db, action_id, {
        "extraction_status": "failed",
        "error_message":     error,
    })


def mark_analysis_done(db, action_id: int) -> None:
    _update_action(db, action_id, {
        "analysis_status":       "completed",
        "analysis_completed_at": _now_utc(),
    })


def mark_analysis_failed(db, action_id: int, error: str) -> None:
    _update_action(db, action_id, {
        "analysis_status": "failed",
        "error_message":   error,
    })


# ── Queue queries ─────────────────────────────────────────────────────────────

def get_pending_actions(db) -> list[dict]:
    """Return all rows where the primary action has not yet run."""
    try:
        result = (
            db.supabase.table(config.SUPABASE_TABLE_EMAIL_ACTIONS)
            .select("*")
            .eq("action_status", "pending")
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.warning("Failed to fetch pending actions: %s", e)
        return []
