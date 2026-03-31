"""
CLI: tender extraction (scrape GOJEP → Supabase).
"""
import argparse
import logging
import sys

from config import settings
from modules.tenders.get_tenders import GOJEPScraper

logger = logging.getLogger(__name__)


def run_extraction(args):
    if not settings.OPENROUTER_API_KEY:
        print("Error: OPENROUTER_API_KEY is required for captcha solving.")
        print("Please set it in your .env file or environment variables.")
        print("You can get an API key from: https://openrouter.ai/")
        return False

    captcha_model = settings.CAPTCHA_MODEL
    if captcha_model not in settings.OPENROUTER_MODELS:
        print(f"Error: Invalid captcha model '{captcha_model}'. Using key 'qwen_35_9b'.")
        captcha_model = "qwen_35_9b"
    captcha_model_identifier = settings.OPENROUTER_MODELS.get(captcha_model, captcha_model)

    if args.headless:
        settings.HEADLESS_MODE = True
    settings.OUTPUT_FORMAT = args.output_format
    settings.TENDERS_OUTPUT_DIRECTORY = args.output_dir
    settings.OUTPUT_DIRECTORY = args.output_dir
    settings.LOG_LEVEL = args.log_level

    auto_reconcile = args.auto_reconcile and not args.no_auto_reconcile
    settings.AUTO_RECONCILE = auto_reconcile

    try:
        from db.supabase_client import SupabaseClient
        try:
            db_client = SupabaseClient()
            latest_pub_date = db_client.get_latest_publication_date()
            active_ids = db_client.get_active_tender_ids()
        except getattr(Exception, "suppress", Exception) as e:
            logger.warning(f"Could not connect to Supabase to fetch watermark: {e}")
            latest_pub_date = None
            active_ids = set()
            
        print("Starting GOJEP Tender Extraction...")
        print("Configuration:")
        print(f"  - Model: {captcha_model} ({captcha_model_identifier})")
        print(f"  - Output Format: {settings.OUTPUT_FORMAT}")
        print(f"  - Output Directory: {settings.TENDERS_OUTPUT_DIRECTORY}")
        print(f"  - Headless Mode: {settings.HEADLESS_MODE}")
        print(f"  - Log Level: {settings.LOG_LEVEL}")
        print(f"  - Max Pages: {'All' if args.max_pages == 0 else args.max_pages}")
        print(f"  - Auto Reconciliation: {'Enabled' if settings.AUTO_RECONCILE else 'Disabled'}")
        print(f"  - Latest DB Publication Date: {latest_pub_date if latest_pub_date else 'None'}")
        print(f"  - Known Active Database IDs: {len(active_ids)}")
        print()

        scraper = GOJEPScraper()
        max_pages = None if args.max_pages == 0 else args.max_pages
        
        output_path = scraper.run_extraction(
            latest_publication_date=latest_pub_date,
            known_active_ids=active_ids,
            max_pages=max_pages
        )

        if output_path:
            print(f"\nExtraction completed successfully. Data saved to {output_path}")
            
            if settings.AUTO_RECONCILE:
                try:
                    from modules.tenders.get_tender_details import TenderDetailExtractor
                    from db.tender_row_mapping import (
                        load_listings_array,
                        load_tender_details_payload,
                        merge_listings_and_details,
                    )
                    from ops.reconcile_tenders import run_reconciliation

                    print("\nStarting automatic Tender Details Extraction...")
                    details_extractor = TenderDetailExtractor()
                    details_output_path = details_extractor.extract_from_tenders_json(output_path)
                    
                    if details_output_path:
                        print(f"Details extraction completed. Data saved to {details_output_path}")
                        
                        from modules.tenders.get_tender_documents import run_downloads
                        try:
                            print("\nStarting automatic Tender Documents Download...")
                            doc_payload = run_downloads(
                                details_output_path,
                                resume=True,
                                download_timeout=120
                            )
                            print(f"Document downloads completed: {doc_payload.get('saved_ok', 0)} saved, {doc_payload.get('failed', 0)} failed out of {doc_payload.get('total_input', 0)}.")
                        except Exception as e:
                            logger.error("Auto document extraction failed: %s", e, exc_info=True)
                            print(f"\nWarning: Automatic document download failed: {e}")
                            
                        print("\nMerging listings and details for database sync...")
                        
                        # Load data
                        listings = load_listings_array(output_path)
                        details = load_tender_details_payload(details_output_path)
                        
                        # Merge
                        merged_dict_by_id = merge_listings_and_details(listings, details)
                        
                        # Flatten back to list
                        merged_list = list(merged_dict_by_id.values())
                        
                        print(f"Successfully merged {len(merged_list)} records. Starting Database Reconciliation...")
                        
                        summary = run_reconciliation(json_data=merged_list)
                        
                        print("\n" + "=" * 60)
                        print("RECONCILIATION SUMMARY:")
                        print(f"   - Total merged records processed: {summary['json_total']:,}")
                        print(f"   - Database before: {summary['database_total']:,}")
                        print(f"   - Database after: {summary.get('database_total_after', summary['database_total']):,}")
                        print(f"   - Success rate: {summary.get('success_rate_after', summary.get('success_rate_before', 0)):.1f}%")
                        if summary.get("cleaning_stats", {}).get("successful_insertions", 0) > 0:
                            print(f"   - Records fixed and inserted: {summary['cleaning_stats']['successful_insertions']:,}")
                        print("=" * 60)
                except Exception as e:
                    logger.error("Auto details extraction / reconciliation failed: %s", e, exc_info=True)
                    print(f"\nWarning: Automatic details extraction or reconciliation failed: {e}")
                    
            return True
            
        print("\nExtraction completed but no new data was found.")
        return False

    except KeyboardInterrupt:
        print("\nExtraction cancelled by user.")
        return False
    except Exception as e:
        print(f"\nError during extraction: {str(e)}")
        logger.error("Extraction execution error: %s", e, exc_info=True)
        return False


