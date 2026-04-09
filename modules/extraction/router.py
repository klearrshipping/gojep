"""
Document Extraction Router
==========================
Lists all files in Supabase Storage, checks which need extraction,
then routes each file to the appropriate extractor:

  PDF / DOCX / XLSX  →  Lightning AI (Docling, GPU)
  XML                →  xml_extractor  (local)
  XLS                →  xls_extractor  (local)
  DOC                →  doc_extractor  (local)

All results are written to the document_extractions table in Supabase.

Usage:
    python -m modules.extraction.router [--local-only] [--lightning-only]
"""

import os
import sys
import argparse
import tempfile
import subprocess
import requests
from datetime import datetime, timezone
from pathlib import Path

# Local extractor imports
from modules.extraction import xml_extractor, xls_extractor, doc_extractor

# ── Config ─────────────────────────────────────────────────────────────────────

SUPABASE_URL = None  # loaded from settings at runtime
SUPABASE_KEY = None
BUCKET       = "tender-documents"
TABLE        = "document_extractions"

# Routes: extension → handler
LIGHTNING_EXTENSIONS = {".pdf", ".docx", ".xlsx"}
LOCAL_EXTENSIONS     = {".xml", ".xls", ".doc"}
ALL_EXTENSIONS       = LIGHTNING_EXTENSIONS | LOCAL_EXTENSIONS

HEADERS = {}  # populated after credentials are loaded


def _init_credentials():
    global SUPABASE_URL, SUPABASE_KEY, HEADERS
    from config.settings import SUPABASE_URL as URL, SUPABASE_SECRET_KEY as KEY
    SUPABASE_URL = URL.rstrip("/")
    SUPABASE_KEY = KEY
    HEADERS = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }


# ── Supabase helpers ───────────────────────────────────────────────────────────

def list_storage_objects(prefix: str = "") -> list:
    objects = []
    limit, offset = 1000, 0
    while True:
        r = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/list/{BUCKET}",
            headers=HEADERS,
            json={"limit": limit, "offset": offset, "prefix": prefix,
                  "sortBy": {"column": "name", "order": "asc"}},
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        objects.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return objects


def list_tender_folders() -> list:
    top = list_storage_objects(prefix="")
    return [o["name"] for o in top if o.get("id") is None]


def list_files_in_folder(tender_id: str) -> list:
    objects = list_storage_objects(prefix=f"{tender_id}/")
    return [o["name"] for o in objects if o.get("id") is not None]


