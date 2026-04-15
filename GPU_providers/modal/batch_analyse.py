"""
Batch tender analysis using OpenRouter — processes one folder at a time,
sending each chunk sequentially to the OpenRouter API.

Usage:
    python GPU_providers/modal/batch_analyse.py
    python GPU_providers/modal/batch_analyse.py --folder 1000_972
    python GPU_providers/modal/batch_analyse.py --reanalyse
    python GPU_providers/modal/batch_analyse.py --dry-run
"""

import argparse
import json
import logging
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from config import settings as config
from modules.analysis.prompt import ANALYSIS_SYSTEM_PROMPT, LOCAL_LLM_TOKEN_LIMIT
from modules.analysis.fetch import _fetch_db_metadata, _format_metadata_header, DB_META_FIELDS
from modules.analysis.chunk import _extract_file_texts, _split_into_chunks
from modules.analysis.parse import _merge_parsed_results
from db.client.supabase_client import SupabaseClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

SIDECAR_FILENAME  = "analysis.json"
FAILED_MARKER     = ".analysis_failed"
MAX_OUTPUT_TOKENS = 4_000


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_done(folder_path):
    return os.path.exists(os.path.join(folder_path, SIDECAR_FILENAME))

def _is_failed(folder_path):
    return os.path.exists(os.path.join(folder_path, FAILED_MARKER))

def _has_extracted_docs(folder_path):
    """
    Return True if this tender folder has at least one non-noise JSON file ready
    for analysis. Checks all locations that _extract_file_texts reads from:
      - tender_data/json_documents/
      - email_updates/clarifications/json_documents/
      - email_updates/new_documents/json_documents/
    """
    candidates = [
        os.path.join(folder_path, "tender_data", "json_documents"),
        os.path.join(folder_path, "email_updates", "clarifications", "json_documents"),
        os.path.join(folder_path, "email_updates", "new_documents", "json_documents"),
    ]
    for d in candidates:
        if os.path.isdir(d):
            if any(
                f.endswith(".json") and f != ".manifest.json"
                for f in os.listdir(d)
            ):
                return True
    return False

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

