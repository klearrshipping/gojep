"""Download clarification documents for a tender via the GOJEP portal.

Emails workflow: saves clarifications to email_updates/clarifications/document_downloads/

Approach:
  1. Navigate to listClarification.do?resourceId=X (requires logged-in Selenium session)
  2. Click the "Download all clarifications" button (JavaScript-driven download)
  3. Wait for the ZIP file to land in a temp download directory
  4. Extract the ZIP to a temp folder, compare against existing clarifications
  5. Only sync new/changed files to email_updates/clarifications/document_downloads/
  6. Upload new/changed files to Supabase Storage
  7. Update manifest tracking (name + size + MD5 hash)

Folder Structure:
  data/tenders/documents/<tender_id>/
  └── email_updates/
      └── clarifications/
          ├── document_downloads/     <- downloaded clarification files
          ├── extracted_documents/    <- flat copy for extraction
          └── json_documents/         <- extracted JSON + .manifest.json
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from config import settings as config
from modules.shared.document_sync import (
    build_file_manifest,
    compare_manifests,
    load_extraction_manifest,
    save_extraction_manifest,
    update_file_in_manifest,
    EMAIL_UPDATES,
    CLARIFICATIONS,
    DOCUMENT_DOWNLOADS,
    EXTRACTED_DOCUMENTS,
    JSON_DOCUMENTS,
)
from modules.shared.document_download import upload_file_to_supabase

logger = logging.getLogger(__name__)

CONTENT_TYPES = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls":  "application/vnd.ms-excel",
    ".xml":  "application/xml",
    ".zip":  "application/zip",
}


def _wait_for_download(download_dir: str, timeout: int = 60) -> Path | None:
    """Poll download_dir until a non-.crdownload file appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = [
            Path(download_dir) / f
            for f in os.listdir(download_dir)
            if not f.endswith(".crdownload")
            and not f.startswith(".")
            and Path(f).suffix.lower() in CONTENT_TYPES
        ]
        if files:
            return max(files, key=lambda p: p.stat().st_mtime)
        time.sleep(1)
    return None


