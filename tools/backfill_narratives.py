"""
Backfill narrative_analysis for existing records in gojep_analysis_results.

Reads rows where narrative_analysis is null, runs the consolidation pass
on the already-saved structured fields, and updates the row in-place.
No document re-reading or chunk extraction — operates purely on DB data.

Usage:
    python tools/backfill_narratives.py
    python tools/backfill_narratives.py --limit 10        # process first N rows
    python tools/backfill_narratives.py --rerun           # reprocess all rows, even those with existing narratives
    python tools/backfill_narratives.py --tender 1020/845 # process a single tender
"""

import argparse
import logging
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings as config
from db.client.supabase_client import SupabaseClient
from modules.analysis.parse import _consolidate
from modules.analysis.prompt import ANALYSIS_OUTPUT_FIELDS, LIST_FIELDS, RATE_LIMIT_DELAY

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

FETCH_COLS = (
    "competition_unique_id,resource_id,tender_folder,"
    "contract_title,procuring_entity,contract_type,scope_of_work,"
    "contract_value,contract_duration,submission_deadline,suitability_summary,"
    "eligibility_requirements,experience_requirements,financial_requirements,"
    "mandatory_documents,evaluation_criteria,key_milestones,lots,special_conditions,"
    "db_procurement_method,db_evaluation_mechanism"
)


def _row_to_parsed(row: dict) -> dict:
    """Convert a DB row back into the parsed dict format expected by _consolidate."""
    parsed = {}
    for field in ANALYSIS_OUTPUT_FIELDS:
        val = row.get(field)
        if val is not None:
            parsed[field] = val
    return parsed


def _row_to_db_meta(row: dict) -> dict:
    """Extract DB metadata fields stored with db_ prefix."""
    return {
        "competition_unique_id": row.get("competition_unique_id"),
        "procurement_method": row.get("db_procurement_method"),
        "evaluation_mechanism": row.get("db_evaluation_mechanism"),
    }


def fetch_rows(db: SupabaseClient, rerun: bool, tender_id: str = None, limit: int = 0) -> list:
    """
    Fetch rows to process from gojep_analysis_results.
    Filters in Python to avoid PostgREST URL-encoding issues with slashes in competition_unique_id.
    """
    rows = []
    page_size = 200
    offset = 0
    while True:
        batch = (
            db.supabase.table(config.SUPABASE_TABLE_ANALYSIS_RESULTS)
            .select(FETCH_COLS)
            .range(offset, offset + page_size - 1)
            .execute()
            .data or []
        )
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    # Filter in Python
    if tender_id:
        rows = [r for r in rows if r.get("competition_unique_id") == tender_id]
    elif not rerun:
        rows = [r for r in rows if not r.get("narrative_analysis")]

    return rows[:limit] if limit else rows


def process_row(db: SupabaseClient, row: dict) -> bool:
    """Run consolidation on a single row and update it in the DB. Returns True on success."""
    uid = row.get("competition_unique_id") or row.get("tender_folder", "unknown")
    parsed = _row_to_parsed(row)
    db_meta = _row_to_db_meta(row)

    if not any(parsed.get(f) for f in LIST_FIELDS):
        logger.warning(f"[{uid}] No list fields found — skipping")
        return False

    try:
        updated, narrative = _consolidate(parsed, db_meta=db_meta)
        if not narrative:
            logger.warning(f"[{uid}] Consolidation returned empty narrative")
            return False

        # Build update payload — clean list fields + narrative
        update_payload: dict = {"narrative_analysis": narrative}
        for field in LIST_FIELDS:
            clean = updated.get(field)
            if clean and isinstance(clean, list):
                update_payload[field] = clean

        # Update by tender_folder (no slash) to avoid PostgREST URL-encoding issues
        db.supabase.table(config.SUPABASE_TABLE_ANALYSIS_RESULTS)\
            .update(update_payload)\
            .eq("tender_folder", row.get("tender_folder") or uid.replace("/", "_", 1))\
            .execute()

        return True
    except Exception as e:
        logger.error(f"[{uid}] Failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Backfill narrative_analysis for existing tender analyses.")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to process (0 = all)")
    parser.add_argument("--rerun", action="store_true", help="Reprocess rows that already have a narrative")
    parser.add_argument("--tender", type=str, default=None, help="Process a single tender by competition_unique_id")
    args = parser.parse_args()

    db = SupabaseClient()
    rows = fetch_rows(db, rerun=args.rerun, tender_id=args.tender, limit=args.limit)
    total = len(rows)

    if total == 0:
        print("No rows to process.")
        return

    print(f"Processing {total} row(s)...\n")
    success = failed = 0

    for i, row in enumerate(rows, 1):
        uid = row.get("competition_unique_id") or row.get("tender_folder", "unknown")
        print(f"[{i}/{total}] {uid}", flush=True)

        if process_row(db, row):
            success += 1
            print(f"  -> Done", flush=True)
            time.sleep(RATE_LIMIT_DELAY)
        else:
            failed += 1

    print(f"\nComplete — {success} succeeded, {failed} failed out of {total}")


if __name__ == "__main__":
    main()
