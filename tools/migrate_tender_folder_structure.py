import shutil
import json
import hashlib
import os
import re
import zipfile
from pathlib import Path


def _copy_file_long(src: Path, dst: Path) -> bool:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    except OSError:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(src, "rb") as fsrc:
                with open(dst, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
            return True
        except OSError:
            return False

DOCUMENTS_ROOT = Path(r"C:\Users\Administrator\Desktop\projects\gojep\data\tenders\documents")

TENDER_DATA = "tender_data"
EMAIL_UPDATES = "email_updates"
DOCUMENT_DOWNLOADS = "document_downloads"
EXTRACTED_DOCUMENTS = "extracted_documents"
ANALYSIS = "analysis"
CLARIFICATIONS = "clarifications"
NEW_DOCUMENTS = "new_documents"
MODIFICATIONS = "modifications"

KNOWN_EXTENSIONS = {
    ".pdf", ".xml", ".docx", ".xlsx", ".xls", ".pptx", ".doc", ".ppt",
    ".txt", ".csv", ".json", ".zip", ".crdownload", ".pb", ".tflite", ".txt"
}
METADATA_FILES = {"analysis.json", "_file_classifications.json"}
ANALYSIS_FLAG_FILES = {".analysis_failed", ".analysis_failed.conflict1", ".analysis_failed.conflict2"}


def compute_md5(path: Path) -> str:
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5.update(chunk)
    return md5.hexdigest()


def is_downloaded_file(name: str) -> bool:
    if name in METADATA_FILES:
        return False
    if name in ANALYSIS_FLAG_FILES:
        return False
    if name.startswith(".") and not name.startswith(".analysis"):
        return False
    ext = Path(name).suffix.lower()
    return ext in KNOWN_EXTENSIONS


def migrate_tender(tender_path: Path, dry_run: bool = True) -> dict:
    results = {
        "tender_id": tender_path.name,
        "tender_files_moved": [],
        "extracted_moved": [],
        "metadata_moved": [],
        "analysis_flags_moved": [],
        "clarification_files_moved": [],
        "clarification_extracted_moved": [],
        "email_metadata_moved": [],
        "skipped": [],
    }

    tender_data_dir = tender_path / TENDER_DATA
    email_updates_dir = tender_path / EMAIL_UPDATES

    existing = tender_data_dir / DOCUMENT_DOWNLOADS
    if existing.exists():
        results["skipped"].append("tender_data already exists")
        return results

    for item in tender_path.iterdir():
        name = item.name

        if item.is_dir():
            if name == "extracted_docs":
                target = tender_data_dir / EXTRACTED_DOCUMENTS
                for f in item.iterdir():
                    results["extracted_moved"].append(f.name)
                if not dry_run:
                    target.mkdir(parents=True, exist_ok=True)
                    for f in item.iterdir():
                        _copy_file_long(f, target / f.name)
            elif name == "clarifications":
                clar_files_dir = email_updates_dir / CLARIFICATIONS / DOCUMENT_DOWNLOADS
                clar_extracted_dir = email_updates_dir / CLARIFICATIONS / EXTRACTED_DOCUMENTS
                clar_json_dir = email_updates_dir / CLARIFICATIONS / ANALYSIS

                for f in item.iterdir():
                    if f.is_dir():
                        if f.name == "extracted_docs":
                            for ef in f.iterdir():
                                results["clarification_extracted_moved"].append(ef.name)
                            if not dry_run:
                                clar_extracted_dir.mkdir(parents=True, exist_ok=True)
                                for ef in f.iterdir():
                                    _copy_file_long(ef, clar_extracted_dir / ef.name)
                        else:
                            results["skipped"].append(f"unknown dir in clarifications: {f.name}")
                    else:
                        if f.name == "manifest.json":
                            results["email_metadata_moved"].append(f.name)
                            if not dry_run:
                                clar_json_dir.mkdir(parents=True, exist_ok=True)
                                _copy_file_long(f, clar_json_dir / f.name)
                        elif is_downloaded_file(f.name):
                            results["clarification_files_moved"].append(f.name)
                            if not dry_run:
                                clar_files_dir.mkdir(parents=True, exist_ok=True)
                                _copy_file_long(f, clar_files_dir / f.name)
                        else:
                            results["skipped"].append(f"unknown clarification file: {f.name}")
            elif name in (NEW_DOCUMENTS, MODIFICATIONS):
                em_files_dir = email_updates_dir / name / DOCUMENT_DOWNLOADS
                em_extracted_dir = email_updates_dir / name / EXTRACTED_DOCUMENTS
                em_json_dir = email_updates_dir / name / ANALYSIS

                for f in item.iterdir():
                    if f.is_dir():
                        if f.name == "extracted_docs":
                            for ef in f.iterdir():
                                if not dry_run:
                                    em_extracted_dir.mkdir(parents=True, exist_ok=True)
                                    _copy_file_long(ef, em_extracted_dir / ef.name)
                        else:
                            results["skipped"].append(f"unknown dir in {name}: {f.name}")
                    else:
                        if f.name == "manifest.json":
                            results["email_metadata_moved"].append(f"{name}/{f.name}")
                            if not dry_run:
                                em_json_dir.mkdir(parents=True, exist_ok=True)
                                _copy_file_long(f, em_json_dir / f.name)
                        elif is_downloaded_file(f.name):
                            results["skipped"].append(f"{name} file not migrated: {f.name}")
                        else:
                            results["skipped"].append(f"unknown {name} file: {f.name}")
            else:
                results["skipped"].append(f"unknown directory: {name}")

        elif item.is_file():
            if name in METADATA_FILES:
                results["metadata_moved"].append(name)
                if not dry_run:
                    td = tender_data_dir / ANALYSIS
                    td.mkdir(parents=True, exist_ok=True)
                    _copy_file_long(item, td / name)
            elif name in ANALYSIS_FLAG_FILES:
                results["analysis_flags_moved"].append(name)
                if not dry_run:
                    td = tender_data_dir / ANALYSIS
                    td.mkdir(parents=True, exist_ok=True)
                    _copy_file_long(item, td / name)
            elif is_downloaded_file(name):
                results["tender_files_moved"].append(name)
                if not dry_run:
                    td = tender_data_dir / DOCUMENT_DOWNLOADS
                    td.mkdir(parents=True, exist_ok=True)
                    _copy_file_long(item, td / name)
            else:
                results["skipped"].append(f"unknown file: {name}")

    return results


def restructure_tender(tender_path: Path, dry_run: bool = True) -> dict:
    results = {
        "tender_id": tender_path.name,
        "extracted_restructured": [],
        "json_restructured": [],
        "nested_deleted": [],
        "skipped": [],
    }

    tender_data = tender_path / TENDER_DATA
    doc_dl = tender_data / DOCUMENT_DOWNLOADS
    nested_extracted = doc_dl / EXTRACTED_DOCUMENTS
    nested_json = doc_dl / ANALYSIS

    if doc_dl.exists():
        if nested_extracted.exists():
            for f in nested_extracted.iterdir():
                results["extracted_restructured"].append(f.name)
                if not dry_run:
                    target = tender_data / EXTRACTED_DOCUMENTS
                    target.mkdir(parents=True, exist_ok=True)
                    _copy_file_long(f, target / f.name)
            if not dry_run:
                shutil.rmtree(nested_extracted)
                results["nested_deleted"].append(EXTRACTED_DOCUMENTS)

        if nested_json.exists():
            for f in nested_json.iterdir():
                results["json_restructured"].append(f.name)
                if not dry_run:
                    target = tender_data / ANALYSIS
                    target.mkdir(parents=True, exist_ok=True)
                    _copy_file_long(f, target / f.name)
            if not dry_run:
                shutil.rmtree(nested_json)
                results["nested_deleted"].append(ANALYSIS)
    else:
        results["skipped"].append("document_downloads not found")

    return results


def flatten_and_extract_documents(tender_path: Path, dry_run: bool = True) -> dict:
    results = {
        "tender_id": tender_path.name,
        "nested_folders_removed": [],
        "zips_extracted": [],
        "raw_files_found": [],
        "extracted_cleaned": [],
        "skipped": [],
    }

    tender_data = tender_path / TENDER_DATA
    doc_dl = tender_data / DOCUMENT_DOWNLOADS

    if not doc_dl.exists():
        results["skipped"].append("document_downloads not found")
        return results

    extracted_dir = tender_data / EXTRACTED_DOCUMENTS
    if extracted_dir.exists():
        if not dry_run:
            extracted_dir.rename(tender_data / (EXTRACTED_DOCUMENTS + "_bak"))
        results["extracted_cleaned"].append("renamed extracted_documents to backup")

    def flatten_dir(dir_path: Path) -> list[str]:
        extracted_zips = []
        raw_files = []
        queue = [dir_path]
        while queue:
            current = queue.pop(0)
            for item in current.iterdir():
                if item.is_dir():
                    queue.append(item)
                elif item.suffix.lower() == ".zip":
                    results["zips_extracted"].append(item.name)
                    if not dry_run:
                        try:
                            with zipfile.ZipFile(item, "r") as zf:
                                for member in zf.infolist():
                                    if member.is_dir():
                                        continue
                                    safe_name = re.sub(r'[<>:"/\\|?*]', "_", os.path.basename(member.filename))
                                    dest = dir_path / safe_name
                                    with zf.open(member) as src, open(dest, "wb") as dst:
                                        dst.write(src.read())
                                    raw_files.append(safe_name)
                            item.unlink()
                        except (zipfile.BadZipFile, OSError, PermissionError):
                            results["skipped"].append(f"bad zip: {item.name}")
                elif is_downloaded_file(item.name):
                    raw_files.append(item.name)

        for item in dir_path.iterdir():
            if item.is_dir():
                results["nested_folders_removed"].append(str(item.relative_to(doc_dl)))
                if not dry_run:
                    shutil.rmtree(item)

        return raw_files

    raw_files = flatten_dir(doc_dl)
    results["raw_files_found"] = raw_files

    return results
    results = {
        "tender_id": tender_path.name,
        "files_promoted": [],
        "extracted_promoted": [],
        "json_promoted": [],
        "nested_deleted": [],
        "skipped": [],
    }

    email_clar = tender_path / EMAIL_UPDATES / CLARIFICATIONS

    nested_dd = email_clar / DOCUMENT_DOWNLOADS
    nested_extracted = nested_dd / EXTRACTED_DOCUMENTS
    nested_json = nested_dd / ANALYSIS

    if not nested_dd.exists():
        results["skipped"].append("clarifications/document_downloads not found")
        return results

    if nested_extracted.exists():
        for f in nested_extracted.iterdir():
            results["extracted_promoted"].append(f.name)
            if not dry_run:
                target = email_clar / EXTRACTED_DOCUMENTS
                target.mkdir(parents=True, exist_ok=True)
                _copy_file_long(f, target / f.name)
        if not dry_run:
            shutil.rmtree(nested_extracted)
            results["nested_deleted"].append("extracted_documents (nested)")

    if nested_json.exists():
        for f in nested_json.iterdir():
            results["json_promoted"].append(f.name)
            if not dry_run:
                target = email_clar / ANALYSIS
                target.mkdir(parents=True, exist_ok=True)
                _copy_file_long(f, target / f.name)
        if not dry_run:
            shutil.rmtree(nested_json)
            results["nested_deleted"].append("json_documents (nested)")

    known_exts = {
        ".pdf", ".xml", ".docx", ".xlsx", ".xls", ".pptx", ".doc", ".ppt",
        ".txt", ".csv", ".zip", ".crdownload"
    }
    for f in nested_dd.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() in known_exts or f.name == "manifest.json":
            results["files_promoted"].append(f.name)
            if not dry_run:
                _copy_file_long(f, email_clar / f.name)
        else:
            results["skipped"].append(f"unknown file: {f.name}")

    if not dry_run:
        shutil.rmtree(nested_dd)
        results["nested_deleted"].append("document_downloads (nested)")

    return results


def fixup_clarification_location(tender_path: Path, dry_run: bool = True) -> dict:
    results = {
        "tender_id": tender_path.name,
        "clarification_files_moved": [],
        "clarification_extracted_moved": [],
        "clarification_json_moved": [],
        "skipped": [],
    }

    tender_data = tender_path / TENDER_DATA
    email_clar_dir = tender_path / EMAIL_UPDATES / CLARIFICATIONS
    email_clar_docs = email_clar_dir / DOCUMENT_DOWNLOADS
    email_clar_extracted = email_clar_dir / EXTRACTED_DOCUMENTS
    email_clar_json = email_clar_dir / ANALYSIS

    old_clar_extracted = tender_data / EXTRACTED_DOCUMENTS
    if old_clar_extracted.exists():
        for f in old_clar_extracted.iterdir():
            if f.suffix == ".json" and "_clarification" in f.stem.lower():
                results["clarification_extracted_moved"].append(f.name)
                if not dry_run:
                    email_clar_extracted.mkdir(parents=True, exist_ok=True)
                    _copy_file_long(f, email_clar_extracted / f.name)
            else:
                results["skipped"].append(f"non-clarification extracted: {f.name}")

    old_clar_files_dir = tender_data / DOCUMENT_DOWNLOADS
    if old_clar_files_dir.exists():
        known_clar_names = {
            "1020_9119174.pdf",
            "1_Redesign and Data Migration-Clarification Request and Responses No. 2.docx",
            "2_MOHW- Redesign and Data Migration of ICT Network - POA Template.docx",
            "3_MOHW- Redesign and Data Migration of ICT Network - POA Template.docx",
        }
        for f in old_clar_files_dir.iterdir():
            if f.is_file() and f.name in known_clar_names:
                results["clarification_files_moved"].append(f.name)
                if not dry_run:
                    email_clar_docs.mkdir(parents=True, exist_ok=True)
                    _copy_file_long(f, email_clar_docs / f.name)
            elif f.is_file() and is_downloaded_file(f.name):
                results["skipped"].append(f"unknown tender file: {f.name}")

    old_clar_json = tender_data / ANALYSIS
    if old_clar_json.exists():
        for f in old_clar_json.iterdir():
            if f.name == "manifest.json":
                results["clarification_json_moved"].append(f.name)
                if not dry_run:
                    email_clar_json.mkdir(parents=True, exist_ok=True)
                    _copy_file_long(f, email_clar_json / f.name)
            elif f.name not in METADATA_FILES and f.name not in ANALYSIS_FLAG_FILES:
                results["skipped"].append(f"unknown json file: {f.name}")

    return results


def cleanup_tender(tender_path: Path, dry_run: bool = True) -> dict:
    results = {
        "tender_id": tender_path.name,
        "deleted_dirs": [],
        "deleted_files": [],
        "errors": [],
    }

    tender_data_dir = tender_path / TENDER_DATA
    if not tender_data_dir.exists():
        results["errors"].append("tender_data not found, skipping cleanup")
        return results

    old_dirs = ["extracted_docs", "clarifications"]
    for d in old_dirs:
        p = tender_path / d
        if p.exists():
            results["deleted_dirs"].append(d)
            if not dry_run:
                shutil.rmtree(p)

    old_email_docs = tender_path / EMAIL_UPDATES / CLARIFICATIONS / DOCUMENT_DOWNLOADS
    if old_email_docs.exists():
        results["deleted_dirs"].append("email_updates/clarifications/document_downloads")
        if not dry_run:
            shutil.rmtree(old_email_docs)

    migrated_exts = {
        ".pdf", ".xml", ".docx", ".xlsx", ".xls", ".pptx", ".doc", ".ppt",
        ".txt", ".csv", ".zip", ".crdownload"
    }
    for f in tender_path.iterdir():
        if not f.is_file():
            continue
        name = f.name
        if name in METADATA_FILES or name in ANALYSIS_FLAG_FILES:
            results["deleted_files"].append(name)
            if not dry_run:
                f.unlink()
        elif f.suffix.lower() in migrated_exts:
            results["deleted_files"].append(name)
            if not dry_run:
                f.unlink()

    return results


def main(dry_run: bool = True, cleanup_only: bool = False, restructure_only: bool = False, fixup_only: bool = False, flatten_only: bool = False, flatten_extract_only: bool = False):
    tender_folders = sorted(
        d for d in DOCUMENTS_ROOT.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )

    if flatten_extract_only:
        mode = "FLATTEN-EXTRACT DRY RUN" if dry_run else "FLATTEN-EXTRACT LIVE"
    elif flatten_only:
        mode = "FLATTEN DRY RUN" if dry_run else "FLATTEN LIVE"
    elif fixup_only:
        mode = "FIXUP DRY RUN" if dry_run else "FIXUP LIVE"
    elif restructure_only:
        mode = "RESTRUCTURE DRY RUN" if dry_run else "RESTRUCTURE LIVE"
    elif cleanup_only:
        mode = "CLEANUP DRY RUN" if dry_run else "CLEANUP LIVE"
    else:
        mode = "DRY RUN" if dry_run else "LIVE RUN"
    print(f"{mode} — {len(tender_folders)} tender folders\n")

    for tf in tender_folders:
        if flatten_extract_only:
            r = flatten_and_extract_documents(tf, dry_run=dry_run)
        elif flatten_only:
            r = flatten_clarification_email(tf, dry_run=dry_run)
        elif fixup_only:
            r = fixup_clarification_location(tf, dry_run=dry_run)
        elif restructure_only:
            r = restructure_tender(tf, dry_run=dry_run)
        elif cleanup_only:
            r = cleanup_tender(tf, dry_run=dry_run)
        else:
            r = migrate_tender(tf, dry_run=dry_run)

        print(f"\n{'='*60}")
        print(f"Tender: {r['tender_id']}")
        if r.get("errors"):
            print(f"  ERRORS: {r['errors']}")
        if flatten_extract_only:
            if r.get("nested_folders_removed"):
                print(f"  Nested folders removed: {len(r['nested_folders_removed'])}")
            if r.get("zips_extracted"):
                print(f"  Zips extracted: {len(r['zips_extracted'])}")
            if r.get("raw_files_found"):
                print(f"  Raw files found: {len(r['raw_files_found'])}")
            if r.get("extracted_cleaned"):
                print(f"  Extracted cleaned: {len(r['extracted_cleaned'])}")
            if r.get("skipped"):
                print(f"  Skipped: {len(r['skipped'])}")
        elif flatten_only:
            if r.get("files_promoted"):
                print(f"  Files promoted: {len(r['files_promoted'])}")
            if r.get("extracted_promoted"):
                print(f"  Extracted promoted: {len(r['extracted_promoted'])}")
            if r.get("json_promoted"):
                print(f"  Json promoted: {len(r['json_promoted'])}")
            if r.get("nested_deleted"):
                print(f"  Deleted: {r['nested_deleted']}")
            if r.get("skipped"):
                print(f"  Skipped: {len(r['skipped'])}")
        elif fixup_only:
            if r.get("clarification_files_moved"):
                print(f"  Clarification files: {len(r['clarification_files_moved'])}")
            if r.get("clarification_extracted_moved"):
                print(f"  Clarification extracted: {len(r['clarification_extracted_moved'])}")
            if r.get("clarification_json_moved"):
                print(f"  Clarification json: {len(r['clarification_json_moved'])}")
            if r.get("skipped"):
                print(f"  Skipped: {len(r['skipped'])}")
        elif restructure_only:
            if r.get("extracted_restructured"):
                print(f"  Extracted restructured: {len(r['extracted_restructured'])}")
            if r.get("json_restructured"):
                print(f"  Json restructured: {len(r['json_restructured'])}")
            if r.get("nested_deleted"):
                print(f"  Deleted nested: {r['nested_deleted']}")
        elif cleanup_only:
            if r.get("deleted_dirs"):
                print(f"  Deleted dirs: {r['deleted_dirs']}")
            if r.get("deleted_files"):
                print(f"  Deleted files: {len(r['deleted_files'])}")
        else:
            if r["skipped"]:
                print(f"  SKIPPED ({len(r['skipped'])}): {r['skipped']}")
            if r["tender_files_moved"]:
                print(f"  Tender files: {len(r['tender_files_moved'])}")
            if r["extracted_moved"]:
                print(f"  Extracted docs: {len(r['extracted_moved'])}")
            if r["metadata_moved"]:
                print(f"  Metadata files: {r['metadata_moved']}")
            if r["analysis_flags_moved"]:
                print(f"  Analysis flags: {r['analysis_flags_moved']}")
            if r["clarification_files_moved"]:
                print(f"  Clarification files: {len(r['clarification_files_moved'])}")
            if r["clarification_extracted_moved"]:
                print(f"  Clarification extracted: {len(r['clarification_extracted_moved'])}")
            if r.get("email_metadata_moved"):
                print(f"  Email metadata: {r['email_metadata_moved']}")

    print(f"\n{'DRY RUN COMPLETE' if dry_run else 'COMPLETE'}")
    if dry_run and flatten_only:
        print("Re-run with --live --flatten to execute the flatten.")
    if dry_run and fixup_only:
        print("Re-run with --live --fixup to execute the fixup.")
    if dry_run and restructure_only:
        print("Re-run with --live --restructure to execute the restructure.")
    if dry_run and cleanup_only:
        print("Re-run with --live --cleanup to execute the cleanup.")


if __name__ == "__main__":
    import sys
    cleanup_only = "--cleanup" in sys.argv
    restructure_only = "--restructure" in sys.argv
    fixup_only = "--fixup" in sys.argv
    flatten_only = "--flatten" in sys.argv
    flatten_extract_only = "--flatten-extract" in sys.argv
    dry = "--live" not in sys.argv
    main(dry_run=dry, cleanup_only=cleanup_only, restructure_only=restructure_only, fixup_only=fixup_only, flatten_only=flatten_only, flatten_extract_only=flatten_extract_only)
