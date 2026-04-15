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

KNOWN_RAW_EXTENSIONS = {
    ".pdf", ".xml", ".docx", ".xlsx", ".xls", ".pptx", ".doc", ".ppt",
    ".txt", ".csv", ".zip", ".crdownload"
}
METADATA_FILES = {"analysis.json", "_file_classifications.json"}
ANALYSIS_FLAG_FILES = {".analysis_failed", ".analysis_failed.conflict1", ".analysis_failed.conflict2"}


def is_raw_file(name: str) -> bool:
    if name in METADATA_FILES:
        return False
    if name in ANALYSIS_FLAG_FILES:
        return False
    if name.startswith("."):
        return False
    ext = Path(name).suffix.lower()
    return ext in KNOWN_RAW_EXTENSIONS


def is_extracted_json(name: str) -> bool:
    return Path(name).suffix.lower() == ".json" and not name.startswith(".")


def separate_tender_data(tender_path: Path, dry_run: bool = True) -> dict:
    results = {
        "tender_id": tender_path.name,
        "files_to_document_downloads": [],
        "extracted_jsons_to_extracted": [],
        "metadata_to_analysis": [],
        "nested_folders_to_remove": [],
        "actions": [],
        "skipped": [],
    }

    tender_data = tender_path / TENDER_DATA
    if not tender_data.exists():
        results["skipped"].append("tender_data does not exist")
        return results

    doc_dl = tender_data / DOCUMENT_DOWNLOADS
    extracted = tender_data / EXTRACTED_DOCUMENTS
    analysis_dir = tender_data / ANALYSIS
    json_old = tender_data / "json_documents"
    json_old_backup = tender_data / "json_documents_old"

    if doc_dl.exists():
        for f in doc_dl.iterdir():
            if f.is_dir():
                results["nested_folders_to_remove"].append(f.name)
                results["actions"].append(f"DELETE nested folder: {f.name} in document_downloads")
            elif is_raw_file(f.name):
                results["skipped"].append(f"raw file already in document_downloads: {f.name}")
            elif is_extracted_json(f.name):
                results["actions"].append(f"MOVE extracted JSON to extracted_documents/: {f.name}")
                results["extracted_jsons_to_extracted"].append(f.name)
            else:
                results["skipped"].append(f"unknown file in document_downloads: {f.name}")

    if extracted.exists():
        for f in extracted.iterdir():
            if f.is_dir():
                results["nested_folders_to_remove"].append(f"extracted_documents/{f.name}")
                results["actions"].append(f"DELETE nested folder in extracted_documents/: {f.name}")
            elif is_extracted_json(f.name):
                results["extracted_jsons_to_extracted"].append(f.name)
                results["actions"].append(f"KEEP in extracted_documents/: {f.name}")
            elif is_raw_file(f.name):
                results["files_to_document_downloads"].append(f.name)
                results["actions"].append(f"MOVE raw file to document_downloads/: {f.name}")
            else:
                results["skipped"].append(f"unknown file in extracted_documents: {f.name}")

    for source in [json_old, json_old_backup]:
        if source.exists() and source.is_dir():
            for f in source.iterdir():
                if f.is_file():
                    if f.name in METADATA_FILES or f.name in ANALYSIS_FLAG_FILES:
                        results["metadata_to_analysis"].append(f.name)
                        results["actions"].append(f"MOVE analysis metadata to analysis/: {f.name} ({source.name})")
                    elif is_extracted_json(f.name):
                        results["extracted_jsons_to_extracted"].append(f.name)
                        results["actions"].append(f"MOVE extracted JSON to extracted_documents/: {f.name} ({source.name})")
                    else:
                        results["skipped"].append(f"unknown file in {source.name}: {f.name}")
        elif source.exists() and source.is_file():
            f = source
            if f.name in METADATA_FILES or f.name in ANALYSIS_FLAG_FILES:
                results["metadata_to_analysis"].append(f.name)
                results["actions"].append(f"MOVE analysis metadata to analysis/: {f.name} ({source.name} [FILE])")
            elif is_extracted_json(f.name):
                results["extracted_jsons_to_extracted"].append(f.name)
                results["actions"].append(f"MOVE extracted JSON to extracted_documents/: {f.name} ({source.name} [FILE])")
            else:
                results["skipped"].append(f"unknown file as {source.name}: {f.name}")

    if not dry_run:
        if doc_dl.exists():
            for f in doc_dl.iterdir():
                if f.is_dir():
                    shutil.rmtree(f)

        if extracted.exists():
            extracted.mkdir(parents=True, exist_ok=True)
            for f in tender_data.iterdir():
                if f.is_dir() and f.name in (EXTRACTED_DOCUMENTS, "extracted_documents_bak"):
                    shutil.rmtree(f)

        analysis_dir.mkdir(parents=True, exist_ok=True)
        for source in [json_old, json_old_backup]:
            if source.exists():
                for f in source.iterdir():
                    if f.is_file():
                        if f.name in METADATA_FILES or f.name in ANALYSIS_FLAG_FILES:
                            _copy_file_long(f, analysis_dir / f.name)
                            f.unlink()
                        elif is_extracted_json(f.name):
                            extracted.mkdir(parents=True, exist_ok=True)
                            _copy_file_long(f, extracted / f.name)
                            f.unlink()

        for source in [json_old, json_old_backup]:
            if source.exists() and source.is_dir() and not any(source.iterdir()):
                shutil.rmtree(source)
            elif source.exists() and source.is_file():
                pass  # file was already moved in the loop above

    return results


