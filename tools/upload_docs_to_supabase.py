"""
Upload locally downloaded tender PDFs to Supabase Storage.

Storage path: tender-documents/{tender_resource_id}/{filename}

Usage:
    python tools/upload_docs_to_supabase.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import SUPABASE_URL, SUPABASE_SECRET_KEY
import requests

BUCKET = "tender-documents"
DOCUMENTS_DIR = PROJECT_ROOT / "data" / "tenders" / "documents"

# File types Docling can extract text from
EXTRACTABLE_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".xml"}

CONTENT_TYPES = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls":  "application/vnd.ms-excel",
    ".xml":  "application/xml",
}

headers = {
    "apikey": SUPABASE_SECRET_KEY,
    "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
}


def sanitize_filename(name: str) -> str:
    """Replace characters Supabase Storage rejects with underscores."""
    safe = ""
    for ch in name:
        if ch.isascii() and (ch.isalnum() or ch in "-_.() "):
            safe += ch
        else:
            safe += "_"
    return safe


def upload_file(tender_id: str, file_path: Path) -> bool:
    filename = sanitize_filename(file_path.name)
    storage_path = f"{tender_id}/{filename}"
    url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/{BUCKET}/{storage_path}"
    content_type = CONTENT_TYPES.get(file_path.suffix.lower(), "application/octet-stream")

    # Check if already uploaded
    check = requests.head(url, headers=headers)
    if check.status_code == 200:
        print(f"  skip (exists): {storage_path}")
        return True

    with open(file_path, "rb") as f:
        resp = requests.post(
            url,
            headers={**headers, "Content-Type": content_type},
            data=f,
        )

    if resp.status_code in (200, 201):
        print(f"  uploaded: {storage_path}")
        return True
    else:
        print(f"  FAILED ({resp.status_code}): {storage_path} — {resp.text[:200]}")
        return False


def main():
    tender_dirs = [d for d in DOCUMENTS_DIR.iterdir() if d.is_dir()]
    print(f"Found {len(tender_dirs)} tender folders.")

    total = uploaded = failed = 0

    for tender_dir in sorted(tender_dirs):
        tender_id = tender_dir.name
        files = [f for f in tender_dir.iterdir()
                 if f.is_file() and f.suffix.lower() in EXTRACTABLE_EXTENSIONS]
        if not files:
            continue

        print(f"\n[{tender_id}] {len(files)} files")
        for f in files:
            total += 1
            if upload_file(tender_id, f):
                uploaded += 1
            else:
                failed += 1

    print(f"\nDone. Total: {total} | Uploaded: {uploaded} | Failed: {failed}")


if __name__ == "__main__":
    main()