def create_tenders_extraction_parser(subparsers=None):
    if subparsers:
        parser = subparsers.add_parser("extract-tenders", help="Run data extraction for GOJEP Tenders")
    else:
        parser = argparse.ArgumentParser(description="GOJEP Tender Data Extraction")

    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument(
        "--output-format",
        choices=["json"],
        default="json",
        help="Output format for extracted data (CSV/Excel exports disabled)",
    )
    parser.add_argument(
        "--output-dir",
        default=settings.TENDERS_OUTPUT_DIRECTORY,
        help="Directory to save extracted data",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Maximum number of pages to scrape (default: 1, use 0 for all pages)",
    )
    parser.add_argument(
        "--auto-reconcile",
        action="store_true",
        default=True,
        help="Automatically run data reconciliation after extraction (default: True)",
    )
    parser.add_argument(
        "--no-auto-reconcile",
        action="store_true",
        help="Disable automatic data reconciliation after extraction",
    )
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Only run data reconciliation (no extraction)",
    )
    parser.add_argument(
        "--reconcile-file",
        type=str,
        help="Specific JSON file to use for reconciliation (optional)",
    )

    if subparsers:
        parser.set_defaults(func=lambda args: run_extraction_cli(args))

    return parser


def run_extraction_cli(args):
    if getattr(args, "reconcile_only", False):
        try:
            print("Running data reconciliation only...")
            from ops.reconcile_tenders import run_reconciliation

            reconcile_file = args.reconcile_file if args.reconcile_file else None
            summary = run_reconciliation(reconcile_file)

            print("\n" + "=" * 60)
            print("RECONCILIATION SUMMARY:")
            print(f"   - JSON total: {summary['json_total']:,}")
            print(f"   - Database before: {summary['database_total']:,}")
            print(f"   - Database after: {summary.get('database_total_after', summary['database_total']):,}")
            print(f"   - Success rate: {summary.get('success_rate_after', summary['success_rate_before']):.1f}%")
            if summary.get("cleaning_stats", {}).get("successful_insertions", 0) > 0:
                print(f"   - Records fixed and inserted: {summary['cleaning_stats']['successful_insertions']:,}")
            print("=" * 60)

            if summary.get("success_rate_after", summary["success_rate_before"]) >= 99.0:
                print("Reconciliation achieved excellent results.")
            else:
                print("Reconciliation completed.")

            return True

        except Exception as e:
            print(f"Error during reconciliation: {str(e)}")
            return False

    return run_extraction(args)


def main():
    parser = create_tenders_extraction_parser()
    args = parser.parse_args()
    success = run_extraction_cli(args)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
