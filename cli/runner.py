"""
Master CLI: `extract` and `analyze` subcommands.
"""
import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="GOJEP Tender Data Platform")
    subparsers = parser.add_subparsers(dest="command", help="Available operations")

    try:
        from cli.extract_tenders import create_tenders_extraction_parser

        create_tenders_extraction_parser(subparsers)
    except ImportError as e:
        print(f"Warning: Could not load tenders extraction module: {e}")

    try:
        from cli.extract_awards import create_awards_extraction_parser

        create_awards_extraction_parser(subparsers)
    except ImportError as e:
        print(f"Warning: Could not load awards extraction module: {e}")

    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        print("\nAvailable commands:")
        print("  extract-tenders   - Run data extraction operations for Tenders")
        print("  extract-awards    - Run data extraction operations for Awards")
        print("\nExamples:")
        print("  python main.py extract-tenders --max-pages 0        # Extract all tender pages")
        print("  python main.py extract-awards                       # Extract un-synced awards")
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
