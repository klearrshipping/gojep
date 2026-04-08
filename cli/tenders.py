"""
CLI commands for the Tender Pipeline.

Pipeline order:
  1. get-tenders           -> Scrape listings -> gojep_tenders_all
  2. get-current-tenders   -> Scrape (sort+48h) -> gojep_tenders_current
  3. get-tender-details    -> Fetch detail pages -> update existing DB records
  4. get-tender-documents  -> Download ZIP docs for each tender
"""

from __future__ import annotations

import argparse
import logging

from config import settings
from db.supabase_client import SupabaseClient
from db.tender_row_mapping import listing_json_row_to_tender

from cli.utils import trigger_sync

logger = logging.getLogger(__name__)




# ── 1. get-tenders ---------------------------------------------------------

def run_get_tenders(args: argparse.Namespace) -> bool:
    """Scrape listings using Publication Date watermark -> push to gojep_tenders_all."""
    from modules.tenders.get_tenders import GOJEPScraper

    db = SupabaseClient()
    watermark = db.get_latest_publication_date()
    trigger_sync(args)
    print(f"get-tenders  (watermark: {watermark or 'None - full sync'})")

    scraper = GOJEPScraper()
    records = scraper.run_extraction(latest_publication_date=watermark)

    if not records:
        print("No new tenders found.")
        return False

    print(f"Pushing {len(records)} listings to {settings.SUPABASE_TABLE_TENDERS_ALL} ...")

    pushed = 0
    for i, raw in enumerate(records):
        row = listing_json_row_to_tender(raw)
        if not row:
            continue
        try:
            db.insert_tenders_batch([row], table_name=settings.SUPABASE_TABLE_TENDERS_ALL)
            pushed += 1
        except Exception as e:
            logger.error(f"[{i+1}] Failed: {e}")

    print(f"Done: {pushed}/{len(records)} listings -> {settings.SUPABASE_TABLE_TENDERS_ALL}")
    return pushed > 0


# ── 2. get-current-tenders -------------------------------------------------

def run_get_current_tenders(args: argparse.Namespace) -> bool:
    """Scrape with deadline sort + 48h cutoff -> clear & push to gojep_tenders_current."""
    from modules.tenders.get_current_tenders import run_current_tenders_extraction

    db = SupabaseClient()
    trigger_sync(args)
    records = run_current_tenders_extraction()

    if not records:
        print("No actionable tenders found within the 48h horizon.")
        return False

    print(f"Found {len(records)} actionable tenders.")

    # Clear the current table
    print(f"Clearing {settings.SUPABASE_TABLE_TENDERS_CURRENT} ...")
    try:
        db.supabase.table(settings.SUPABASE_TABLE_TENDERS_CURRENT).delete().neq("resource_id", "0").execute()
    except Exception as e:
        logger.warning(f"Could not clear table: {e}")

    # Push fresh listings — upsert to ALL first (satisfies FK), then CURRENT and CONTRACT_ANALYSIS
    pushed = 0
    for i, raw in enumerate(records):
        row = listing_json_row_to_tender(raw)
        if not row:
            continue
        # Ensure the record exists in gojep_tenders_all before inserting into current
        db.insert_tenders_batch([row], table_name=settings.SUPABASE_TABLE_TENDERS_ALL)
        result = db.insert_tenders_batch([row], table_name=settings.SUPABASE_TABLE_TENDERS_CURRENT)
        if result.get("success", 0) > 0:
            pushed += 1
        else:
            logger.error(f"[{i+1}] Failed to insert {row.get('resource_id')} into {settings.SUPABASE_TABLE_TENDERS_CURRENT}")

        # Insert into contract_analysis — ignore if already exists so we never overwrite
        # enriched fields (competition_unique_id, analysis_timestamp, detail data)
        try:
            db.supabase.table(settings.SUPABASE_TABLE_CONTRACT_ANALYSIS)\
                .upsert(db._prepare_tender_data(row), on_conflict="resource_id", ignore_duplicates=True)\
                .execute()
        except Exception as e:
            logger.warning(f"[{i+1}] contract_analysis insert failed for {row.get('resource_id')}: {e}")

    print(f"Done: {pushed}/{len(records)} listings -> {settings.SUPABASE_TABLE_TENDERS_CURRENT}")
    print(f"      (also upserted into {settings.SUPABASE_TABLE_CONTRACT_ANALYSIS})")

    # Mark already-analysed tenders as detail_page_extracted=true in gojep_tenders_current
    # so get-tender-details, get-tender-documents, and extract-document-text skip them.
    # A tender is confirmed processed only if BOTH resource_id AND competition_unique_id
    # match an existing entry in gojep_analysis_results.
    try:
        analysed_rows = db.supabase.table(settings.SUPABASE_TABLE_ANALYSIS_RESULTS)\
            .select("resource_id,competition_unique_id")\
            .execute().data or []

        # Build lookup: resource_id -> competition_unique_id for all analysed tenders
        analysed = {
            r["resource_id"]: r["competition_unique_id"]
            for r in analysed_rows
            if r.get("resource_id") and r.get("competition_unique_id")
        }

        # Fetch current tenders to cross-check both keys
        current_rows = db.supabase.table(settings.SUPABASE_TABLE_TENDERS_CURRENT)\
            .select("resource_id,competition_unique_id")\
            .execute().data or []

        skipped = 0
        for row in current_rows:
            rid = row.get("resource_id")
            uid = row.get("competition_unique_id")
            if not rid or not uid:
                continue
            # Both resource_id and competition_unique_id must match
            if analysed.get(rid) == uid:
                try:
                    db.supabase.table(settings.SUPABASE_TABLE_TENDERS_CURRENT)\
                        .update({"detail_page_extracted": True, "previously_analysed": True})\
                        .eq("resource_id", rid)\
                        .execute()
                    skipped += 1
                except Exception as e:
                    logger.warning(f"Could not mark {rid} as extracted: {e}")

        new_count = len(current_rows) - skipped
        print(f"      ({skipped} previously analysed — will skip | {new_count} new — will process)")
    except Exception as e:
        logger.warning(f"Could not mark already-analysed tenders: {e}")

    return pushed > 0


