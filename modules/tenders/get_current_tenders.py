"""
GOJEP Current Opportunities Extractor.

Scrapes all tenders from the portal and returns only those whose submission
deadline is at least 48 hours from now (Jamaica time).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

from db.tenders.tender_row_mapping import parse_timestamp_field
from modules.tenders.get_tenders import GOJEPScraper, JAMAICA_TZ

logger = logging.getLogger(__name__)


def run_current_tenders_extraction() -> List[Dict[str, Any]]:
    """
    Scrapes the GOJEP portal and returns records whose submission deadline
    is >= now + 48 hours (Jamaica time). Returns an empty list if none found.
    """
    jamaica_now    = datetime.now(JAMAICA_TZ)
    deadline_cutoff = jamaica_now + timedelta(hours=48)

    print("=" * 60)
    print("get-current-tenders  (Actionable 48h Sync)")
    print(f"Jamaica Time : {jamaica_now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Cutoff       : {deadline_cutoff.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    scraper = GOJEPScraper()
    output_path = scraper.run_extraction()

    if not output_path:
        logger.info("Scraper returned no output file.")
        return []

    with open(output_path, encoding="utf-8") as f:
        raw_records = json.load(f)

    if not isinstance(raw_records, list):
        logger.warning(f"Unexpected JSON structure in {output_path}")
        return []

    # Filter to tenders whose deadline is beyond the 48h cutoff
    active = []
    for rec in raw_records:
        deadline_str = rec.get("bids_submission_deadline")
        deadline_utc = parse_timestamp_field(deadline_str)
        if not deadline_utc:
            # No deadline info — include conservatively
            active.append(rec)
            continue
        try:
            from datetime import timezone
            dt = datetime.fromisoformat(deadline_utc.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= deadline_cutoff.astimezone(timezone.utc):
                active.append(rec)
        except Exception:
            active.append(rec)  # include if parse fails

    logger.info(f"Scraped {len(raw_records)} tenders, {len(active)} active beyond 48h cutoff.")
    return active
