"""
CLI Subcommand for GOJEP Contract Awards End-to-End Extraction.
"""

import argparse
import logging
import os
import tempfile
import json
from typing import Optional

from config import settings as config
from modules.awards.get_awards import GOJEPAwardsScraper
from modules.awards.get_awards_details import extract_award_pdf, resolve_pdf_path
from db.supabase_client import SupabaseClient
from db.award_row_mapping import merge_awards_and_details
from ops.reconcile_awards import run_reconciliation

logger = logging.getLogger(__name__)

def run_awards_extraction(args: argparse.Namespace) -> bool:
    try:
        max_pages = None if args.max_pages == 0 else args.max_pages
        latest_award_date = None
        db_client = SupabaseClient()
        
        if not args.full_sync:
            latest_award_date = db_client.get_latest_award_date()
            if latest_award_date:
                print(f"\nDelta Extraction Active: Database watermark is {latest_award_date.strftime('%Y-%m-%d %H:%M:%S')}")
            else:
                print("\nNo Database watermark found. Performing full initial extraction.")
        else:
            print("\nFull Sync requested. Ignoring Database watermark.")

        print("\n[1/4] Starting Awards Scraper & Notice Downloads...")
        scraper = GOJEPAwardsScraper()
        awards = scraper.run(
            max_pages=max_pages,
            save_json=True,
            download_pdfs=True,
            resume_pdfs=True,
            latest_award_date=latest_award_date
        )

        if not awards:
            print("\nNo new awards found. Database is completely up to date.")
            return True

        print(f"\n[2/4] Starting local PDF detail extraction for {len(awards)} valid awards...")
        
        # We manually emulate extract_from_awards_json but in-memory so it flows perfectly
        details_payload = {}
        for i, row in enumerate(awards):
            pdf_path = resolve_pdf_path(row, i, config.AWARDS_OUTPUT_DIRECTORY)
            base = {
                "row_number": row.get("row_number"),
                "resource_id": row.get("resource_id"),
                "pdf_resource_id": row.get("pdf_resource_id"),
                "pdf_url": row.get("pdf_url"),
                "title": row.get("title"),
            }
            if not pdf_path:
                details_payload[row.get("resource_id")] = {
                    **base, "pdf_path": None, "extracted": None, "error": "pdf_file_not_found"
                }
            else:
                extracted = extract_award_pdf(pdf_path)
                err = extracted.pop("error", None)
                details_payload[row.get("resource_id")] = {
                    **base, "pdf_path": pdf_path, "extracted": extracted, "error": err
                }
                
        print("\n[3/4] Merging Base Listings and PDF Details...")
        merged_dict = merge_awards_and_details(awards, details_payload)
        merged_list = list(merged_dict.values())
        
        print("\n[4/4] Synchronising perfectly formatted payload to Supabase Database...")
        summary = run_reconciliation(json_data=merged_list)
        
        print("\n--- Awards Sync Complete ---")
        print(f"Total Processed: {summary.get('json_total', 0)}")
        print(f"Newly Added: {summary.get('new_records_found', 0)}")
        print(f"Records Fixed/Updated: {summary.get('existing_updated', 0)}")
        return True

    except Exception as e:
        print(f"\nError during awards extraction process: {str(e)}")
        logger.error("Extraction error: %s", e, exc_info=True)
        return False

def create_awards_extraction_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("extract-awards", help="Run GOJEP Awards data extraction & database sync (End-to-End)")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum number of pagination pages to scrape for awards (0 = no limit, rely on Delta watermark)",
    )
    parser.add_argument(
        "--full-sync",
        action="store_true",
        help="Force completely disregard the database watermark and run over all previous pages.",
    )
    parser.set_defaults(func=run_awards_extraction)