def _call_openrouter(request: dict) -> dict:
    """Send a single chat request to OpenRouter and return the response dict."""
    model = config.OPENROUTER_MODELS[config.ANALYSIS_MODEL]
    payload = {
        "model": model,
        "messages": request["messages"],
        "max_tokens": request.get("max_tokens", MAX_OUTPUT_TOKENS),
        "temperature": request.get("temperature", 0.1),
    }
    t0 = time.time()
    resp = requests.post(
        config.OPENROUTER_URL,
        headers=config.OPENROUTER_HEADERS,
        json=payload,
        timeout=180,
    )
    resp.raise_for_status()
    elapsed = round(time.time() - t0, 2)
    data = resp.json()
    data["_elapsed_s"] = elapsed
    return data

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
        # Exclude keys not present in gojep_analysis_results schema
        _SKIP_KEYS = {"folder", "analysed_at", "analysis_timestamp", "validation_warnings"}
        row = {
            "tender_folder":         folder_name,
            "competition_unique_id": competition_uid,
            "analysis_timestamp":    now,
            **{k: v for k, v in result.items() if k not in _SKIP_KEYS},
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
    """Load files, build chunks, return list of API request dicts."""
    meta_header = ""
    if db_meta:
        meta_header = _format_metadata_header(db_meta) + "\n\n=== TENDER DOCUMENTS ===\n"

    file_texts = _extract_file_texts(folder_path)
    if not file_texts:
        return []

    chunks     = _split_into_chunks(file_texts, LOCAL_LLM_TOKEN_LIMIT)
    num_chunks = len(chunks)
    reqs       = []

    for i, chunk in enumerate(chunks, start=1):
        parts = [meta_header] if meta_header else []
        if num_chunks > 1:
            parts.append(f"[Part {i} of {num_chunks} -- extract all fields visible in this part]\n")
        for source_file, text in chunk:
            parts.append(f"\n\n=== FILE: {source_file} ===\n{text}")

        reqs.append({
            "messages": [
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user",   "content": "".join(parts)},
            ],
            "max_tokens":  MAX_OUTPUT_TOKENS,
            "temperature": 0.1,
        })

    return reqs


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Batch tender analysis via OpenRouter")
    parser.add_argument("--folder",     default="", help="Process a single folder only")
    parser.add_argument("--reanalyse",  action="store_true", help="Re-analyse already-processed folders")
    parser.add_argument("--dry-run",    action="store_true", help="Count chunks without calling the API")
    parser.add_argument("--since",      default="", help="Reanalyse folders with analysis_timestamp older than YYYY-MM-DD")
    args = parser.parse_args()

    docs_dir = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "documents")

    all_folders = sorted([
        d for d in os.listdir(docs_dir)
        if os.path.isdir(os.path.join(docs_dir, d))
    ])
    if args.folder:
        all_folders = [f for f in all_folders if f == args.folder]
        if not all_folders:
            print(f"Folder '{args.folder}' not found.")
            return

    db = SupabaseClient() if config.SAVE_TO_SUPABASE else None

    stale_folders: set[str] = set()
    if db and args.since:
        try:
            rows = db.supabase.table(config.SUPABASE_TABLE_ANALYSIS_RESULTS)\
                .select("tender_folder")\
                .lt("analysis_timestamp", args.since)\
                .execute().data or []
            stale_folders = {r["tender_folder"] for r in rows}
            logger.info(f"Stale folders (before {args.since}): {len(stale_folders)}")
        except Exception as e:
            logger.warning(f"Could not fetch stale folders: {e}")

    analysed_folders: set[str] = set()
    if db and not args.reanalyse:
        try:
            page_size = 1000
            offset = 0
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
        if args.since and folder_name in stale_folders:
            if _has_extracted_docs(folder_path):
                pending_folders.append((folder_name, folder_path))
            continue
        if not args.reanalyse and _is_done(folder_path):
            continue
        if not args.reanalyse and _is_failed(folder_path):
            continue
        if not _has_extracted_docs(folder_path):
            continue
        if not args.reanalyse and folder_name in analysed_folders:
            logger.debug(f"Skipping {folder_name} -- confirmed in DB")
            continue
        pending_folders.append((folder_name, folder_path))

    total = len(pending_folders)
    model = config.OPENROUTER_MODELS[config.ANALYSIS_MODEL]
    print(f"\nModel  : {model}")
    print(f"Pending: {total} folder(s)\n")

    if args.dry_run:
        print("Building chunk counts (loading files)...")
        grand_total = 0
        for folder_name, folder_path in pending_folders:
            db_meta  = _fetch_db_metadata(folder_name, db, tender_folder=folder_path) if db else None
            reqs     = _build_chunk_requests(folder_path, db_meta)
            grand_total += len(reqs)
            print(f"  {folder_name}: {len(reqs)} chunk(s)")
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

        if args.reanalyse:
            for marker in [SIDECAR_FILENAME, FAILED_MARKER]:
                p = os.path.join(folder_path, marker)
                if os.path.exists(p):
                    os.unlink(p)

        db_meta  = _fetch_db_metadata(folder_name, db, tender_folder=folder_path) if db else None
        reqs     = _build_chunk_requests(folder_path, db_meta)

        if not reqs:
            print(f"  No extractable content -- skipping", flush=True)
            continue

        num_chunks = len(reqs)
        db_status  = "with DB metadata" if db_meta else "no DB metadata"
        print(f"  {num_chunks} chunk(s), {db_status}", flush=True)

        parsed_chunks = []
        failed        = False

        for chunk_idx, req in enumerate(reqs, start=1):
            try:
                response = _call_openrouter(req)
                content  = response.get("choices", [{}])[0].get("message", {}).get("content", "")
                elapsed  = response.get("_elapsed_s", "?")
                parsed   = _parse_llm_json(content)

                if parsed:
                    parsed_chunks.append(parsed)
                    print(f"  chunk {chunk_idx}/{num_chunks} -> ok ({elapsed}s)", flush=True)
                else:
                    print(f"  chunk {chunk_idx}/{num_chunks} -> JSON parse failed", flush=True)
                    logger.warning(f"[{folder_name}] chunk {chunk_idx}: bad content: {content[:200]}")

            except Exception as e:
                print(f"  chunk {chunk_idx}/{num_chunks} -> ERROR: {e}", flush=True)
                logger.error(f"[{folder_name}] chunk {chunk_idx} error: {e}")
                failed = True
                break

        if failed or not parsed_chunks:
            _mark_failed(folder_path)
            failed_count += 1
            print(f"  -> FAILED", flush=True)
            continue

        merged = _merge_parsed_results(parsed_chunks)
        _save_result(folder_path, folder_name, merged, db_meta, db)
        done_count += 1

        folder_elapsed = int(time.time() - folder_start)
        print(f"  -> SAVED ({folder_elapsed}s for {num_chunks} chunk(s), ~{folder_elapsed//max(1,num_chunks)}s/chunk)", flush=True)

    total_elapsed = int(time.time() - run_start)
    print(f"\n{'='*50}")
    print(f"Batch analysis complete -- {total_elapsed//60}m {total_elapsed%60}s total")
    print(f"  Done  : {done_count}")
    print(f"  Failed: {failed_count}")


if __name__ == "__main__":
    main()
