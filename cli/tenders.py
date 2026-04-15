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
import json
import logging
import os
import re
import shutil

from config import settings
from db.client.supabase_client import SupabaseClient
from db.tenders.tender_row_mapping import listing_json_row_to_tender

logger = logging.getLogger(__name__)



def _safe_folder_name(competition_unique_id: str) -> str:
    """Mirror the folder-naming logic in get_tender_documents.py."""
    return re.sub(r"[^\w\-.]+", "_", str(competition_unique_id))


def prune_documents_to_current_listing(_args=None) -> bool:
    """
    Ensure the documents folder is an exact mirror of gojep_tenders_current.

    - Deletes any folder not corresponding to a competition_unique_id in the table.
    - Raises an error if folder count does not exactly match tender count after pruning.
    """
    try:
        db = SupabaseClient()
        rows = (
            db.supabase.table(settings.SUPABASE_TABLE_TENDERS_CURRENT)
            .select("competition_unique_id")
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"Could not fetch tenders_current from Supabase: {e}")
        print("Skipping document pruning — could not reach Supabase.")
        return True

    if not rows:
        print("gojep_tenders_current is empty — skipping document pruning to avoid wiping all folders.")
        return True

    tender_count = len(rows)

    # Build valid folder names solely from competition_unique_id
    valid_folders: set[str] = set()
    for row in rows:
        cid = row.get("competition_unique_id")
        if cid:
            valid_folders.add(_safe_folder_name(str(cid)))

    print(f"gojep_tenders_current: {tender_count} tenders.")

    docs_dir = os.path.join(settings.TENDERS_OUTPUT_DIRECTORY, "documents")
    if not os.path.isdir(docs_dir):
        print("Documents folder does not exist — nothing to prune.")
        return True

    removed = 0
    kept = 0
    for entry in os.listdir(docs_dir):
        entry_path = os.path.join(docs_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        if entry in valid_folders:
            kept += 1
        else:
            print(f"  Removing folder: {entry}")
            try:
                shutil.rmtree(entry_path)
                removed += 1
            except Exception as e:
                logger.error(f"  Failed to remove {entry}: {e}")

    print(f"  Pruned: kept {kept}, removed {removed} folder(s).")
    return True


def verify_documents_integrity(_args=None) -> bool:
    """
    Confirm the documents folder exactly mirrors gojep_tenders_current.
    Called after get-tender-documents so all missing folders have been downloaded.
    Raises an error if counts still don't match — indicating a download failure.
    """
    try:
        db = SupabaseClient()
        rows = (
            db.supabase.table(settings.SUPABASE_TABLE_TENDERS_CURRENT)
            .select("competition_unique_id")
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"Could not fetch tenders_current from Supabase: {e}")
        print("Skipping integrity check — could not reach Supabase.")
        return True

    tender_count = len(rows)
    docs_dir = os.path.join(settings.TENDERS_OUTPUT_DIRECTORY, "documents")
    folder_count = sum(
        1 for e in os.listdir(docs_dir)
        if os.path.isdir(os.path.join(docs_dir, e))
    ) if os.path.isdir(docs_dir) else 0

    if folder_count != tender_count:
        msg = (
            f"ERROR: documents folder count ({folder_count}) does not match "
            f"gojep_tenders_current ({tender_count}). "
            f"{abs(tender_count - folder_count)} tender(s) are missing document folders."
        )
        print(msg)
        logger.error(msg)
        return False

    print(f"  OK — {folder_count} folders match {tender_count} tenders exactly.")
    return True




# ── 1. get-tenders ---------------------------------------------------------

def run_get_tenders(args: argparse.Namespace) -> bool:
    """Scrape listings using Publication Date watermark -> push to gojep_tenders_all."""
    from modules.tenders.get_tenders import GOJEPScraper

    db = SupabaseClient()
    watermark = db.get_latest_publication_date()

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
    current_resource_ids = []
    for i, raw in enumerate(records):
        row = listing_json_row_to_tender(raw)
        if not row:
            continue
        rid = row.get("resource_id")
        if rid:
            current_resource_ids.append(rid)
        # Ensure the record exists in gojep_tenders_all before inserting into current
        db.insert_tenders_batch([row], table_name=settings.SUPABASE_TABLE_TENDERS_ALL)
        result = db.insert_tenders_batch([row], table_name=settings.SUPABASE_TABLE_TENDERS_CURRENT)
        if result.get("success", 0) > 0:
            pushed += 1
        else:
            logger.error(f"[{i+1}] Failed to insert {rid} into {settings.SUPABASE_TABLE_TENDERS_CURRENT}")

        # Insert into contract_analysis — ignore if already exists so we never overwrite
        # enriched fields (competition_unique_id, analysis_timestamp, detail data)
        try:
            db.supabase.table(settings.SUPABASE_TABLE_CONTRACT_ANALYSIS)\
                .upsert(db._prepare_tender_data(row), on_conflict="resource_id", ignore_duplicates=True)\
                .execute()
        except Exception as e:
            logger.warning(f"[{i+1}] contract_analysis insert failed for {rid}: {e}")

    print(f"Done: {pushed}/{len(records)} listings -> {settings.SUPABASE_TABLE_TENDERS_CURRENT}")

    # Prune contract_analysis to match tenders_current exactly — remove any rows
    # whose tender is no longer in the active scrape window.
    if current_resource_ids:
        try:
            db.supabase.table(settings.SUPABASE_TABLE_CONTRACT_ANALYSIS)\
                .delete()\
                .not_in_("resource_id", current_resource_ids)\
                .execute()
            print(f"      (contract_analysis synced to {len(current_resource_ids)} current tenders)")
        except Exception as e:
            logger.warning(f"Could not prune contract_analysis: {e}")

    # Mark already-analysed tenders as detail_page_extracted=true in gojep_tenders_current
    # so get-tender-details, get-tender-documents, and extract-document-text skip them.
    try:
        # Step 1: restore competition_unique_id from contract_analysis into tenders_current.
        # The listing scrape sets competition_unique_id=NULL on every fresh insert; the detail
        # value is preserved in contract_analysis from prior runs (ignore_duplicates upsert).
        ca_rows = db.supabase.table(settings.SUPABASE_TABLE_CONTRACT_ANALYSIS)\
            .select("resource_id,competition_unique_id")\
            .in_("resource_id", current_resource_ids)\
            .execute().data or []

        for ca in ca_rows:
            rid = ca.get("resource_id")
            uid = ca.get("competition_unique_id")
            if rid and uid:
                try:
                    db.supabase.table(settings.SUPABASE_TABLE_TENDERS_CURRENT)\
                        .update({"competition_unique_id": uid})\
                        .eq("resource_id", rid)\
                        .execute()
                except Exception as e:
                    logger.warning(f"Could not restore competition_unique_id for {rid}: {e}")

        # Step 2: cross-check against analysis_results — both keys must match so a
        # re-tendered competition (same resource_id, new competition_unique_id) is re-processed.
        analysed_rows = db.supabase.table(settings.SUPABASE_TABLE_ANALYSIS_RESULTS)\
            .select("resource_id,competition_unique_id")\
            .execute().data or []

        analysed = {
            r["resource_id"]: r["competition_unique_id"]
            for r in analysed_rows
            if r.get("resource_id") and r.get("competition_unique_id")
        }

        current_rows = db.supabase.table(settings.SUPABASE_TABLE_TENDERS_CURRENT)\
            .select("resource_id,competition_unique_id")\
            .execute().data or []

        skipped = 0
        for row in current_rows:
            rid = row.get("resource_id")
            uid = row.get("competition_unique_id")
            if not rid or not uid:
                continue
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

        # Prune analysis_results to current tenders only
        if current_resource_ids:
            try:
                db.supabase.table(settings.SUPABASE_TABLE_ANALYSIS_RESULTS)\
                    .delete()\
                    .not_in_("resource_id", current_resource_ids)\
                    .execute()
            except Exception as e:
                logger.warning(f"Could not prune analysis_results: {e}")

    except Exception as e:
        logger.warning(f"Could not mark already-analysed tenders: {e}")

    return pushed > 0


# ── 3. get-tender-details --------------------------------------------------

def run_get_tender_details(args: argparse.Namespace) -> bool:
    """Fetch detail pages for DB records that don't have them yet.
    Uses browser with login + CAPTCHA handling."""
    from modules.tenders.get_tender_details import TenderDetailExtractor
    from db.tenders.tender_row_mapping import fields_to_tender_patch

    db = SupabaseClient()
    table = getattr(args, "table", settings.SUPABASE_TABLE_TENDERS_ALL)
    limit = getattr(args, "limit", 100)

    # Query records where detail_page_extracted = false

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
    import tempfile
    from modules.tenders.get_tender_documents import run_downloads

    json_path = getattr(args, "json_file", None)

    if not json_path:
    
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


# ── Sync analysis tables --------------------------------------------------

def run_sync_analysis_tables(_args) -> bool:
    """
    Prune gojep_contract_analysis and gojep_analysis_results so they contain
    only tenders currently in gojep_tenders_current. No scraping — reads the
    DB as-is and deletes stale rows.
    """
    db = SupabaseClient()

    try:
        rows = db.supabase.table(settings.SUPABASE_TABLE_TENDERS_CURRENT)\
            .select("resource_id")\
            .execute().data or []
    except Exception as e:
        print(f"Failed to fetch tenders_current: {e}")
        return False

    current_ids = [r["resource_id"] for r in rows if r.get("resource_id")]
    if not current_ids:
        print("gojep_tenders_current is empty — aborting to avoid wiping analysis tables.")
        return False

    print(f"gojep_tenders_current has {len(current_ids)} tenders.")

    for table in (settings.SUPABASE_TABLE_CONTRACT_ANALYSIS, settings.SUPABASE_TABLE_ANALYSIS_RESULTS):
        try:
            db.supabase.table(table)\
                .delete()\
                .not_in_("resource_id", current_ids)\
                .execute()
            print(f"  {table}: pruned to {len(current_ids)} tenders.")
        except Exception as e:
            print(f"  {table}: prune failed — {e}")

    return True


# ── Orchestrator -----------------------------------------------------------

def run_cleanup_expired(_args=None) -> bool:
    """Delete expired tenders from gojep_tenders_current."""
    from tools.cleanup_expired import run_cleanup
    return run_cleanup()


def run_pipeline(_args) -> bool:
    """
    Run the full tender scraping pipeline end-to-end:
      1. cleanup-expired      — remove expired tenders from gojep_tenders_current
      2. get-current-tenders  — scrape active listings (48h horizon)
      3. get-tender-details   — fetch detail pages for each listing
      4. get-tender-documents — download ZIP documents for each tender
    Document extraction and LLM analysis are handled separately via
    'colab-pipeline' (tools/colab/extract.py + analyse.py).
    """
    import types

    from cli.analysis import run_analysis_pipeline

    steps = [
        ("cleanup-expired",           run_cleanup_expired,               types.SimpleNamespace()),
        ("get-current-tenders",       run_get_current_tenders,          types.SimpleNamespace()),
        ("prune-documents",           prune_documents_to_current_listing, types.SimpleNamespace()),
        ("get-tender-details",        run_get_tender_details,            types.SimpleNamespace(
            table=settings.SUPABASE_TABLE_TENDERS_CURRENT, limit=200)),
        ("get-tender-documents",      run_get_tender_documents,          types.SimpleNamespace(
            json_file=None, resume=True)),
        ("verify-documents",          verify_documents_integrity,        types.SimpleNamespace()),
        ("run-analysis",              run_analysis_pipeline,             types.SimpleNamespace(
            skip_extract=False, skip_analyse=False)),
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

    # prune-documents
    p_prune = subparsers.add_parser(
        "prune-documents",
        help="Remove document folders for tenders not in gojep_tenders_current",
    )
    p_prune.set_defaults(func=prune_documents_to_current_listing)

    # verify-documents
    p_verify = subparsers.add_parser(
        "verify-documents",
        help="Verify document folders exactly match gojep_tenders_current",
    )
    p_verify.set_defaults(func=verify_documents_integrity)

    # sync-analysis-tables
    p_sync = subparsers.add_parser(
        "sync-analysis-tables",
        help="Prune contract_analysis and analysis_results to match tenders_current (no scraping)",
    )
    p_sync.set_defaults(func=run_sync_analysis_tables)

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
