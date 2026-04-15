"""
CLI commands for the Analysis Pipeline.

Commands:
  run-analysis      -> Orchestrator: sync to Drive, extract (Colab), analyse (Colab)
  review-analysis   -> Print formatted summary of all analysed tenders
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import logging

logger = logging.getLogger(__name__)


# ── run-analysis orchestrator -------------------------------------------------

def run_analysis_pipeline(args: argparse.Namespace) -> bool:
    """
    Full analysis pipeline:
      1. Upload new documents to Supabase Storage + dispatch Lightning for Docling extraction
      2. Pull extracted text from Supabase document_extractions table to local extracted_docs/
      3. Run LLM batch analysis via Modal (Qwen2.5-32B on L40S GPU)
    """
    import subprocess
    import sys as _sys

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    skip_extract = getattr(args, "skip_extract", False)
    skip_analyse = getattr(args, "skip_analyse", False)

    # ── Step 1: Local document extraction ─────────────────────────────────────
    if not skip_extract:
        print("\n" + "="*60)
        print("  STEP 1: Extracting documents from local folders...")
        print("="*60)
        from modules.document_processing.extract import run_document_extraction
        result = run_document_extraction()
        print(f"\n  Extraction complete: {result['newly_processed']} processed, {result['skipped']} skipped, {result['errors']} errors")
    else:
        print("  [skip] Document extraction skipped.")

    # ── Step 2: LLM batch analysis via OpenRouter ─────────────────────────────
    if not skip_analyse:
        print("\n" + "="*60)
        print("  STEP 2: Running LLM analysis via OpenRouter (qwen/qwen3-vl-32b-instruct)...")
        print("="*60)
        analyse_script = os.path.join(project_dir, "GPU_providers", "modal", "batch_analyse.py")
        result = subprocess.run(
            [_sys.executable, analyse_script],
            cwd=project_dir,
        )
        if result.returncode != 0:
            print("  WARNING: Analysis returned non-zero exit code.")
        else:
            print("  Analysis complete.")
    else:
        print("  [skip] Modal analysis skipped.")

    print("\n" + "="*60)
    print("  Analysis pipeline complete.")
    print("="*60)
    return True


# ── review-analysis -----------------------------------------------------------

def run_review_analysis(args: argparse.Namespace) -> bool:
    """Print a formatted review of all analysed tenders from local analysis.json sidecars."""
    from config import settings as config

    docs_dir = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "documents")
    pattern = os.path.join(docs_dir, "*", "analysis.json")
    files = sorted(glob.glob(pattern))

    if not files:
        print("No analysis.json files found. Run 'analyse-tenders' first.")
        return False

    # Load all records
    records = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                records.append(json.load(f))
        except Exception as e:
            logger.warning(f"Could not read {path}: {e}")

    # Apply filters
    filter_type = getattr(args, "type", None)
    search = getattr(args, "search", None)
    folder = getattr(args, "folder", None)

    if filter_type:
        records = [r for r in records if (r.get("contract_type") or "").lower() == filter_type.lower()]
    if search:
        kw = search.lower()
        records = [
            r for r in records
            if kw in (r.get("contract_title") or "").lower()
            or kw in (r.get("scope_of_work") or "").lower()
            or kw in (r.get("procuring_entity") or "").lower()
        ]
    if folder:
        records = [r for r in records if folder.lower() in r.get("tender_folder", "").lower()]

    if not records:
        print("No records match the given filters.")
        return False

    detail = getattr(args, "detail", False)
    export_csv = getattr(args, "csv", None)

    if export_csv:
        _export_csv(records, export_csv)
        return True

    if detail and len(records) == 1:
        _print_detail(records[0])
    else:
        _print_summary_table(records, detail=detail)

    return True


def _divider(char: str = "─", width: int = 100) -> str:
    return char * width


def _wrap(text: str, width: int, indent: int = 0) -> list[str]:
    """Word-wrap text to width, returning list of lines with optional indent."""
    if not text:
        return [""]
    pad = " " * indent
    words = text.split()
    lines = []
    current = pad
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current.rstrip())
            current = pad + word + " "
        else:
            current += word + " "
    if current.strip():
        lines.append(current.rstrip())
    return lines or [""]


def _print_summary_table(records: list, detail: bool = False) -> None:
    """Print a compact summary table of all records."""
    print(f"\n{'='*100}")
    print(f"  TENDER ANALYSIS REVIEW  —  {len(records)} tender(s)")
    print(f"{'='*100}\n")

    for i, r in enumerate(records, 1):
        folder       = r.get("tender_folder", "?")
        title        = r.get("contract_title") or r.get("db_title") or "Unknown"
        entity       = r.get("procuring_entity") or r.get("db_procuring_entity") or "?"
        ctype        = r.get("contract_type") or "?"
        value        = r.get("contract_value") or "Not stated"
        duration     = r.get("contract_duration") or "Not stated"
        deadline     = r.get("submission_deadline") or r.get("db_submission_deadline") or "?"
        suitability  = r.get("suitability_summary") or ""
        warnings     = r.get("validation_warnings") or []

        print(f"[{i}] {folder}")
        print(_divider())

        # Title (wrapped)
        title_lines = _wrap(title, 94, indent=4)
        print(f"  Title        : {title_lines[0].strip()}")
        for line in title_lines[1:]:
            print(f"               {line}")

        print(f"  Entity       : {entity}")
        print(f"  Type         : {ctype}")
        print(f"  Value        : {value}")
        print(f"  Duration     : {duration}")
        print(f"  Deadline     : {deadline}")

        if warnings:
            print(f"  ⚠  Warnings  : {', '.join(warnings)}")

        # Suitability summary (wrapped)
        if suitability:
            suit_lines = _wrap(suitability, 90, indent=4)
            print(f"  Suitability  : {suit_lines[0].strip()}")
            for line in suit_lines[1:]:
                print(f"               {line}")

        if detail:
            _print_detail_fields(r)

        print()

    print(_divider("="))
    print(f"  Total: {len(records)} | Types: {_type_summary(records)}")
    print(_divider("="))


def _type_summary(records: list) -> str:
    from collections import Counter
    counts = Counter(r.get("contract_type") or "Unknown" for r in records)
    return "  ".join(f"{k}: {v}" for k, v in sorted(counts.items()))


def _print_detail_fields(r: dict) -> None:
    """Print the expanded detail fields for a single record inline."""

    def _list_field(label: str, items) -> None:
        if not items:
            return
        print(f"\n  {label}:")
        for item in items:
            if isinstance(item, dict):
                event = item.get("event", "")
                date  = item.get("date", "")
                print(f"    • {event}: {date}" if date else f"    • {event}")
            else:
                for line in _wrap(str(item), 90, indent=6):
                    print(f"    •{line}")

    scope = r.get("scope_of_work") or ""
    if scope:
        print(f"\n  Scope of Work:")
        for line in _wrap(scope, 90, indent=4):
            print(f"    {line}")

    _list_field("Eligibility Requirements",  r.get("eligibility_requirements"))
    _list_field("Experience Requirements",   r.get("experience_requirements"))
    _list_field("Financial Requirements",    r.get("financial_requirements"))
    _list_field("Mandatory Documents",       r.get("mandatory_documents"))
    _list_field("Evaluation Criteria",       r.get("evaluation_criteria"))
    _list_field("Key Milestones",            r.get("key_milestones"))

    lots = r.get("lots") or []
    if lots:
        _list_field("Lots", lots)

    special = r.get("special_conditions") or []
    if special:
        _list_field("Special Conditions", special)


def _print_detail(r: dict) -> None:
    """Full detail view for a single record."""
    folder  = r.get("tender_folder", "?")
    print(f"\n{'='*100}")
    print(f"  FULL ANALYSIS — {folder}")
    print(f"{'='*100}")
    _print_summary_table([r], detail=False)
    _print_detail_fields(r)
    print()


def _export_csv(records: list, output_path: str) -> None:
    """Export flat summary to CSV."""
    import csv

    fieldnames = [
        "tender_folder", "contract_title", "procuring_entity", "contract_type",
        "contract_value", "contract_duration", "submission_deadline",
        "scope_of_work", "suitability_summary",
        "eligibility_requirements", "experience_requirements",
        "financial_requirements", "mandatory_documents",
        "evaluation_criteria", "key_milestones", "lots", "special_conditions",
    ]

    def _flatten(val) -> str:
        if val is None:
            return ""
        if isinstance(val, list):
            parts = []
            for item in val:
                if isinstance(item, dict):
                    parts.append("; ".join(f"{k}: {v}" for k, v in item.items()))
                else:
                    parts.append(str(item))
            return " | ".join(parts)
        return str(val)

    try:
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for r in records:
                writer.writerow({field: _flatten(r.get(field)) for field in fieldnames})
        print(f"Exported {len(records)} records to {output_path}")
    except Exception as e:
        print(f"CSV export failed: {e}")


# ── Parser Registration -------------------------------------------------------

def create_analysis_parser(subparsers) -> None:

    # run-analysis (orchestrator)
    p0 = subparsers.add_parser(
        "run-analysis",
        help="Orchestrator: sync to Drive -> extract (Colab) -> analyse (Colab)",
    )
    p0.add_argument("--skip-extract", action="store_true", help="Skip document text extraction")
    p0.add_argument("--skip-analyse", action="store_true", help="Skip Modal LLM analysis")
    p0.set_defaults(func=run_analysis_pipeline)

    # review-analysis
    p1 = subparsers.add_parser(
        "review-analysis",
        help="Review analysed tender results — summary table with optional filters",
    )
    p1.add_argument("--type",   default=None, help="Filter by contract type (Goods, Works, Services, Consultancy)")
    p1.add_argument("--search", default=None, help="Keyword search across title, scope, and entity")
    p1.add_argument("--folder", default=None, help="Filter by tender folder name (partial match)")
    p1.add_argument("--detail", action="store_true", help="Show full field breakdown for each result")
    p1.add_argument("--csv",    default=None, metavar="FILE", help="Export results to a CSV file")
    p1.set_defaults(func=run_review_analysis)
