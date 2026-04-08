"""
Master CLI entry point.

Commands are registered from three modules:
  cli/tenders.py  — Steps 1-5: tender scraping & document extraction
  cli/awards.py   — Awards extraction (end-to-end)
  cli/analysis.py — Step 6+: LLM analysis
"""
import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="GOJEP Tender Data Platform")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    try:
        from cli.tenders import create_tenders_parser
        create_tenders_parser(subparsers)
    except ImportError as e:
        print(f"Warning: Could not load tenders module: {e}")

    try:
        from cli.awards import create_awards_parser
        create_awards_parser(subparsers)
    except ImportError as e:
        print(f"Warning: Could not load awards module: {e}")

    try:
        from cli.analysis import create_analysis_parser
        create_analysis_parser(subparsers)
    except ImportError as e:
        print(f"Warning: Could not load analysis module: {e}")

    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        print("\nTender pipeline:")
        print("  run-tenders              Orchestrator: scrape -> details -> download (steps 1-4)")
        print("  get-tenders              Step 1: Scrape listings -> gojep_tenders_all")
        print("  get-current-tenders      Step 2: Scrape (48h horizon) -> gojep_tenders_current")
        print("  get-tender-details       Step 3: Fetch detail pages for DB records")
        print("  get-tender-documents     Step 4: Download ZIP documents")
        print("  extract-document-text    Step 5: Extract text (local, non-PDF only)")
        print("  run-analysis             Step 6: Sync to Drive + extract (Colab) + analyse (Colab)")
        print("\nAwards pipeline:")
        print("  extract-awards           End-to-end awards extraction & DB sync")
        sys.exit(1)

    try:
        success = args.func(args)
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\nError during operation: {str(e)}")
        logger.error("Main orchestrator error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
