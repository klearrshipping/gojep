"""
DB helpers for the gojep_email_updates table (audit log).

Every email fetched from Gmail gets a row here. Status lifecycle:

  pending        — just fetched, not yet triaged
  queued         — matched to a tender; action queued in gojep_email_actions
  actioned       — action completed successfully
  failed         — action raised an exception
  skipped        — informational email, open tender, no action needed
  discarded      — tender not in gojep_tenders_current, or duplicate
  manual_review  — Path B email that could not be matched confidently
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_pending(db, parsed: dict[str, Any]) -> bool:
    """
    Insert a parsed email as 'pending'.
    Uses ignore_duplicates=True — if the email was already saved from a
    previous run (any status), it is left untouched.
    Returns True if inserted, False if skipped (already exists) or on error.
    """
    from config import settings as config
    import json as _json

    row = {
        "email_message_id":  parsed["email_message_id"],
        "received_at":       parsed.get("received_at"),
        "sender":            parsed.get("sender"),
        "subject":           parsed.get("subject"),
        "path":              parsed.get("path"),
        "resource_id":       parsed.get("resource_id"),
        "tender_title":      parsed.get("tender_title"),
        "update_type":       parsed.get("update_type"),
        "action_url":        parsed.get("action_url"),
        "action_url_type":   parsed.get("action_url_type"),
        "extracted_dates":   _json.dumps(parsed["extracted_dates"]) if parsed.get("extracted_dates") else None,
        "processing_status": "pending",
        "inserted_at":       _now_utc(),
    }

    try:
        result = db.supabase.table(config.SUPABASE_TABLE_EMAIL_UPDATES).upsert(
            row, on_conflict="email_message_id", ignore_duplicates=True
        ).execute()
        inserted = bool(result.data)
        return inserted
    except Exception as e:
        logger.warning("Failed to insert pending email %s: %s", parsed["email_message_id"], e)
        return False


def get_pending_emails(db) -> list[dict[str, Any]]:
    """
    Return all emails with processing_status='pending' ordered by received_at.
    Used by triage to process both current-run and any leftover emails from
    previous runs that were saved but not yet triaged.
    """
    from config import settings as config
    import json as _json

    try:
        rows = (
            db.supabase.table(config.SUPABASE_TABLE_EMAIL_UPDATES)
            .select("*")
            .eq("processing_status", "pending")
            .order("received_at", desc=False)
            .execute()
            .data or []
        )
        # Deserialise extracted_dates from JSON string back to dict
        for row in rows:
            if row.get("extracted_dates") and isinstance(row["extracted_dates"], str):
                try:
                    row["extracted_dates"] = _json.loads(row["extracted_dates"])
                except Exception:
                    row["extracted_dates"] = {}
            elif not row.get("extracted_dates"):
                row["extracted_dates"] = {}
        return rows
    except Exception as e:
        logger.warning("Failed to fetch pending emails: %s", e)
        return []


def _update_status(db, email_message_id: str, status: str, extra: dict | None = None) -> None:
    from config import settings as config

    patch: dict[str, Any] = {"processing_status": status, "processed_at": _now_utc()}
    if extra:
        patch.update(extra)

    try:
        db.supabase.table(config.SUPABASE_TABLE_EMAIL_UPDATES).update(patch).eq(
            "email_message_id", email_message_id
        ).execute()
    except Exception as e:
        logger.warning("Failed to update status for %s -> %s: %s", email_message_id, status, e)


def mark_queued(db, email_message_id: str) -> None:
    """Action has been queued in gojep_email_actions — awaiting processing."""
    _update_status(db, email_message_id, "queued")


def mark_actioned(db, email_message_id: str) -> None:
    """Primary action completed successfully."""
    _update_status(db, email_message_id, "actioned")


def mark_failed(db, email_message_id: str, error: str) -> None:
    _update_status(db, email_message_id, "failed", {"error_message": error})


def mark_skipped(db, email_message_id: str) -> None:
    _update_status(db, email_message_id, "skipped")


def mark_discarded(db, email_message_id: str) -> None:
    _update_status(db, email_message_id, "discarded")


def mark_manual_review(db, email_message_id: str, reason: str) -> None:
    _update_status(db, email_message_id, "manual_review", {"error_message": reason})