# ── 3. get-tender-details --------------------------------------------------

def run_get_tender_details(args: argparse.Namespace) -> bool:
    """Fetch detail pages for DB records that don't have them yet.
    Uses browser with login + CAPTCHA handling."""
    from modules.tenders.get_tender_details import TenderDetailExtractor
    from db.tender_row_mapping import fields_to_tender_patch

    db = SupabaseClient()
    table = getattr(args, "table", settings.SUPABASE_TABLE_TENDERS_ALL)
    limit = getattr(args, "limit", 100)

    # Query records where detail_page_extracted = false
    trigger_sync(args)
    records = db.get_tenders_without_details(limit=limit, table_name=table)
    if not records:
        print("All records already have details extracted.")
        return True

    print(f"Found {len(records)} records without detail pages in {table}.")
    extractor = TenderDetailExtractor()
    updated = 0

    for i, rec in enumerate(records):
        rid = rec.get("resource_id", "?")
        url = rec.get("detail_url")
        if not url:
            print(f"  [{i+1}/{len(records)}] {rid}: No detail_url, skipping.")
            continue

        print(f"  [{i+1}/{len(records)}] Fetching details for {rid} ...")
        try:
            detail = extractor._extract_one(url)
            if detail and detail.get("fields"):
                patch = fields_to_tender_patch(detail["fields"], detail)
                db.update_tender_details(rid, patch, table_name=table)
                # Sync enriched detail fields to gojep_contract_analysis
                try:
                    db.supabase.table(settings.SUPABASE_TABLE_CONTRACT_ANALYSIS)\
                        .update({k: v for k, v in patch.items() if v is not None})\
                        .eq("resource_id", rid)\
                        .execute()
                except Exception as ce:
                    logger.warning(f"  contract_analysis detail sync failed for {rid}: {ce}")
                updated += 1
        except Exception as e:
            logger.error(f"  Detail extraction failed for {rid}: {e}")

    print(f"Done: {updated}/{len(records)} detail pages extracted -> {table}")
    return updated > 0


# ── 4. get-tender-documents ------------------------------------------------

def run_get_tender_documents(args: argparse.Namespace) -> bool:
    """Download ZIP documents for tenders in gojep_tenders_current."""
    import json
    import tempfile
    import os
    from modules.tenders.get_tender_documents import run_downloads

    json_path = getattr(args, "json_file", None)

    if not json_path:
        trigger_sync(args)
        db = SupabaseClient()
        rows = db.supabase.table(settings.SUPABASE_TABLE_TENDERS_CURRENT)\
            .select("resource_id, detail_url, competition_unique_id")\
            .eq("detail_page_extracted", True)\
            .execute().data or []

        if not rows:
            print("No tenders with details found in gojep_tenders_current.")
            return False

        # Exclude tenders already confirmed in gojep_analysis_results
        try:
            analysed_rows = db.supabase.table(settings.SUPABASE_TABLE_ANALYSIS_RESULTS)\
                .select("resource_id,competition_unique_id")\
                .execute().data or []
            analysed = {
                r["resource_id"]: r["competition_unique_id"]
                for r in analysed_rows
                if r.get("resource_id") and r.get("competition_unique_id")
            }
        except Exception as e:
            logger.warning(f"Could not fetch analysed tenders — will download all: {e}")
            analysed = {}

        unanalysed = [
            r for r in rows
            if r.get("resource_id") and analysed.get(r["resource_id"]) != r.get("competition_unique_id")
        ]
        skipped = len(rows) - len(unanalysed)
        if skipped:
            print(f"  Skipping {skipped} already-analysed tenders.")
        rows = unanalysed

        if not rows:
            print("All tenders with details have already been analysed.")
            return True

        base_url = f"{settings.GOJEP_BASE_URL}/epps/cft/prepareViewCfTWS.do?resourceId="
        records = [
            {
                "title_url": r.get("detail_url") or f"{base_url}{r['resource_id']}",
                "resource_id_from_url": r["resource_id"],
                "fields": {"competition_unique_id": r.get("competition_unique_id")},
            }
            for r in rows
            if r.get("resource_id")
        ]

        print(f"Fetched {len(records)} new tenders for document download.")
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump({"records": records}, tmp, ensure_ascii=False)
        tmp.close()
        json_path = tmp.name

    resume = getattr(args, "resume", False)
    print(f"Downloading documents from: {json_path} (resume={resume})")
    payload = run_downloads(json_path, resume=resume)
    ok = payload.get("saved_ok", 0)
    total = payload.get("total_input", 0)
    print(f"Done: {ok}/{total} document ZIPs downloaded.")
    return ok > 0