def _click_download_all(driver, resource_id: str, download_dir: str) -> Path | None:
    """Navigate to listClarification.do, click 'Download all clarifications', wait for file."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    url = f"{config.GOJEP_BASE_URL}/epps/cft/listClarification.do?resourceId={resource_id}"
    logger.info("Navigating to clarification page: %s", url)
    driver.get(url)

    WebDriverWait(driver, config.SELENIUM_TIMEOUT).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

    try:
        btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[@onclick='download(this)']")
            )
        )
    except Exception:
        logger.warning("  'Download all clarifications' button not found for resourceId=%s", resource_id)
        return None

    logger.info("  Clicking 'Download all clarifications' button ...")
    btn.click()

    downloaded = _wait_for_download(download_dir, timeout=60)
    if not downloaded:
        logger.warning("  Download timed out for resourceId=%s", resource_id)
    return downloaded


def _extract_zip_flat(zip_path: Path, dest_dir: str) -> None:
    """
    Extract all files from a ZIP directly into dest_dir with no sub-folders.
    If a filename already exists it is skipped (not overwritten).
    Recursively flattens any nested ZIPs found after extraction, then removes them.
    """
    os.makedirs(dest_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                fname = re.sub(r'[<>:"/\\|?*]', "_", os.path.basename(member.filename))
                if not fname:
                    continue
                target = os.path.join(dest_dir, fname)
                if os.path.exists(target):
                    continue
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                logger.info("  Extracted: %s", fname)
    except zipfile.BadZipFile:
        dest_path = os.path.join(dest_dir, zip_path.name)
        if str(zip_path) != dest_path:
            shutil.copy2(zip_path, dest_path)
        return

    # Recursively flatten any nested ZIPs
    for name in list(os.listdir(dest_dir)):
        if not name.lower().endswith(".zip"):
            continue
        nested = Path(dest_dir) / name
        try:
            _extract_zip_flat(nested, dest_dir)
            nested.unlink()
            logger.info("  Extracted and removed nested zip: %s", name)
        except zipfile.BadZipFile:
            logger.warning("  Skipped bad nested zip: %s", name)
            nested.unlink()
        except Exception as e:
            logger.warning("  Failed to extract nested zip %s: %s", name, e)


def _unblock_files(directory: str) -> None:
    """Strip Windows Zone.Identifier from downloaded files so Docling can read them."""
    import subprocess
    try:
        subprocess.run(
            ["powershell", "-Command",
             f"Get-ChildItem -Path '{directory}' -Recurse | Unblock-File"],
            capture_output=True, timeout=30,
        )
    except Exception as e:
        logger.warning("  Unblock-File failed for %s: %s", directory, e)


def download_new_clarification_attachments(
    driver,
    resource_id: str,
    competition_unique_id: str,
    docs_base_dir: str | None = None,
) -> dict[str, Any]:
    """
    Click "Download all clarifications" for resource_id, compare against
    existing files, sync only new/changed files to
    email_updates/clarifications/document_downloads/, upload to Supabase Storage,
    and update manifest.

    Args:
        driver:                  Logged-in Selenium WebDriver.
        resource_id:             GOJEP resource ID (e.g. "8568071").
        competition_unique_id:   Folder name as stored on disk (e.g. "1000_972").
        docs_base_dir:           Root of the documents dir. Defaults to
                                 data/tenders/documents/.

    Returns dict:
    {
      "resource_id": str,
      "competition_unique_id": str,
      "new_attachments_downloaded": int,
      "skipped_already_uploaded": int,
      "failed_uploads": int,
      "downloaded_files": [str, ...],
      "new_files": [str, ...],
      "changed_files": [str, ...],
      "missing_files": [str, ...],
    }
    """
    if docs_base_dir is None:
        docs_base_dir = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "documents")

    folder_name = competition_unique_id.replace("/", "_", 1)
    tender_dir = os.path.join(docs_base_dir, folder_name)

    clarif_downloads = os.path.join(
        tender_dir, EMAIL_UPDATES, CLARIFICATIONS, DOCUMENT_DOWNLOADS
    )
    clarif_extracted = os.path.join(
        tender_dir, EMAIL_UPDATES, CLARIFICATIONS, EXTRACTED_DOCUMENTS
    )
    clarif_json = os.path.join(
        tender_dir, EMAIL_UPDATES, CLARIFICATIONS, JSON_DOCUMENTS
    )

    os.makedirs(clarif_downloads, exist_ok=True)
    os.makedirs(clarif_extracted, exist_ok=True)
    os.makedirs(clarif_json, exist_ok=True)

    existing_manifest = build_file_manifest(clarif_downloads, relative_to=clarif_downloads)

    import tempfile
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_download:
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {
            "behavior":     "allow",
            "downloadPath": os.path.normpath(os.path.abspath(tmp_download)).replace("/", "\\"),
        })

        downloaded_file = _click_download_all(driver, resource_id, tmp_download)

        if not downloaded_file:
            return {
                "resource_id":                resource_id,
                "competition_unique_id":      competition_unique_id,
                "new_attachments_downloaded": 0,
                "skipped_already_uploaded":    0,
                "failed_uploads":             0,
                "downloaded_files":           [],
                "new_files":                  [],
                "changed_files":              [],
                "missing_files":             [],
                "error":                      "download button not found or timed out",
            }

        tmp_clarif_dir = os.path.join(tmp_download, "clarifications")
        os.makedirs(tmp_clarif_dir, exist_ok=True)
        _extract_zip_flat(downloaded_file, tmp_clarif_dir)

    new_manifest = build_file_manifest(tmp_clarif_dir, relative_to=tmp_clarif_dir)

    existing_flat = {}
    for fname, info in existing_manifest.items():
        existing_flat[os.path.basename(fname)] = info

    new_flat = {}
    for fname, info in new_manifest.items():
        new_flat[os.path.basename(fname)] = info

    comparison = compare_manifests(existing_flat, new_flat)

    files_to_sync = [fname for fname, status in comparison.items() if status in ("new", "changed")]

    missing_files = [fname for fname, status in comparison.items() if status == "deleted"]
    if missing_files:
        logger.warning("Files removed from clarification download for %s: %s",
                      competition_unique_id, missing_files)

    copied_files: list[str] = []
    for fname in files_to_sync:
        src = os.path.join(tmp_clarif_dir, fname)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(clarif_downloads, fname))
            copied_files.append(fname)
            logger.info("  Synced clarification file: %s", fname)

    for fname in copied_files:
        src = os.path.join(clarif_downloads, fname)
        dst = os.path.join(clarif_extracted, fname)
        shutil.copy2(src, dst)

    # Strip Windows Zone.Identifier so Docling can read the files
    if copied_files:
        _unblock_files(clarif_downloads)

    extraction_manifest = load_extraction_manifest(clarif_json)

    new_count = 0
    skipped_count = 0
    failed_count = 0
    uploaded_files: list[str] = []

    for fname in files_to_sync:
        file_path = Path(clarif_downloads) / fname
        if not file_path.exists():
            continue

        if file_path.suffix.lower() not in CONTENT_TYPES:
            logger.debug("  Skipping non-document file: %s", fname)
            skipped_count += 1
            continue

        storage_subfolder = f"{EMAIL_UPDATES}/{CLARIFICATIONS}"
        success = upload_file_to_supabase(file_path, folder_name, storage_subfolder)
        if success:
            file_hash = build_file_manifest(os.path.dirname(file_path)).get(fname, {}).get("hash_md5", "")
            update_file_in_manifest(
                extraction_manifest,
                fname,
                file_path.stat().st_size,
                file_hash,
                f"{EMAIL_UPDATES}/{CLARIFICATIONS}/{DOCUMENT_DOWNLOADS}/{fname}",
            )
            uploaded_files.append(fname)
            new_count += 1
        else:
            failed_count += 1

    save_extraction_manifest(clarif_json, extraction_manifest)

    result = {
        "resource_id":                resource_id,
        "competition_unique_id":       competition_unique_id,
        "new_attachments_downloaded":  new_count,
        "skipped_already_uploaded":   skipped_count,
        "failed_uploads":             failed_count,
        "downloaded_files":           uploaded_files,
        "new_files":                  [f for f in files_to_sync if comparison.get(f) == "new"],
        "changed_files":              [f for f in files_to_sync if comparison.get(f) == "changed"],
        "missing_files":              missing_files,
    }

    logger.info(
        "Clarification download complete for resourceId=%s: %d new, %d changed, %d skipped, %d failed",
        resource_id,
        len(result["new_files"]),
        len(result["changed_files"]),
        skipped_count,
        failed_count,
    )
    return result