def download_file(storage_path: str) -> bytes:
    r = requests.get(
        f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{storage_path}",
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.content


def get_already_extracted() -> set:
    done = set()
    limit, offset = 1000, 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{TABLE}",
            headers=HEADERS,
            params={"select": "tender_resource_id,filename",
                    "extraction_status": "eq.completed",
                    "limit": str(limit), "offset": str(offset)},
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for row in batch:
            done.add(f"{row['tender_resource_id']}/{row['filename']}")
        if len(batch) < limit:
            break
        offset += limit
    return done


def upsert_extraction(record: dict):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers={**HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
        params={"on_conflict": "tender_resource_id,filename"},
        json=record,
    )
    r.raise_for_status()


# ── Local extraction ───────────────────────────────────────────────────────────

LOCAL_EXTRACTOR_MAP = {
    ".xml": xml_extractor.extract,
    ".xls": xls_extractor.extract,
    ".doc": doc_extractor.extract,
}


def run_local_extraction(tender_id: str, filename: str, storage_path: str):
    suffix = Path(filename).suffix.lower()
    extractor_fn = LOCAL_EXTRACTOR_MAP[suffix]

    upsert_extraction({
        "tender_resource_id": tender_id,
        "filename":           filename,
        "storage_path":       storage_path,
        "extraction_status":  "pending",
    })

    tmp_path = None
    try:
        data = download_file(storage_path)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        text = extractor_fn(tmp_path)
        os.unlink(tmp_path)

        upsert_extraction({
            "tender_resource_id": tender_id,
            "filename":           filename,
            "storage_path":       storage_path,
            "extracted_text":     text,
            "page_count":         None,
            "extraction_status":  "completed",
            "extracted_at":       datetime.now(timezone.utc).isoformat(),
        })
        print(f"    done ({len(text):,} chars)")
        return True

    except Exception as e:
        print(f"    FAILED: {e}")
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        upsert_extraction({
            "tender_resource_id": tender_id,
            "filename":           filename,
            "storage_path":       storage_path,
            "extraction_status":  "failed",
            "error_message":      str(e)[:500],
        })
        return False


# ── Lightning dispatch ─────────────────────────────────────────────────────────

def dispatch_to_lightning():
    """Trigger the Lightning cloud job for GPU-based extraction via lightning-sdk."""
    from lightning_sdk import Studio, Machine

    print("Connecting to Lightning Studio...")
    studio = Studio(
        name="docling-gpu-extraction-devbox",
        teamspace="document-information-extraction-project",
        user="klearrshipping",
    )

    # Start the studio if it's not already running
    status = getattr(studio, "status", None)
    if status is None or str(status).lower() != "running":
        print(f"Studio is not running (status: {status}). Starting...")
        studio.start()
        print("Studio started.")
    else:
        print(f"Studio already running (status: {status}).")

    script_path = "/teamspace/studios/this_studio/main.py"
    print(f"Running extraction script on Lightning: {script_path}")
    try:
        studio.run(f"python {script_path}")
        print("Lightning extraction complete.")
        print("Stopping studio to save costs...")
        studio.stop()
        print("Studio stopped.")
        return True
    except Exception as e:
        print(f"Lightning job failed: {e}")
        print("Stopping studio after failure...")
        try:
            studio.stop()
        except Exception:
            pass
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

def main(local_only: bool = False, lightning_only: bool = False):
    _init_credentials()

    print("Loading already-extracted records...")
    already_done = get_already_extracted()
    print(f"  {len(already_done)} already completed.")

    print("Listing tender folders...")
    tender_folders = list_tender_folders()
    print(f"  {len(tender_folders)} folders found.")

    # Collect files needing extraction by route
    lightning_queue = []  # (tender_id, filename, storage_path)
    local_queue     = []

    for tender_id in sorted(tender_folders):
        files = list_files_in_folder(tender_id)
        for filename in files:
            suffix = Path(filename).suffix.lower()
            if suffix not in ALL_EXTENSIONS:
                continue
            storage_path = f"{tender_id}/{filename}"
            if storage_path in already_done:
                continue
            if suffix in LIGHTNING_EXTENSIONS:
                lightning_queue.append((tender_id, filename, storage_path))
            elif suffix in LOCAL_EXTENSIONS:
                local_queue.append((tender_id, filename, storage_path))

    print(f"\nQueued for Lightning: {len(lightning_queue)}")
    print(f"Queued for local:     {len(local_queue)}")

    # ── Local extraction ───────────────────────────────────────────────────────
    if not lightning_only and local_queue:
        print(f"\n-- Local extraction ({len(local_queue)} files) --")
        local_ok = local_fail = 0
        for tender_id, filename, storage_path in local_queue:
            print(f"  [{tender_id}] {filename}...")
            if run_local_extraction(tender_id, filename, storage_path):
                local_ok += 1
            else:
                local_fail += 1
        print(f"Local done. Extracted: {local_ok} | Failed: {local_fail}")

    # ── Lightning dispatch ─────────────────────────────────────────────────────
    if not local_only and lightning_queue:
        print(f"\n-- Lightning dispatch ({len(lightning_queue)} files) --")
        dispatch_to_lightning()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Route document extraction to Lightning or local extractors.")
    parser.add_argument("--local-only",    action="store_true", help="Only run local extractors (XML/XLS/DOC)")
    parser.add_argument("--lightning-only",action="store_true", help="Only dispatch to Lightning (PDF/DOCX/XLSX)")
    args = parser.parse_args()
    main(local_only=args.local_only, lightning_only=args.lightning_only)