# ── 5. extract-document-text ----------------------------------------------

def run_extract_document_text(args: argparse.Namespace) -> bool:
    """Extract structured text from all downloaded tender documents."""
    from modules.tenders.extract_documents import run_document_extraction

    trigger_sync(args)
    print("Starting document text extraction...")
    result = run_document_extraction()
    processed = result.get("newly_processed", 0)
    skipped = result.get("skipped", 0)
    errors = result.get("errors", 0)
    total = result.get("total_files_scanned", 0)
    print(f"\nExtraction complete:")
    print(f"  Scanned   : {total}")
    print(f"  Extracted : {processed}")
    print(f"  Skipped   : {skipped} (already done)")
    print(f"  Errors    : {errors}")
    if errors > 0:
        print(f"  WARNING: {errors} file(s) failed extraction — check logs for details.")
    return processed > 0 or errors == 0


# ── Orchestrator -----------------------------------------------------------

def run_pipeline(_args) -> bool:
    """
    Run the full tender scraping pipeline end-to-end:
      1. get-current-tenders  — scrape active listings (48h horizon)
      2. get-tender-details   — fetch detail pages for each listing
      3. get-tender-documents — download ZIP documents for each tender
    Document extraction and LLM analysis are handled separately via
    'colab-pipeline' (tools/colab/extract.py + analyse.py).
    """
    import types

    steps = [
        ("get-current-tenders",  run_get_current_tenders,  types.SimpleNamespace()),
        ("get-tender-details",   run_get_tender_details,   types.SimpleNamespace(
            table=settings.SUPABASE_TABLE_TENDERS_CURRENT, limit=200)),
        ("get-tender-documents", run_get_tender_documents, types.SimpleNamespace(
            json_file=None, resume=True)),
    ]

    for name, fn, step_args in steps:
        print(f"\n{'='*60}")
        print(f"  STEP: {name}")
        print(f"{'='*60}")
        try:
            result = fn(step_args)
            if not result:
                print(f"  {name} returned no results — continuing.")
        except Exception as e:
            logger.error(f"  {name} failed: {e}", exc_info=True)
            print(f"\nPipeline stopped at '{name}'. Fix the error and re-run.")
            return False

    print(f"\n{'='*60}")
    print("  Tender pipeline complete.")
    print(f"  Next step: run 'colab-pipeline' to extract documents and analyse.")
    print(f"{'='*60}\n")
    return True


# ── Parser Registration ----------------------------------------------------

def create_tenders_parser(subparsers) -> None:
    # Orchestrator
    p0 = subparsers.add_parser(
        "run-tenders",
        help="Full tender pipeline: scrape -> details -> download documents",
    )
    p0.set_defaults(func=run_pipeline)

    # 1. get-tenders
    p1 = subparsers.add_parser("get-tenders", help="Step 1: Scrape listings -> historical archive")
    p1.set_defaults(func=run_get_tenders)

    # 2. get-current-tenders
    p2 = subparsers.add_parser("get-current-tenders", help="Step 2: Scrape (sort+48h) -> active dashboard")
    p2.set_defaults(func=run_get_current_tenders)

    # 3. get-tender-details
    p3 = subparsers.add_parser("get-tender-details", help="Step 3: Fetch detail pages for DB records")
    p3.add_argument("--table", default=settings.SUPABASE_TABLE_TENDERS_CURRENT, help="Which table to process")
    p3.add_argument("--limit", type=int, default=100, help="Max records to process")
    p3.set_defaults(func=run_get_tender_details)

    # 4. get-tender-documents
    p4 = subparsers.add_parser("get-tender-documents", help="Step 4: Download ZIP documents")
    p4.add_argument("--json-file", default=None, help="Specific JSON file to use")
    p4.add_argument("--resume", action="store_true", help="Skip tenders whose folder already exists")
    p4.set_defaults(func=run_get_tender_documents)

    # 5. extract-document-text
    p5 = subparsers.add_parser("extract-document-text", help="Step 5: Extract text from downloaded documents")
    p5.set_defaults(func=run_extract_document_text)

    # Add --sync to all
    for p in [p1, p2, p3, p4, p5]:
        p.add_argument("--sync", action="store_true", help="Sync data with Google Drive before processing")


if __name__ == "__main__":
    import argparse as _ap
    parser = _ap.ArgumentParser(description="GOJEP Tender Pipeline")
    parser.add_argument("--analyse", action="store_true", help="Also run Gemma batch analysis after extraction")
    parsed = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    run_pipeline(analyse=parsed.analyse)
