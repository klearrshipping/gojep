"""
GOJEP Document Extraction — Lightning AI Studio
================================================
Pulls tender documents from Supabase Storage, runs Docling on each,
and writes extracted text to the document_extractions table.

Environment variables required:
    SUPABASE_URL       — Supabase project URL
    SUPABASE_KEY       — Supabase service role key

Run locally in the studio:
    python main.py

Run as a Lightning cloud job from the VPS:
    lightning run script main.py --cloud --machine T4
"""

import os
import re
import json
import tempfile
import requests
from datetime import datetime, timezone
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
BUCKET       = "tender-documents"
TABLE        = "document_extractions"

EXTRACTABLE_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls"}

FORCE_REPROCESS = False  # set True to re-extract everything

HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

# ── Supabase helpers ───────────────────────────────────────────────────────────

def sb_get(path: str, params: dict = None) -> dict:
    r = requests.get(f"{SUPABASE_URL}{path}", headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def sb_post(path: str, payload: dict) -> dict:
    r = requests.post(f"{SUPABASE_URL}{path}", headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


def sb_patch(path: str, params: dict, payload: dict) -> dict:
    r = requests.patch(f"{SUPABASE_URL}{path}", headers=HEADERS, params=params, json=payload)
    r.raise_for_status()
    return r.json()


def list_storage_objects(prefix: str = "") -> list[dict]:
    """List all objects in the bucket (paginated)."""
    objects = []
    limit = 1000
    offset = 0
    while True:
        payload = {"limit": limit, "offset": offset, "prefix": prefix, "sortBy": {"column": "name", "order": "asc"}}
        r = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/list/{BUCKET}",
            headers=HEADERS,
            json=payload,
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


def list_tender_folders() -> list[str]:
    """Return list of tender_id folder names in the bucket."""
    top = list_storage_objects(prefix="")
    return [o["name"] for o in top if o.get("id") is None]  # folders have no id


def list_files_in_folder(tender_id: str) -> list[str]:
    """Return filenames inside a tender folder."""
    objects = list_storage_objects(prefix=f"{tender_id}/")
    return [o["name"] for o in objects if o.get("id") is not None]


def download_file(storage_path: str) -> bytes:
    r = requests.get(
        f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{storage_path}",
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.content


def get_already_extracted() -> set[str]:
    """Return set of 'tender_resource_id/filename' already in document_extractions."""
    done = set()
    limit = 1000
    offset = 0
    while True:
        params = {
            "select": "tender_resource_id,filename",
            "extraction_status": "eq.completed",
            "limit": str(limit),
            "offset": str(offset),
        }
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{TABLE}",
            headers=HEADERS,
            params=params,
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
        headers={
            **HEADERS,
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        params={"on_conflict": "tender_resource_id,filename"},
        json=record,
    )
    r.raise_for_status()


# ── Docling setup ──────────────────────────────────────────────────────────────

def init_docling():
    import torch
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        TableStructureOptions,
        TableFormerMode,
    )
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice

    print(f"CUDA available: {torch.cuda.is_available()}")

    accelerator = AcceleratorOptions(device=AcceleratorDevice.AUTO)

    # Full layout pipeline: accurate TableFormer mode + OCR for scanned/image PDFs
    pdf_opts = PdfPipelineOptions()
    pdf_opts.accelerator_options = accelerator
    pdf_opts.do_ocr = True                   # handles scanned + image-based PDFs
    pdf_opts.do_table_structure = True
    pdf_opts.table_structure_options = TableStructureOptions(mode=TableFormerMode.ACCURATE)
    pdf_opts.do_picture_classification = False
    pdf_opts.do_picture_description = False

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_opts)}
    )
    return converter


def extract_text(converter, file_path: str) -> tuple[str, int]:
    """Run Docling and return (markdown_text, page_count)."""
    result = converter.convert(file_path)
    doc = result.document
    markdown = doc.export_to_markdown()
    page_count = len(doc.pages) if hasattr(doc, "pages") else None
    return markdown, page_count


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("Installing Docling...")
    os.system("pip install -q docling pypdf")

    print(f"Connecting to Supabase: {SUPABASE_URL}")
    converter = init_docling()

    print("Loading already-extracted records...")
    already_done = set() if FORCE_REPROCESS else get_already_extracted()
    print(f"  {len(already_done)} already extracted.")

    print("Listing tender folders in storage...")
    tender_folders = list_tender_folders()
    print(f"  {len(tender_folders)} folders found.")

    total = processed = skipped = failed = 0

    for tender_id in sorted(tender_folders):
        files = list_files_in_folder(tender_id)
        extractable = [
            f for f in files
            if Path(f).suffix.lower() in EXTRACTABLE_EXTENSIONS
        ]
        if not extractable:
            continue

        print(f"\n[{tender_id}] {len(extractable)} files")

        for filename in extractable:
            storage_path = f"{tender_id}/{filename}"
            total += 1

            if storage_path in already_done:
                print(f"  skip: {filename}")
                skipped += 1
                continue

            print(f"  extracting: {filename}...")
            suffix = Path(filename).suffix.lower()

            # Mark as pending
            upsert_extraction({
                "tender_resource_id": tender_id,
                "filename":           filename,
                "storage_path":       storage_path,
                "extraction_status":  "pending",
            })

            try:
                data = download_file(storage_path)

                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(data)
                    tmp_path = tmp.name

                markdown, page_count = extract_text(converter, tmp_path)
                os.unlink(tmp_path)

                upsert_extraction({
                    "tender_resource_id": tender_id,
                    "filename":           filename,
                    "storage_path":       storage_path,
                    "extracted_text":     markdown,
                    "page_count":         page_count,
                    "extraction_status":  "completed",
                    "extracted_at":       datetime.now(timezone.utc).isoformat(),
                })
                print(f"    done ({page_count} pages, {len(markdown):,} chars)")
                processed += 1

            except Exception as e:
                print(f"    FAILED: {e}")
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
                failed += 1

    print(f"\n{'='*60}")
    print(f"Done. Total: {total} | Extracted: {processed} | Skipped: {skipped} | Failed: {failed}")


if __name__ == "__main__":
    main()
