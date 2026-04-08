"""
Batch tender analysis using RunPod serverless endpoint.

Mirrors GPU_providers/modal/batch_analyse.py but sends inference
requests to a RunPod endpoint instead of Modal GPU containers.

Usage:
    python -m GPU_providers.runpod.batch_analyse
    python -m GPU_providers.runpod.batch_analyse --folder 1000_972
    python -m GPU_providers.runpod.batch_analyse --reanalyse
    python -m GPU_providers.runpod.batch_analyse --dry-run
"""

import json
import logging
import os
import sys
import time
import argparse

import runpod

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from config import settings as config
from modules.analysis.analyse_tender import (
    ANALYSIS_SYSTEM_PROMPT,
    _fetch_db_metadata,
    _extract_file_texts,
    _split_into_chunks,
    _format_metadata_header,
    _merge_parsed_results,
    LOCAL_LLM_TOKEN_LIMIT,
    DB_META_FIELDS,
)
from db.supabase_client import SupabaseClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

SIDECAR_FILENAME  = "analysis.json"
FAILED_MARKER     = ".analysis_failed"
MAX_OUTPUT_TOKENS = 4_000

# ── RunPod setup ───────────────────────────────────────────────────────────────

runpod.api_key = config.RUNPOD_API_KEY
endpoint = runpod.Endpoint(config.RUNPOD_ENDPOINT_ID)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_done(folder_path):
    return os.path.exists(os.path.join(folder_path, SIDECAR_FILENAME))

def _is_failed(folder_path):
    return os.path.exists(os.path.join(folder_path, FAILED_MARKER))

def _has_extracted_docs(folder_path):
    d = os.path.join(folder_path, "extracted_docs")
    return os.path.isdir(d) and any(f.endswith(".json") for f in os.listdir(d))

def _mark_failed(folder_path):
    open(os.path.join(folder_path, FAILED_MARKER), "w").close()

def _parse_llm_json(raw):
    import re
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None

def _save_result(folder_path, folder_name, result, db_meta, db):
    from datetime import datetime, timezone
    if db_meta:
        for field in DB_META_FIELDS:
            result[f"db_{field}"] = db_meta.get(field)
    now = datetime.now(timezone.utc).isoformat()
    result["folder"]             = folder_name
    result["analysed_at"]        = now
    result["analysis_timestamp"] = now

    sidecar = os.path.join(folder_path, SIDECAR_FILENAME)
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    if db and config.SAVE_TO_SUPABASE:
        resource_id     = db_meta.get("resource_id") if db_meta else None
        competition_uid = folder_name.replace("_", "/", 1)
        row = {
            "tender_folder":         folder_name,
            "competition_unique_id": competition_uid,
            "analysis_timestamp":    now,
            **{k: v for k, v in result.items() if k not in ("folder", "analysed_at", "analysis_timestamp")},
        }
        if resource_id:
            row["resource_id"] = resource_id
        try:
            db.supabase.table(config.SUPABASE_TABLE_ANALYSIS_RESULTS)\
                .upsert(row, on_conflict="tender_folder").execute()
        except Exception as e:
            logger.warning(f"Supabase save failed for {folder_name}: {e}")

        try:
            db.supabase.table(config.SUPABASE_TABLE_CONTRACT_ANALYSIS)\
                .update({"analysis_timestamp": now})\
                .eq("competition_unique_id", competition_uid)\
                .execute()
        except Exception as e:
            logger.warning(f"contract_analysis timestamp update failed for {folder_name}: {e}")

def _build_chunk_requests(folder_path, db_meta):
    """Load files, build chunks, return list of RunPod input dicts."""
    meta_header = ""
    if db_meta:
        meta_header = _format_metadata_header(db_meta) + "\n\n=== TENDER DOCUMENTS ===\n"

    file_texts = _extract_file_texts(folder_path)
    if not file_texts:
        return []

    chunks     = _split_into_chunks(file_texts, LOCAL_LLM_TOKEN_LIMIT)
    num_chunks = len(chunks)
    requests   = []

    for i, chunk in enumerate(chunks, start=1):
        parts = [meta_header] if meta_header else []
        if num_chunks > 1:
            parts.append(f"[Part {i} of {num_chunks} — extract all fields visible in this part]\n")
        for source_file, text in chunk:
            parts.append(f"\n\n=== FILE: {source_file} ===\n{text}")

        requests.append({
            "method":      "chat.completions",
            "messages": [
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user",   "content": "".join(parts)},
            ],
            "max_tokens":  MAX_OUTPUT_TOKENS,
            "temperature": 0.1,
        })

    return requests


def _run_chunk(request: dict) -> str | None:
    """Submit a single chunk to the RunPod endpoint and wait for the result."""
    try:
        # SDK wraps automatically as {"input": request}
        job = endpoint.run(request)
        output = job.output(timeout=300)  # 5 minute timeout per chunk

        if isinstance(output, dict):
            # vLLM OpenAI-compatible response nested under "output"
            if "output" in output and isinstance(output["output"], dict):
                output = output["output"]
            # Standard OpenAI choices format
            choices = output.get("choices")
            if choices:
                return choices[0].get("message", {}).get("content")
            # Fallback keys
            return output.get("text") or output.get("content") or output.get("output")

        if isinstance(output, str):
            return output

        return None
    except Exception as e:
        logger.error(f"RunPod job failed: {e}")
        return None


# ── Main ───────────────────────────────────────────────────────────────────────