def separate_email_clarifications(tender_path: Path, dry_run: bool = True) -> dict:
    results = {
        "tender_id": tender_path.name,
        "files_to_document_downloads": [],
        "extracted_jsons_to_extracted": [],
        "metadata_to_analysis": [],
        "clarification_jsons_to_analysis": [],
        "nested_folders_to_remove": [],
        "actions": [],
        "skipped": [],
    }

    clar_root = tender_path / EMAIL_UPDATES / CLARIFICATIONS
    if not clar_root.exists():
        results["skipped"].append("email_updates/clarifications does not exist")
        return results

    doc_dl = clar_root / DOCUMENT_DOWNLOADS
    extracted = clar_root / EXTRACTED_DOCUMENTS
    analysis_dir = clar_root / ANALYSIS

    files_at_root = [f for f in clar_root.iterdir() if f.is_file()]
    for f in files_at_root:
        if is_raw_file(f.name):
            results["files_to_document_downloads"].append(f.name)
            results["actions"].append(f"MOVE raw clarification to document_downloads/: {f.name}")
        elif f.name == "manifest.json":
            results["clarification_jsons_to_analysis"].append(f.name)
            results["actions"].append(f"MOVE manifest.json to analysis/: {f.name}")
        elif is_extracted_json(f.name):
            results["extracted_jsons_to_extracted"].append(f.name)
            results["actions"].append(f"MOVE extracted JSON to extracted_documents/: {f.name}")
        elif f.name in METADATA_FILES or f.name in ANALYSIS_FLAG_FILES:
            results["metadata_to_analysis"].append(f.name)
            results["actions"].append(f"MOVE metadata to analysis/: {f.name}")
        else:
            results["skipped"].append(f"unknown file at clarifications root: {f.name}")

    if doc_dl.exists():
        for f in doc_dl.iterdir():
            if f.is_dir():
                results["nested_folders_to_remove"].append(f"clarifications/document_downloads/{f.name}")
                results["actions"].append(f"DELETE nested folder: document_downloads/{f.name}")
            elif is_raw_file(f.name):
                results["files_to_document_downloads"].append(f.name)
                results["actions"].append(f"MOVE raw to document_downloads/: {f.name}")
            elif is_extracted_json(f.name):
                results["extracted_jsons_to_extracted"].append(f.name)
                results["actions"].append(f"MOVE extracted JSON to extracted_documents/: {f.name}")
            else:
                results["skipped"].append(f"unknown file in clarifications/document_downloads: {f.name}")

    if extracted.exists():
        for f in extracted.iterdir():
            if f.is_dir():
                results["nested_folders_to_remove"].append(f"clarifications/extracted_documents/{f.name}")
                results["actions"].append(f"DELETE nested folder: extracted_documents/{f.name}")
            elif is_extracted_json(f.name):
                results["extracted_jsons_to_extracted"].append(f.name)
                results["actions"].append(f"KEEP in extracted_documents/: {f.name}")
            else:
                results["skipped"].append(f"unknown file in clarifications/extracted_documents: {f.name}")

    if not dry_run:
        doc_dl.mkdir(parents=True, exist_ok=True)
        extracted.mkdir(parents=True, exist_ok=True)
        analysis_dir.mkdir(parents=True, exist_ok=True)

        for f in files_at_root:
            if is_raw_file(f.name):
                _copy_file_long(f, doc_dl / f.name)
                f.unlink()
            elif f.name == "manifest.json":
                _copy_file_long(f, analysis_dir / f.name)
                f.unlink()
            elif is_extracted_json(f.name):
                _copy_file_long(f, extracted / f.name)
                f.unlink()

        if doc_dl.exists():
            for f in doc_dl.iterdir():
                if f.is_dir():
                    shutil.rmtree(f)

        if extracted.exists():
            for f in extracted.iterdir():
                if f.is_dir():
                    shutil.rmtree(f)

    return results


def main(dry_run: bool = True):
    tender_folders = sorted(
        d for d in DOCUMENTS_ROOT.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )

    mode = "DRY RUN" if dry_run else "LIVE RUN"
    print(f"{mode} — {len(tender_folders)} tender folders\n")

    for tf in tender_folders:
        r1 = separate_tender_data(tf, dry_run=dry_run)
        r2 = separate_email_clarifications(tf, dry_run=dry_run)

        all_actions = r1["actions"] + r2["actions"]
        if not all_actions and not r1["skipped"] and not r2["skipped"]:
            continue

        print(f"\n{'='*60}")
        print(f"Tender: {tf.name}")
        if r1["skipped"]:
            print(f"  tender_data SKIPPED ({len(r1['skipped'])}): {r1['skipped'][:3]}...")
        if r2["skipped"]:
            print(f"  clarifications SKIPPED ({len(r2['skipped'])}): {r2['skipped'][:3]}...")
        if all_actions:
            print(f"  ACTIONS ({len(all_actions)}):")
            for a in all_actions[:10]:
                print(f"    {a}")
            if len(all_actions) > 10:
                print(f"    ... and {len(all_actions) - 10} more")

    print(f"\n{'DRY RUN COMPLETE' if dry_run else 'SEPARATION COMPLETE'}")
    if dry_run:
        print("Re-run with --live to execute the separation.")


if __name__ == "__main__":
    import sys
    dry = "--live" not in sys.argv
    main(dry_run=dry)
