"""
Pull completed extractions from the Supabase document_extractions table
and write them to local extracted_docs/<tender_id>/<filename>.json folders.

Run standalone:
    python tools/pull_extractions.py

Or imported and called as pull_extractions() from the analysis pipeline.
"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import settings as config
from db.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

DOCS_DIR = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "documents")
TABLE    = "document_extractions"


def pull_extractions(docs_dir: str = DOCS_DIR) -> dict:
    """
    Fetch all completed rows from document_extractions and write each to:
        <docs_dir>/<tender_resource_id>/extracted_docs/<filename>.json

    Returns a dict with counts: pulled, skipped, failed.
    """
    db = SupabaseClient()

    print("Fetching completed extractions from Supabase...")
    rows = []
    page_size = 1000
    offset = 0
    while True:
        batch = (
            db.supabase.table(TABLE)
            .select("tender_resource_id,filename,extracted_text")
            .eq("extraction_status", "completed")
            .range(offset, offset + page_size - 1)
            .execute()
            .data or []
        )
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    print(f"  {len(rows)} completed extraction(s) found in DB.")

    pulled = skipped = failed = 0

    for row in rows:
        tender_id      = row.get("tender_resource_id", "")
        filename       = row.get("filename", "")
        extracted_text = row.get("extracted_text") or ""

        if not tender_id or not filename:
            failed += 1
            continue

        folder_path    = os.path.join(docs_dir, tender_id)
        extracted_dir  = os.path.join(folder_path, "extracted_docs")
        output_path    = os.path.join(extracted_dir, f"{filename}.json")

        if os.path.exists(output_path):
            skipped += 1
            continue

        if not os.path.isdir(folder_path):
            # Tender folder doesn't exist locally — skip
            skipped += 1
            continue

        os.makedirs(extracted_dir, exist_ok=True)

        try:
            payload = {
                "source_file": filename,
                "content": {
                    "markdown": extracted_text,
                },
            }
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            pulled += 1
        except Exception as e:
            logger.warning(f"Failed to write {output_path}: {e}")
            failed += 1

    print(f"  Pulled: {pulled} | Skipped (exists): {skipped} | Failed: {failed}")
    return {"pulled": pulled, "skipped": skipped, "failed": failed}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    pull_extractions()
