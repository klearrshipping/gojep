"""Shared document sync and comparison logic for GOJEP tender documents."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Folder structure constants ───────────────────────────────────────────────

TENDER_DATA = "tender_data"
EMAIL_UPDATES = "email_updates"
DOCUMENT_DOWNLOADS = "document_downloads"
EXTRACTED_DOCUMENTS = "extracted_documents"
JSON_DOCUMENTS = "json_documents"
MANIFEST_FILENAME = ".manifest.json"

# Email update subfolders
CLARIFICATIONS = "clarifications"
NEW_DOCUMENTS = "new_documents"
MODIFICATIONS = "modifications"


# ── Manifest building ────────────────────────────────────────────────────────

def build_file_manifest(
    directory: str,
    relative_to: str | None = None,
    include_hash: bool = True,
) -> dict[str, dict]:
    """Build a manifest of all files in directory.

    Args:
        directory: Root directory to scan.
        relative_to: If provided, paths in manifest are relative to this path.
                    If None, paths are relative to directory.
        include_hash: If True, compute MD5 hash for each file.

    Returns:
        Dict mapping relative path -> {size, mtime, hash_md5}.
        Empty dict if directory doesn't exist.
    """
    manifest: dict[str, dict] = {}
    if not os.path.exists(directory):
        return manifest
    base_path = Path(relative_to) if relative_to else Path(directory)
    for root, _, files in os.walk(directory):
        for fname in files:
            if fname == MANIFEST_FILENAME:
                continue
            full_path = Path(root) / fname
            try:
                stat = full_path.stat()
                rel_path = str(full_path.relative_to(base_path).as_posix())
                entry: dict[str, Any] = {"size": stat.st_size, "mtime": stat.st_mtime}
                if include_hash:
                    entry["hash_md5"] = compute_md5(full_path)
                manifest[rel_path] = entry
            except Exception as e:
                logger.warning("Failed to process file %s: %s", full_path, e)
    return manifest


def compute_md5(file_path: Path) -> str:
    """Compute MD5 hash of a file."""
    md5 = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                md5.update(chunk)
        return md5.hexdigest()
    except Exception as e:
        logger.warning("Failed to compute MD5 for %s: %s", file_path, e)
        return ""


# ── Manifest I/O ─────────────────────────────────────────────────────────────

def load_extraction_manifest(json_docs_dir: str) -> dict[str, Any]:
    """Load extraction manifest from json_documents/.manifest.json."""
    manifest_path = os.path.join(json_docs_dir, MANIFEST_FILENAME)
    if not os.path.exists(manifest_path):
        return {"files": {}, "last_sync": None}
    try:
        with open(manifest_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load manifest from %s: %s", manifest_path, e)
        return {"files": {}, "last_sync": None}


def save_extraction_manifest(json_docs_dir: str, manifest: dict[str, Any]) -> None:
    """Save extraction manifest to json_documents/.manifest.json."""
    manifest["last_sync"] = datetime.now(timezone.utc).isoformat() + "Z"
    manifest_path = os.path.join(json_docs_dir, MANIFEST_FILENAME)
    os.makedirs(json_docs_dir, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def update_file_in_manifest(
    manifest: dict[str, Any],
    filename: str,
    size: int,
    hash_md5: str,
    source_path: str,
    superseded: bool = False,
) -> None:
    """Update or add a file entry in the manifest."""
    manifest["files"][filename] = {
        "size": size,
        "hash_md5": hash_md5,
        "extracted_at": datetime.now(timezone.utc).isoformat() + "Z",
        "source_path": source_path,
        "superseded": superseded,
    }


# ── Manifest comparison ───────────────────────────────────────────────────────

def compare_manifests(
    existing: dict[str, dict],
    new: dict[str, dict],
) -> dict[str, str]:
    """Compare two file manifests.

    Args:
        existing: Manifest from original state.
        new: Manifest from newly downloaded files.

    Returns:
        Dict mapping filename -> "new" | "changed" | "deleted".
    """
    result: dict[str, str] = {}
    existing_filenames = set(existing.keys())
    new_filenames = set(new.keys())
    for fname in new_filenames:
        if fname not in existing_filenames:
            result[fname] = "new"
        else:
            old_entry = existing[fname]
            new_entry = new[fname]
            if old_entry.get("hash_md5") != new_entry.get("hash_md5"):
                result[fname] = "changed"
            elif old_entry.get("size") != new_entry.get("size"):
                result[fname] = "changed"
    for fname in existing_filenames:
        if fname not in new_filenames:
            result[fname] = "deleted"
    return result


def get_changed_files(
    existing_dir: str,
    new_files_dir: str,
    existing_manifest: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """Compare existing files against newly downloaded files.

    Args:
        existing_dir: Directory containing existing files.
        new_files_dir: Directory containing newly downloaded files.
        existing_manifest: Optional pre-loaded manifest for existing_dir.

    Returns:
        (changed_files, missing_files) - lists of relative paths.
    """
    if existing_manifest is None:
        existing_manifest = build_file_manifest(existing_dir)
    new_manifest = build_file_manifest(new_files_dir)
    comparison = compare_manifests(
        {fname: info for fname, info in existing_manifest.items() if info.get("hash_md5")},
        new_manifest,
    )
    changed = [fname for fname, status in comparison.items() if status in ("new", "changed")]
    missing = [fname for fname, status in comparison.items() if status == "deleted"]
    return changed, missing


# ── File sync operations ───────────────────────────────────────────────────────

def sync_files_to_directory(
    files: list[str],
    source_dir: str,
    target_dir: str,
    create_target: bool = True,
) -> list[str]:
    """Copy specified files from source_dir to target_dir.

    Args:
        files: List of relative paths to copy.
        source_dir: Source directory.
        target_dir: Target directory.
        create_target: If True, create target_dir if it doesn't exist.

    Returns:
        List of files that were successfully copied.
    """
    if not files:
        return []
    if create_target:
        os.makedirs(target_dir, exist_ok=True)
    copied: list[str] = []
    for fname in files:
        source_path = Path(source_dir) / fname
        target_path = Path(target_dir) / fname
        if not source_path.exists():
            logger.warning("Source file not found: %s", source_path)
            continue
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.exists():
                target_path.unlink()
            shutil.copy2(source_path, target_path)
            copied.append(fname)
            logger.info("Synced file: %s -> %s", fname, target_dir)
        except Exception as e:
            logger.error("Failed to sync file %s: %s", fname, e)
    return copied


def delete_files_from_directory(files: list[str], target_dir: str) -> list[str]:
    """Delete specified files from target_dir."""
    deleted: list[str] = []
    for fname in files:
        target_path = Path(target_dir) / fname
        try:
            if target_path.exists():
                target_path.unlink()
                deleted.append(fname)
                logger.info("Deleted file: %s", target_path)
        except Exception as e:
            logger.error("Failed to delete file %s: %s", fname, e)
    return deleted