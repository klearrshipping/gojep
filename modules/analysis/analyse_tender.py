"""
Tender analysis orchestrator.

Pipeline per tender folder:
  1. Fetch structured metadata from gojep_tenders_current (fallback: gojep_tenders_all)
  2. Collect extracted document text from json_documents/*.json
  3. Split documents into chunks of ≤27,500 tokens at file boundaries
  4. Send each chunk to OpenRouter LLM; merge results
  5. Validate the response has required fields
  6. Save result to Supabase (gojep_analysis_results) + local analysis.json sidecar

Folder structure:
  <tender_id>/
      original_documents/           <- source files (nested)
      extracted_documents/          <- flat synced files (not used by analysis)
      json_documents/               <- extracted JSONs + .manifest.json
      analysis.json                  <- analysis sidecar

Failure handling:
  - On API error or parse failure: write .analysis_failed marker, skip on future runs
  - On rate limit or transient error: exponential backoff up to MAX_RETRIES
  - --reanalyse flag: delete existing sidecar + marker, force re-analysis
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import settings as config
from modules.analysis.prompt import (
    ANALYSIS_OUTPUT_FIELDS,
    LIST_FIELDS,
    RATE_LIMIT_DELAY,
    MAX_RETRIES,
)
from modules.analysis.fetch import _fetch_db_metadata, DB_META_FIELDS
from modules.analysis.chunk import build_chunk_contexts
from modules.analysis.call import _call_llm
from modules.analysis.parse import _parse_llm_response, _validate_parsed, _merge_parsed_results, _consolidate

logger = logging.getLogger(__name__)


# ── Failure marker helpers ─────────────────────────────────────────────────────

def _write_failure_marker(path: str, reason: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"error": reason, "timestamp": datetime.now(timezone.utc).isoformat()}, f)
    except Exception:
        pass


def _read_failure_marker(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("error", "unknown")
    except Exception:
        return "unknown"


def _resource_id_from_folder(folder_name: str) -> str:
    """
    Best-effort resource_id from folder name.
    Returns the full folder name to avoid collisions between folders sharing
    the same first segment (e.g. 1000_972, 1000_973).
    The actual DB resource_id is populated from fetched metadata when available.
    """
    return folder_name


# ── Per-folder analysis ────────────────────────────────────────────────────────

def analyse_tender_folder(tender_folder: str, db=None, reanalyse: bool = False) -> bool:
    """
    Analyse a single tender folder. Returns True if a new analysis was saved.

    reanalyse=True: delete existing sidecar and failure marker, force re-run.
    """
    folder_name = os.path.basename(tender_folder)
    resource_id = _resource_id_from_folder(folder_name)
    sidecar_path = os.path.join(tender_folder, "analysis.json")
    failed_marker_path = os.path.join(tender_folder, ".analysis_failed")

    if reanalyse:
        for p in [sidecar_path, failed_marker_path]:
            if os.path.exists(p):
                os.unlink(p)

    if os.path.exists(sidecar_path):
        logger.debug(f"Already analysed, skipping: {folder_name}")
        return False

    if os.path.exists(failed_marker_path):
        reason = _read_failure_marker(failed_marker_path)
        logger.debug(f"Previously failed ({reason}), skipping: {folder_name}")
        return False

    # 1. Fetch DB metadata
    db_meta = None
    if db:
        db_meta = _fetch_db_metadata(folder_name, db, tender_folder=tender_folder)

    # 2. Build chunk contexts
    chunk_contexts = build_chunk_contexts(tender_folder, db_meta)
    if not chunk_contexts:
        logger.warning(f"No context available for {folder_name}, skipping.")
        return False

    num_chunks = len(chunk_contexts)
    all_source_files: List[str] = []
    for _, src in chunk_contexts:
        for f in src:
            if f not in all_source_files:
                all_source_files.append(f)
    total_tokens = sum(len(ctx) // 4 for ctx, _ in chunk_contexts)
    unique_source_files = {re.sub(r" \[part \d+/\d+\]$", "", f) for f in all_source_files}

    print(
        f"  -> Analysing {folder_name} "
        f"({'with' if db_meta else 'without'} DB metadata, "
        f"~{total_tokens:,} tokens, {len(unique_source_files)} source file(s), "
        f"{num_chunks} chunk(s), route=OpenRouter)...",
        flush=True,
    )

    # 3. Call LLM per chunk
    parsed_results: List[Dict[str, Any]] = []
    raw_responses: List[str] = []
    for i, (context, _) in enumerate(chunk_contexts, start=1):
        if num_chunks > 1:
            print(f"  -> Chunk {i}/{num_chunks} (~{len(context) // 4:,} tokens)...", flush=True)
        try:
            raw_response = _call_llm(context)
        except Exception as e:
            msg = str(e)
            logger.error(f"LLM API call failed for {folder_name} chunk {i}/{num_chunks}: {msg}")
            print(f"  -> ERROR (API, chunk {i}): {msg}", flush=True)
            _write_failure_marker(failed_marker_path, f"API error chunk {i}: {msg}")
            return False
        raw_responses.append(raw_response)
        parsed_results.append(_parse_llm_response(raw_response))

    # 4. Merge chunks + validate
    parsed = _merge_parsed_results(parsed_results) if num_chunks > 1 else parsed_results[0]
    warnings = _validate_parsed(parsed)
    for w in warnings:
        print(f"  -> WARN: {w}", flush=True)
        logger.warning(f"{folder_name}: {w}")

    # 5. Consolidate + generate narrative
    print(f"  -> Consolidating and generating narrative...", flush=True)
    parsed, narrative_analysis = _consolidate(parsed, db_meta=db_meta)

    if "parse_error" in parsed and len(parsed) == 1:
        msg = parsed["parse_error"]
        _write_failure_marker(failed_marker_path, f"Parse error: {msg}")
        return False

    # 6. Build result record
    db_resource_id = db_meta.get("resource_id") if db_meta else None
    result: Dict[str, Any] = {
        "resource_id": db_resource_id or resource_id,
        "tender_folder": folder_name,
        "competition_unique_id": db_meta.get("competition_unique_id") if db_meta else folder_name,
        "source_files": all_source_files,
        "analysis_timestamp": datetime.now(timezone.utc).isoformat(),
        "raw_llm_response": raw_responses if num_chunks > 1 else raw_responses[0],
        "validation_warnings": warnings,
        "narrative_analysis": narrative_analysis,
    }
    for field in ANALYSIS_OUTPUT_FIELDS:
        result[field] = parsed.get(field)
    if db_meta:
        for field in DB_META_FIELDS:
            result[f"db_{field}"] = db_meta.get(field)

    # 7. Save local sidecar
    try:
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save sidecar for {folder_name}: {e}")
        return False

    # 8. Push to Supabase
    if db:
        try:
            db_row: Dict[str, Any] = {
                "resource_id": db_resource_id or resource_id,
                "tender_folder": folder_name,
                "competition_unique_id": result.get("competition_unique_id"),
                "source_files": all_source_files,
                "analysis_timestamp": result["analysis_timestamp"],
                "raw_llm_response": raw_responses if num_chunks > 1 else raw_responses[0],
                "narrative_analysis": narrative_analysis,
            }
            for field in ANALYSIS_OUTPUT_FIELDS:
                val = result.get(field)
                db_row[field] = val if field not in LIST_FIELDS else (
                    val if isinstance(val, list) else ([] if val is None else [val])
                )
            if db_meta:
                for field in DB_META_FIELDS:
                    db_row[f"db_{field}"] = db_meta.get(field)

            db.supabase.table(config.SUPABASE_TABLE_ANALYSIS_RESULTS)\
                .upsert(db_row, on_conflict="tender_folder")\
                .execute()
            logger.debug(f"Upserted analysis for {folder_name}")
        except Exception as e:
            logger.error(f"Supabase upsert failed for {folder_name}: {e} (local sidecar saved)")
            print(f"  -> WARNING: DB sync failed for {folder_name}: {e}", flush=True)

    title = parsed.get("contract_title") or parsed.get("procuring_entity") or folder_name
    ctype = parsed.get("contract_type", "?")
    print(f"  -> Done: [{ctype}] {title}", flush=True)
    return True


# ── Batch runner ───────────────────────────────────────────────────────────────

def run_tender_analysis(limit: int = 0, reanalyse: bool = False) -> Dict[str, Any]:
    """
    Walk all tender document folders and analyse each with the LLM.
    limit=0 processes all. reanalyse=True forces re-analysis of already-done folders.
    Returns summary stats + per-folder log.
    """
    docs_dir = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "documents")
    if not os.path.exists(docs_dir):
        logger.warning(f"Documents directory not found: {docs_dir}")
        return {"total": 0, "analysed": 0, "skipped": 0, "errors": 0, "warnings": 0}

    db = None
    try:
        from db.client.supabase_client import SupabaseClient
        db = SupabaseClient()
    except Exception as e:
        logger.warning(f"Supabase unavailable — results saved locally only: {e}")

    tender_folders = sorted([
        os.path.join(docs_dir, d)
        for d in os.listdir(docs_dir)
        if os.path.isdir(os.path.join(docs_dir, d))
    ])
    if limit > 0:
        tender_folders = tender_folders[:limit]

    total = len(tender_folders)
    analysed = skipped = errors = total_warnings = 0
    log_entries = []

    print(f"Found {total} tender folders to analyse.", flush=True)
    if reanalyse:
        print("  (--reanalyse active: existing analyses will be overwritten)", flush=True)

    run_start = datetime.now(timezone.utc)

    for i, folder in enumerate(tender_folders, start=1):
        folder_name = os.path.basename(folder)
        sidecar_path = os.path.join(folder, "analysis.json")
        failed_path = os.path.join(folder, ".analysis_failed")

        print(f"[{i}/{total}] {folder_name}", flush=True)

        if not reanalyse:
            if os.path.exists(sidecar_path):
                skipped += 1
                continue
            if os.path.exists(failed_path):
                reason = _read_failure_marker(failed_path)
                print(f"  -> Previously failed ({reason}), skipping", flush=True)
                skipped += 1
                continue

        try:
            if analyse_tender_folder(folder, db=db, reanalyse=reanalyse):
                analysed += 1
                try:
                    with open(sidecar_path, encoding="utf-8") as f:
                        total_warnings += len(json.load(f).get("validation_warnings", []))
                except Exception:
                    pass
                log_entries.append({"folder": folder_name, "status": "analysed"})
                time.sleep(RATE_LIMIT_DELAY)
            else:
                skipped += 1
                log_entries.append({"folder": folder_name, "status": "skipped"})
        except Exception as e:
            print(f"  -> ERROR: {e}", flush=True)
            logger.error(f"Analysis failed for {folder}: {e}")
            _write_failure_marker(failed_path, str(e))
            errors += 1
            log_entries.append({"folder": folder_name, "status": "error", "error": str(e)})

    _write_run_log(run_start, total, analysed, skipped, errors, total_warnings, log_entries)

    return {
        "total": total,
        "analysed": analysed,
        "skipped": skipped,
        "errors": errors,
        "warnings": total_warnings,
    }


def _write_run_log(
    run_start: datetime,
    total: int,
    analysed: int,
    skipped: int,
    errors: int,
    warnings: int,
    entries: List[Dict],
) -> None:
    """Write a timestamped run log to data/logs/."""
    log_dir = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "..", "logs")
    os.makedirs(log_dir, exist_ok=True)
    timestamp = run_start.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"analysis_{timestamp}.json")
    payload = {
        "run_start": run_start.isoformat(),
        "run_end": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": total,
            "analysed": analysed,
            "skipped": skipped,
            "errors": errors,
            "warnings": warnings,
        },
        "folders": entries,
    }
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nRun log saved: {log_path}", flush=True)
    except Exception as e:
        logger.warning(f"Could not write run log: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_tender_analysis()