def main(folder: str = "", reanalyse: bool = False, dry_run: bool = False):
    docs_dir = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "documents")

    all_folders = sorted([
        d for d in os.listdir(docs_dir)
        if os.path.isdir(os.path.join(docs_dir, d))
    ])
    if folder:
        all_folders = [f for f in all_folders if f == folder]
        if not all_folders:
            print(f"Folder '{folder}' not found.")
            return

    db = SupabaseClient() if config.SAVE_TO_SUPABASE else None

    analysed_folders: set[str] = set()
    if db and not reanalyse:
        try:
            page_size, offset = 1000, 0
            while True:
                rows = db.supabase.table(config.SUPABASE_TABLE_ANALYSIS_RESULTS)\
                    .select("tender_folder,resource_id,competition_unique_id")\
                    .range(offset, offset + page_size - 1)\
                    .execute().data or []
                for r in rows:
                    tf  = r.get("tender_folder")
                    rid = r.get("resource_id")
                    uid = r.get("competition_unique_id")
                    if tf and rid and uid and tf == uid.replace("/", "_", 1):
                        analysed_folders.add(tf)
                if len(rows) < page_size:
                    break
                offset += page_size
            logger.info(f"Confirmed analysed in DB: {len(analysed_folders)} tender(s)")
        except Exception as e:
            logger.warning(f"Could not fetch analysed folders from DB: {e}")

    pending_folders = []
    for folder_name in all_folders:
        folder_path = os.path.join(docs_dir, folder_name)
        if not reanalyse and _is_done(folder_path):
            continue
        if not reanalyse and _is_failed(folder_path):
            continue
        if not _has_extracted_docs(folder_path):
            continue
        if not reanalyse and folder_name in analysed_folders:
            logger.debug(f"Skipping {folder_name} — confirmed in DB")
            continue
        pending_folders.append((folder_name, folder_path))

    total = len(pending_folders)
    print(f"\nPending: {total} folder(s)\n")

    if dry_run:
        print("Building chunk counts (loading files)...")
        grand_total = 0
        for folder_name, folder_path in pending_folders:
            db_meta  = _fetch_db_metadata(folder_name, db, tender_folder=folder_path) if db else None
            requests = _build_chunk_requests(folder_path, db_meta)
            grand_total += len(requests)
            print(f"  {folder_name}: {len(requests)} chunk(s)")
        print(f"\nTotal chunks: {grand_total}")
        return

    if not pending_folders:
        print("Nothing to analyse.")
        return

    done_count   = 0
    failed_count = 0
    run_start    = time.time()

    for idx, (folder_name, folder_path) in enumerate(pending_folders, start=1):
        folder_start  = time.time()
        elapsed_total = int(time.time() - run_start)
        print(f"\n[{idx}/{total}] {folder_name}  (total elapsed: {elapsed_total//60}m {elapsed_total%60}s)", flush=True)

        if reanalyse:
            for marker in [SIDECAR_FILENAME, FAILED_MARKER]:
                p = os.path.join(folder_path, marker)
                if os.path.exists(p):
                    os.unlink(p)

        db_meta  = _fetch_db_metadata(folder_name, db, tender_folder=folder_path) if db else None
        requests = _build_chunk_requests(folder_path, db_meta)

        if not requests:
            print("  No extractable content — skipping", flush=True)
            continue

        num_chunks = len(requests)
        db_status  = "with DB metadata" if db_meta else "no DB metadata"
        print(f"  {num_chunks} chunk(s), {db_status}", flush=True)

        parsed_chunks = []
        failed        = False

        for chunk_idx, request in enumerate(requests, start=1):
            t0      = time.time()
            content = _run_chunk(request)
            elapsed = round(time.time() - t0, 1)

            if content:
                parsed = _parse_llm_json(content)
                if parsed:
                    parsed_chunks.append(parsed)
                    print(f"  chunk {chunk_idx}/{num_chunks} -> ok ({elapsed}s)", flush=True)
                else:
                    print(f"  chunk {chunk_idx}/{num_chunks} -> JSON parse failed", flush=True)
                    logger.warning(f"[{folder_name}] chunk {chunk_idx}: bad content: {content[:200]}")
            else:
                print(f"  chunk {chunk_idx}/{num_chunks} -> no output", flush=True)
                failed = True

        if failed or not parsed_chunks:
            _mark_failed(folder_path)
            failed_count += 1
            print("  -> FAILED", flush=True)
            continue

        merged = _merge_parsed_results(parsed_chunks)
        _save_result(folder_path, folder_name, merged, db_meta, db)
        done_count += 1

        folder_elapsed = int(time.time() - folder_start)
        print(f"  -> SAVED ({folder_elapsed}s for {num_chunks} chunk(s), ~{folder_elapsed//max(1,num_chunks)}s/chunk)", flush=True)

    total_elapsed = int(time.time() - run_start)
    print(f"\n{'='*50}")
    print(f"Batch analysis complete — {total_elapsed//60}m {total_elapsed%60}s total")
    print(f"  Done  : {done_count}")
    print(f"  Failed: {failed_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch tender analysis via RunPod.")
    parser.add_argument("--folder",    default="", help="Analyse a single folder")
    parser.add_argument("--reanalyse", action="store_true", help="Re-analyse already-done folders")
    parser.add_argument("--dry-run",   action="store_true", help="Count chunks without running inference")
    args = parser.parse_args()
    main(folder=args.folder, reanalyse=args.reanalyse, dry_run=args.dry_run)
