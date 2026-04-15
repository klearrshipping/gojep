"""
CLI commands for the Email Update Pipeline.

Commands:
  process-email-updates   Full pipeline: fetch → parse → match → action → mark processed
  review-email-updates    Print a summary of all fetched+parsed emails (dry-run friendly)

Pipeline per email (process-email-updates):

  1. Fetch unprocessed emails via Gmail API
  2. Parse each email (Path A: regex / Path B: LLM)
  3. Skip informational-only types (time_limit, clarification_period_end)
  4. Match resource_id to DB record in gojep_tenders_all / gojep_tenders_current
  5. Branch by action_url_type:
       list_clarification  → download new clarification attachments (Selenium)
       prepare_view        → re-fetch tender detail page (TenderDetailExtractor)
       None (Path B)       → date field patch only
  6. Mark email as processed in Gmail ('tender-update-processed' label)
  7. Print summary
"""

from __future__ import annotations

import argparse
import json
import logging

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

_NEXT_ACTION_LABEL: dict[str, str] = {
    "clarification_response":  "download clarification documents",
    "new_documents":           "download contract documents",
    "modifications":           "re-fetch tender detail page",
    "deadline_extension":      "re-fetch tender detail page (deadline update)",
    "cancellation":            "remove from active tenders",
    "addendum":                "download addendum documents",
    "site_visit":              "patch date fields",
    "clarification_period_end":"informational — no action",
    "time_limit":              "informational — no action",
    "other":                   "no action",
}


def _next_action(update_type: str, action_url_type: str | None, path: str) -> str:
    if action_url_type == "list_clarification":
        return "download clarification documents"
    if action_url_type == "prepare_view":
        return "re-fetch tender detail page"
    if path == "B":
        return _NEXT_ACTION_LABEL.get(update_type, "patch date fields (Path B)")
    return _NEXT_ACTION_LABEL.get(update_type, "no action")


def _match_resource_id_to_db(resource_id: str, db) -> dict | None:
    """
    Look up a resource_id in gojep_tenders_current only.
    If not found, the tender is no longer active — caller should discard.
    Returns the DB row dict or None.
    """
    from config import settings as config

    try:
        result = (
            db.supabase.table(config.SUPABASE_TABLE_TENDERS_CURRENT)
            .select("resource_id, competition_unique_id, title, submission_deadline, detail_url")
            .eq("resource_id", resource_id)
            .limit(1)
            .execute()
        )
        if result.data:
            row = result.data[0]
            row["_source_table"] = config.SUPABASE_TABLE_TENDERS_CURRENT
            return row
    except Exception as e:
        logger.warning("DB lookup failed for resource_id=%s: %s", resource_id, e)

    return None


def _handle_list_clarification(parsed: dict, db_row: dict, dry_run: bool) -> dict:
    """
    Click "Download all clarifications" for the matched tender, extract the ZIP,
    and upload new files to Supabase Storage.
    Requires a logged-in Selenium session.
    """
    from config import settings as config

    resource_id           = parsed["resource_id"]
    competition_unique_id = db_row.get("competition_unique_id") or resource_id

    if dry_run:
        logger.info(
            "  [dry-run] Would scrape clarification page for resourceId=%s", resource_id
        )
        return {"action": "list_clarification", "dry_run": True, "resource_id": resource_id}

    from modules.tenders.get_tenders import GOJEPScraper
    from modules.emails.get_clarification_documents import download_new_clarification_attachments
    import os

    docs_base_dir = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "documents")

    print(f"  Launching browser (headless) ...")
    print(f"  Navigating to clarification page for resourceId={resource_id} ...")
    scraper = GOJEPScraper()
    scraper._setup_driver(headless=True)
    try:
        result = download_new_clarification_attachments(
            driver=scraper.driver,
            resource_id=resource_id,
            competition_unique_id=competition_unique_id,
            docs_base_dir=docs_base_dir,
        )
    finally:
        scraper.driver.quit()

    files = result.get("downloaded_files", [])
    skipped = result.get("skipped_already_uploaded", 0)
    print(f"  Downloaded {len(files)} file(s), skipped {skipped} already uploaded.")
    if files:
        for f in files:
            print(f"    + {f}")

    return {"action": "list_clarification", **result}


def _handle_new_documents(parsed: dict, db_row: dict, dry_run: bool) -> dict:
    """
    Navigate directly to listContractDocuments.do (URL from the email),
    click "Download Zip file", extract and upload new files to Supabase Storage.
    """
    from config import settings as config

    resource_id           = parsed["resource_id"]
    competition_unique_id = db_row.get("competition_unique_id") or resource_id
    action_url            = parsed.get("action_url") or (
        f"{config.GOJEP_BASE_URL}/epps/cft/listContractDocuments.do?resourceId={resource_id}"
    )

    if dry_run:
        logger.info("  [dry-run] Would download contract documents for resourceId=%s", resource_id)
        return {"action": "new_documents", "dry_run": True, "resource_id": resource_id, "url": action_url}

    from modules.tenders.get_tenders import GOJEPScraper
    from modules.tenders.get_tender_documents import download_contract_documents_direct
    import os

    docs_base_dir = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "documents")

    print(f"  Launching browser (headless) ...")
    print(f"  Navigating to: {action_url}")
    scraper = GOJEPScraper()
    scraper._setup_driver(headless=True)
    try:
        result = download_contract_documents_direct(
            driver=scraper.driver,
            resource_id=resource_id,
            competition_unique_id=competition_unique_id,
            action_url=action_url,
            docs_base_dir=docs_base_dir,
            save_to_email_updates=True,
        )
    finally:
        scraper.driver.quit()

    files = result.get("downloaded_files", [])
    skipped = result.get("skipped_already_uploaded", 0)
    print(f"  Downloaded {len(files)} file(s), skipped {skipped} already uploaded.")
    if files:
        for f in files:
            print(f"    + {f}")

    return {"action": "new_documents", **result}


def _assess_new_files(downloaded_files: list[str], resource_id: str, update_type: str) -> dict:
    """
    Call the VLM assessor to determine whether newly downloaded files
    warrant a full re-analysis. Falls back to needs_reanalysis=True on any error.
    """
    from modules.emails.assess_new_documents import assess_new_documents_for_reanalysis
    try:
        return assess_new_documents_for_reanalysis(
            new_file_paths=downloaded_files,
            resource_id=resource_id,
            update_type=update_type,
        )
    except Exception as e:
        logger.warning("  VLM assessment error for %s: %s — defaulting to re-analysis", resource_id, e)
        return {"needs_reanalysis": True, "reason": f"assessment error: {e}", "assessed_files": []}


# Fields that are administrative only — a change here does not warrant re-analysis
_MINOR_FIELDS = {
    "submission_deadline", "submission_deadline_parsed", "original_submission_deadline",
    "clarification_period_end", "bid_opening_date", "site_visit_date",
    "bid_deadline_days_remaining", "bid_deadline_hours_remaining",
    "extraction_timestamp", "detail_url", "detail_page_extracted",
}


def _save_tender_details_json(detail: dict, competition_unique_id: str) -> None:
    """Save the raw fetched detail to <tender_folder>/tender_data/tender_details.json."""
    import os
    import re
    from config import settings as config

    safe_id = re.sub(r"[^\w\-.]+", "_", str(competition_unique_id))
    folder = os.path.join(
        config.TENDERS_OUTPUT_DIRECTORY, "documents", safe_id, "tender_data"
    )
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "tender_details.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    logger.info("  Saved tender details JSON: %s", path)


def _handle_prepare_view(parsed: dict, db_row: dict, db, dry_run: bool) -> dict:
    """
    Re-fetch the tender detail page (prepareViewCfTWS.do) to pick up
    any field changes (deadline extension, modifications etc.).

    Saves the fetched detail as tender_details.json in the tender folder.
    Classifies changed fields as minor (dates/admin) or substantive (scope/
    description/evaluation) and sets needs_reanalysis accordingly.
    """
    from config import settings as config
    from db.tenders.tender_row_mapping import fields_to_tender_patch
    from modules.tenders.get_tender_details import TenderDetailExtractor

    resource_id = parsed["resource_id"]
    detail_url  = parsed["action_url"] or db_row.get("detail_url") or (
        f"{config.GOJEP_BASE_URL}/epps/cft/prepareViewCfTWS.do?resourceId={resource_id}"
    )

    if dry_run:
        logger.info("  [dry-run] Would re-fetch detail page: %s", detail_url)
        return {"action": "prepare_view", "dry_run": True, "url": detail_url}

    extractor = TenderDetailExtractor()
    try:
        detail = extractor._extract_one(detail_url)
    except Exception as e:
        logger.error("  Detail page re-fetch failed for %s: %s", resource_id, e)
        return {"action": "prepare_view", "error": str(e)}

    if not detail or not detail.get("fields"):
        return {"action": "prepare_view", "error": "no fields returned"}

    patch = fields_to_tender_patch(detail["fields"], detail)

    # Detect what actually changed vs the DB row
    changed_fields = [
        k for k, v in patch.items()
        if v is not None and str(v) != str(db_row.get(k, ""))
    ]
    logger.info("  Changed fields: %s", changed_fields)

    # Save fetched detail as JSON to the tender folder
    cuid = db_row.get("competition_unique_id") or patch.get("competition_unique_id")
    if cuid:
        try:
            _save_tender_details_json(detail, cuid)
        except Exception as e:
            logger.warning("  Could not save tender_details.json for %s: %s", resource_id, e)

    # Classify: are any of the changed fields substantive?
    substantive_changes = [f for f in changed_fields if f not in _MINOR_FIELDS]
    needs_reanalysis = bool(substantive_changes)
    logger.info(
        "  Substantive changes: %s — re-analysis %s",
        substantive_changes or "none",
        "required" if needs_reanalysis else "not required",
    )

    # Apply patch to both tables
    for table in [config.SUPABASE_TABLE_TENDERS_CURRENT, config.SUPABASE_TABLE_TENDERS_ALL]:
        try:
            db.supabase.table(table).update(patch).eq("resource_id", resource_id).execute()
        except Exception as e:
            logger.warning("  Patch failed on %s for %s: %s", table, resource_id, e)

    return {
        "action":               "prepare_view",
        "resource_id":          resource_id,
        "fields_changed":       changed_fields,
        "substantive_changes":  substantive_changes,
        "needs_reanalysis":     needs_reanalysis,
        "patch_applied":        patch,
    }


def _handle_path_b_patch(parsed: dict, db_row: dict, db, dry_run: bool) -> dict:
    """
    Apply date field patches from a direct entity email (Path B).
    Only updates fields where LLM extracted a non-null date.
    """
    from config import settings as config
    from db.tenders.tender_row_mapping import parse_timestamp_field

    resource_id = db_row["resource_id"]
    extracted   = parsed.get("extracted_dates", {})

    patch: dict = {}
    if extracted.get("submission_deadline"):
        ts = parse_timestamp_field(extracted["submission_deadline"])
        if ts:
            patch["submission_deadline"]        = ts
            patch["submission_deadline_parsed"] = ts
    if extracted.get("site_visit_date"):
        ts = parse_timestamp_field(extracted["site_visit_date"])
        if ts:
            patch["site_visit_date"] = ts
    if extracted.get("bid_opening_date"):
        ts = parse_timestamp_field(extracted["bid_opening_date"])
        if ts:
            patch["bid_opening_date"] = ts

    if not patch:
        return {"action": "path_b_patch", "resource_id": resource_id, "fields_changed": []}

    if dry_run:
        logger.info("  [dry-run] Would patch %s with: %s", resource_id, list(patch.keys()))
        return {"action": "path_b_patch", "dry_run": True, "fields_changed": list(patch.keys())}

    for table in [config.SUPABASE_TABLE_TENDERS_CURRENT, config.SUPABASE_TABLE_TENDERS_ALL]:
        try:
            db.supabase.table(table).update(patch).eq("resource_id", resource_id).execute()
        except Exception as e:
            logger.warning("  Path B patch failed on %s for %s: %s", table, resource_id, e)

    return {
        "action":         "path_b_patch",
        "resource_id":    resource_id,
        "fields_changed": list(patch.keys()),
    }


def _handle_deadline_extension(parsed: dict, db_row: dict, db, dry_run: bool) -> dict:
    """
    Handle deadline extension — re-fetch the detail page to pick up the new
    submission deadline and any other changed fields.
    Reuses _handle_prepare_view since the portal will carry the updated date.
    """
    result = _handle_prepare_view(parsed, db_row, db, dry_run)
    result["action"] = "deadline_extension"
    return result


def _handle_cancellation(parsed: dict, db_row: dict, db, dry_run: bool) -> dict:
    """
    Remove the tender from gojep_tenders_current.
    The record is kept in gojep_tenders_all for historical reference.
    """
    from config import settings as config

    resource_id = db_row["resource_id"]

    if dry_run:
        logger.info("  [dry-run] Would remove resource_id=%s from gojep_tenders_current", resource_id)
        return {"action": "cancellation", "dry_run": True, "resource_id": resource_id}

    try:
        db.supabase.table(config.SUPABASE_TABLE_TENDERS_CURRENT).delete().eq("resource_id", resource_id).execute()
        logger.info("  Removed resource_id=%s from gojep_tenders_current", resource_id)
    except Exception as e:
        logger.warning("  Cancellation delete failed for %s: %s", resource_id, e)
        return {"action": "cancellation", "resource_id": resource_id, "error": str(e)}

    return {"action": "cancellation", "resource_id": resource_id, "removed_from_current": True}


_ADDENDUM_SYSTEM_PROMPT = """\
You are analyzing a GOJEP (Government of Jamaica Electronic Procurement) addendum email.
An addendum may change tender scope, documents, dates, evaluation criteria, or other terms.

Read the email carefully and return ONLY a valid JSON object with these fields:

{
  "summary": "One paragraph describing what this addendum changes",
  "submission_deadline": "New bid deadline if changed — format exactly as written, null if unchanged",
  "site_visit_date": "New site visit/conference date if changed, null if unchanged",
  "bid_opening_date": "New bid opening date if changed, null if unchanged",
  "scope_changed": true/false,
  "documents_changed": true/false,
  "evaluation_criteria_changed": true/false,
  "other_changes": "Free text describing any other changes not captured above, null if none",
  "recommended_action": "One of: patch_dates | re_fetch_detail_page | manual_review | no_action",
  "recommended_action_reason": "Why you chose that action"
}

Return ONLY the JSON object — no markdown fences, no commentary.
"""


def _handle_addendum(parsed: dict, db_row: dict, db, dry_run: bool) -> dict:
    """
    Addendum handler — downloads the updated documents ZIP (same button as
    new_documents: downloadForAnonymousUser) and records an LLM summary of
    what changed for the action_result audit trail.
    """
    import json as _json
    import requests
    from config import settings as config

    resource_id           = db_row["resource_id"]
    competition_unique_id = db_row.get("competition_unique_id") or resource_id
    action_url            = parsed.get("action_url") or (
        f"{config.GOJEP_BASE_URL}/epps/cft/listContractDocuments.do?resourceId={resource_id}"
    )
    subject = parsed.get("subject", "")
    body    = parsed.get("body_plain", "") or ""

    # ── LLM summary (best-effort — does not block the download) ──────────────
    analysis: dict = {}
    try:
        payload = {
            "model":       config.OPENROUTER_MODELS[config.CLASSIFIER_MODEL],
            "messages":    [
                {"role": "system", "content": _ADDENDUM_SYSTEM_PROMPT},
                {"role": "user",   "content": f"Subject: {subject}\n\n{body}"},
            ],
            "max_tokens":  400,
            "temperature": 0.0,
        }
        resp = requests.post(
            config.OPENROUTER_URL,
            headers=config.OPENROUTER_HEADERS,
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            import re as _re
            content = _re.sub(r"^```[^\n]*\n?", "", content)
            content = _re.sub(r"\n?```$", "", content)
        analysis = _json.loads(content)
    except Exception as e:
        logger.warning("Addendum LLM summary failed for resource_id=%s: %s", resource_id, e)

    summary = analysis.get("summary", "(LLM analysis unavailable)")
    logger.info("  Addendum summary for %s: %s", resource_id, summary)

    if dry_run:
        return {
            "action":       "addendum",
            "dry_run":      True,
            "resource_id":  resource_id,
            "url":          action_url,
            "llm_summary":  summary,
        }

    # ── Download documents ZIP (same flow as new_documents) ──────────────────
    from modules.tenders.get_tenders import GOJEPScraper
    from modules.tenders.get_tender_documents import download_contract_documents_direct
    import os

    docs_base_dir = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "documents")
    folder_name = str(competition_unique_id).replace("/", "_", 1)

    new_docs_downloads = os.path.join(docs_base_dir, folder_name, EMAIL_UPDATES, NEW_DOCUMENTS, DOCUMENT_DOWNLOADS)
    os.makedirs(new_docs_downloads, exist_ok=True)

    print(f"  Launching browser (headless) ...")
    print(f"  Navigating to: {action_url}")
    scraper = GOJEPScraper()
    scraper._setup_driver(headless=True)
    try:
        dl_result = download_contract_documents_direct(
            driver=scraper.driver,
            resource_id=resource_id,
            competition_unique_id=competition_unique_id,
            action_url=action_url,
            docs_base_dir=docs_base_dir,
            save_to_email_updates=True,
        )
    finally:
        scraper.driver.quit()

    files = dl_result.get("downloaded_files", [])
    skipped = dl_result.get("skipped_already_uploaded", 0)
    print(f"  Downloaded {len(files)} file(s), skipped {skipped} already uploaded.")
    if files:
        for f in files:
            print(f"    + {f}")

    return {
        "action":      "addendum",
        "resource_id": resource_id,
        "llm_summary": summary,
        **dl_result,
    }


# ── Re-analysis queue ─────────────────────────────────────────────────────────

def _unblock_files(folder: str) -> None:
    """Unblock downloaded files in a folder using PowerShell Unblock-File.
    Required on Windows — files downloaded from the internet carry a Zone.Identifier
    alternate data stream that blocks WSL (Docling) from reading them.
    Uses -Recurse to also cover subdirectories.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             f"Get-ChildItem -Path '{folder}' -Recurse -File | Unblock-File"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 and result.stderr.strip():
            logger.warning("Unblock-File stderr for %s: %s", folder, result.stderr.strip())
        else:
            logger.debug("Unblocked files in %s", folder)
    except Exception as e:
        logger.warning("Unblock-File failed for %s: %s", folder, e)


def _run_reanalysis_queue(queue: dict, db) -> None:
    """
    For each tender queued for re-analysis:
      1. Unblock downloaded files (Windows Zone.Identifier)
      2. Check email_updates subfolders for new/changed files (clarifications, new_documents)
      3. Sync changed files to extracted_documents (for that email_updates subfolder)
      4. Extract changed files to json_documents
      5. Re-run analyse_tender_folder via OpenRouter
      6. Stamp extraction_status / analysis_status on gojep_email_actions rows

    Uses new folder structure:
      <tender_id>/
          tender_data/
              document_downloads/
              extracted_documents/
              json_documents/
          email_updates/
              clarifications/
                  document_downloads/
                  extracted_documents/
                  json_documents/
              new_documents/
                  document_downloads/
                  extracted_documents/
                  json_documents/
    """
    if not queue:
        return

    import os
    import shutil
    from db.emails.email_actions import mark_extraction_done, mark_extraction_failed, mark_analysis_done, mark_analysis_failed
    from config import settings as config
    from modules.shared.document_sync import (
        build_file_manifest,
        compare_manifests,
        load_extraction_manifest,
        save_extraction_manifest,
        update_file_in_manifest,
        TENDER_DATA,
        EMAIL_UPDATES,
        CLARIFICATIONS,
        NEW_DOCUMENTS,
        DOCUMENT_DOWNLOADS,
        EXTRACTED_DOCUMENTS,
        JSON_DOCUMENTS,
    )
    from modules.document_processing.extract import (
        extract_nested_zips,
        extract_via_lightning_flat,
        extract_xlsx,
        extract_pptx,
        extract_text,
        _get_ext,
        DOCLING_FORMATS,
        XLSX_FORMATS,
        PPTX_FORMATS,
        SKIP_EXTENSIONS,
        SafeEncoder,
    )
    from modules.analysis.analyse_tender import analyse_tender_folder

    docs_base_dir = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "documents")

    # Load all pending/failed action rows upfront — avoid PostgREST filtering on
    # competition_unique_id values that contain '/' (slash causes URL-encoding issues).
    pending_action_map: dict = {}
    try:
        all_pending = (
            db.supabase.table("gojep_email_actions")
            .select("id, competition_unique_id, extraction_status")
            .in_("extraction_status", ["pending", "failed"])
            .execute()
        ).data or []
        for row in all_pending:
            cuid = row.get("competition_unique_id")
            if cuid:
                pending_action_map.setdefault(cuid, []).append(row["id"])
        logger.debug("Loaded %d pending action rows across %d tenders", len(all_pending), len(pending_action_map))
    except Exception as e:
        logger.warning("Could not pre-load pending action rows: %s", e)

    print(f"\n{'=' * 70}")
    print(f"  Re-analysing {len(queue)} tender(s) with updated context ...")

    for competition_unique_id, entry in queue.items():
        folder_name = str(competition_unique_id).replace("/", "_", 1)
        tender_folder = os.path.join(docs_base_dir, folder_name)
        reason = entry.get("reason", "")

        print(f"\n  {competition_unique_id}  \u2014  {reason}")

        if not os.path.exists(tender_folder):
            print(f"    Folder not found: {tender_folder} \u2014 skipping.")
            continue

        try:
            # ── Step 1: Extract any new/changed files from email_updates ──────
            email_subfolders = [
                os.path.join(tender_folder, EMAIL_UPDATES, CLARIFICATIONS),
                os.path.join(tender_folder, EMAIL_UPDATES, NEW_DOCUMENTS),
            ]

            all_changed_files: list[str] = []

            for email_subfolder in email_subfolders:
                if not os.path.exists(email_subfolder):
                    continue

                doc_downloads = os.path.join(email_subfolder, DOCUMENT_DOWNLOADS)
                doc_extracted = os.path.join(email_subfolder, EXTRACTED_DOCUMENTS)
                doc_json = os.path.join(email_subfolder, JSON_DOCUMENTS)

                if not os.path.exists(doc_downloads):
                    continue

                _unblock_files(doc_downloads)
                extract_nested_zips(os.path.dirname(email_subfolder), os.path.basename(email_subfolder))

                original_manifest = build_file_manifest(doc_downloads, relative_to=doc_downloads)
                extraction_manifest = load_extraction_manifest(doc_json)
                existing_files = extraction_manifest.get("files", {})

                comparison = compare_manifests(existing_files, original_manifest)
                changed_files = [fname for fname, status in comparison.items() if status in ("new", "changed")]

                if not changed_files:
                    continue

                print(f"    Found {len(changed_files)} changed file(s) in {os.path.basename(email_subfolder)}: {changed_files}")

                os.makedirs(doc_extracted, exist_ok=True)
                os.makedirs(doc_json, exist_ok=True)

                copied_files: list[str] = []
                for fname in changed_files:
                    src = os.path.join(doc_downloads, fname)
                    dst = os.path.join(doc_extracted, fname)
                    if os.path.exists(src):
                        shutil.copy2(src, dst)
                        copied_files.append(fname)

                lightning_files: list[str] = []
                local_files: list[str] = []

                for fname in copied_files:
                    fpath = os.path.join(doc_extracted, fname)
                    ext = _get_ext(fpath)
                    if ext in SKIP_EXTENSIONS or ext == "":
                        continue
                    if ext in DOCLING_FORMATS:
                        lightning_files.append(fpath)
                    else:
                        local_files.append(fpath)

                from datetime import datetime
                for fpath in local_files:
                    file_name = os.path.basename(fpath)
                    ext = _get_ext(fpath)
                    if ext in XLSX_FORMATS:
                        content = extract_xlsx(fpath)
                    elif ext in PPTX_FORMATS:
                        content = extract_pptx(fpath)
                    else:
                        content = extract_text(fpath)

                    if content and "error" not in content:
                        output_data = {
                            "source_file": file_name,
                            "extension": ext.lstrip("."),
                            "extraction_timestamp": datetime.utcnow().isoformat() + "Z",
                            "content": content,
                        }
                        output_json_path = os.path.join(doc_json, f"{file_name}.json")
                        with open(output_json_path, "w", encoding="utf-8") as f:
                            json.dump(output_data, f, ensure_ascii=False, indent=2, cls=SafeEncoder)
                        print(f"      Extracted locally: {file_name}")

                if lightning_files:
                    print(f"\n      Dispatching Lightning Studio for {len(lightning_files)} file(s)...")
                    extract_via_lightning_flat(lightning_files, doc_json)

                for fname in copied_files:
                    src_path = os.path.join(doc_downloads, fname)
                    if os.path.exists(src_path):
                        stat = os.stat(src_path)
                        hash_md5 = original_manifest.get(fname, {}).get("hash_md5", "")
                        update_file_in_manifest(
                            extraction_manifest,
                            fname,
                            stat.st_size,
                            hash_md5,
                            f"{EMAIL_UPDATES}/{os.path.basename(email_subfolder)}/{DOCUMENT_DOWNLOADS}/{fname}",
                        )

                save_extraction_manifest(doc_json, extraction_manifest)
                all_changed_files.extend(copied_files)

            if not all_changed_files:
                print(f"    No changed files to re-extract.")

            # ── Step 2: Stamp extraction done, then re-analyse ────────────────
            # Use the preloaded map (keyed in Python) to avoid PostgREST slash-encoding issues.
            action_ids = pending_action_map.get(competition_unique_id, [])
            for aid in action_ids:
                mark_extraction_done(db, aid)

            print(f"    Re-analysing full tender context ...")
            analyse_tender_folder(tender_folder, db=db, reanalyse=True)
            for aid in action_ids:
                mark_analysis_done(db, aid)
            print(f"    Done.")

        except Exception as e:
            logger.error("Reanalysis failed for %s: %s", competition_unique_id, e, exc_info=True)
            print(f"    FAILED: {e}")
            try:
                action_rows = (
                    db.supabase.table("gojep_email_actions")
                    .select("id, extraction_status")
                    .eq("competition_unique_id", competition_unique_id)
                    .in_("extraction_status", ["pending", "failed"])
                    .execute()
                ).data or []
                for r in action_rows:
                    mark_analysis_failed(db, r["id"], str(e))
            except Exception:
                pass


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_process_email_updates(args: argparse.Namespace) -> bool:
    """
    Full email update pipeline — four phases:

      Phase 1 — Ingest:   fetch emails from Gmail; save each to
                           gojep_email_updates (skip if already saved);
                           trash from Gmail inbox immediately so the DB
                           is the sole source of truth from this point.
                           Also picks up any 'pending' emails from
                           previous runs that were saved but not yet
                           processed (e.g. after a crash).
      Phase 2 — Triage:   match each pending email to an open tender;
                           queue actionable ones in gojep_email_actions,
                           mark others skipped / discarded / manual_review.
      Phase 3 — Process:  work through gojep_email_actions queue; run
                           each handler, mark actioned or failed.
      Phase 4 — Reanalysis: run VLM-gated re-analysis for affected tenders.
                           No Gmail cleanup needed — inbox was cleared in Phase 1.
    """
    from modules.emails.gmail_client import get_gmail_service, fetch_unprocessed_emails, delete_email
    from modules.emails.parse_update import parse_emails, _INFORMATIONAL_TYPES
    from modules.emails.match_path_b import match_path_b_to_tender
    from db.client.supabase_client import SupabaseClient
    from db.emails.email_updates import (
        insert_pending, get_pending_emails, mark_queued, mark_actioned,
        mark_failed, mark_skipped, mark_discarded, mark_manual_review,
    )
    from db.emails.email_actions import insert_queued, mark_action_completed, mark_action_failed, get_pending_actions

    dry_run = getattr(args, "dry_run", False)
    if dry_run:
        print("  [dry-run mode] Actions skipped — DB writes still applied.\n")

    # ── Authenticate ─────────────────────────────────────────────────────────
    print("Authenticating with Gmail ...")
    try:
        service = get_gmail_service()
    except Exception as e:
        print(f"Gmail authentication failed: {e}")
        return False

    db = SupabaseClient()

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 1 — INGEST
    # Save each email to DB and delete from Gmail inbox immediately.
    # The DB is the source of truth from this point — Gmail is not needed again.
    # Already-saved emails (from a previous run) are skipped via ignore_duplicates.
    # ════════════════════════════════════════════════════════════════════════
    print("Fetching unprocessed GOJEP emails ...")
    raw_emails = fetch_unprocessed_emails(service)
    new_count = 0
    if raw_emails:
        print(f"Found {len(raw_emails)} email(s) in inbox. Saving and clearing inbox ...")
        parsed_emails = parse_emails(raw_emails)
        for p in parsed_emails:
            inserted = insert_pending(db, p)
            try:
                delete_email(service, p["email_message_id"])
            except Exception as e:
                logger.warning("Could not trash email %s: %s", p["email_message_id"], e)
            if inserted:
                new_count += 1
        print(f"  {new_count} new email(s) saved. {len(parsed_emails) - new_count} already in DB (skipped).\n")
    else:
        print("No new emails in inbox.\n")

    # Triage works from the DB — picks up current batch + any pending leftovers
    # from previous runs that were saved but not yet processed.
    pending_rows = get_pending_emails(db)
    if not pending_rows:
        print("No pending emails to process.")
    else:
        print(f"{len(pending_rows)} pending email(s) to triage (including any from previous runs).\n")

    # Reconstruct parsed dicts from DB rows so triage/Phase 3 work identically
    # regardless of whether the email came from the current run or a saved prior run.
    def _row_to_parsed(row: dict) -> dict:
        update_type = row.get("update_type") or "other"
        return {
            "email_message_id": row["email_message_id"],
            "received_at":      row.get("received_at"),
            "sender":           row.get("sender"),
            "subject":          row.get("subject") or "",
            "path":             row.get("path") or "A",
            "resource_id":      row.get("resource_id"),
            "tender_title":     row.get("tender_title"),
            "update_type":      update_type,
            "action_url":       row.get("action_url"),
            "action_url_type":  row.get("action_url_type"),
            "extracted_dates":  row.get("extracted_dates") or {},
            "requires_action":  update_type not in _INFORMATIONAL_TYPES,
            "raw_summary":      f"[{update_type}] {row.get('subject', '')[:80]}",
        }

    # Deduplicate within the pending batch: same (resource_id, action_type) →
    # keep only the most recent, mark others discarded.
    seen_actions: set[tuple[str, str]] = set()
    deduped:      list[dict] = []
    duplicate_ids: list[str] = []

    for row in pending_rows:
        p     = _row_to_parsed(row)
        rid   = p.get("resource_id") or ""
        atype = p.get("action_url_type") or p.get("update_type") or ""
        key   = (rid, atype)
        if p.get("requires_action") and rid and key in seen_actions:
            duplicate_ids.append(p["email_message_id"])
        else:
            if p.get("requires_action") and rid:
                seen_actions.add(key)
            deduped.append(p)

    if duplicate_ids:
        for mid in duplicate_ids:
            mark_discarded(db, mid)
        print(f"  Marked {len(duplicate_ids)} duplicate(s) as discarded.")
    print(f"  {len(deduped)} unique email(s) to triage.\n")

    # Keep a lookup so Phase 3 can reconstruct the parsed dict from the action row
    parsed_lookup: dict[str, dict] = {p["email_message_id"]: p for p in deduped}

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 2 — TRIAGE
    # ════════════════════════════════════════════════════════════════════════
    SEP = "\u2500" * 70
    _DOC_TYPES = {"new_documents", "clarification_response", "addendum"}

    skipped_count       = 0
    unmatched_count     = 0
    manual_review_count = 0
    queued_count        = 0

    print(f"Triaging {len(deduped)} email(s) ...")
    for i, parsed in enumerate(deduped, 1):
        mid         = parsed["email_message_id"]
        update_type = parsed["update_type"]
        resource_id = parsed.get("resource_id")

        print(f"\n{SEP}")
        print(f"  [{i}/{len(deduped)}] {parsed['subject'][:70]}")
        print(f"  Type: {update_type}  |  Path: {parsed['path']}  |  resource_id: {resource_id or 'none'}")

        # Match against open tenders
        db_row = None
        if resource_id:
            db_row = _match_resource_id_to_db(resource_id, db)
        elif parsed["path"] == "B":
            db_row = match_path_b_to_tender(db, parsed) if not dry_run else None

        if not db_row:
            if parsed["path"] == "B" and parsed.get("requires_action"):
                print(f"  \u2192 No confident match \u2014 manual review.")
                mark_manual_review(db, mid, "Path B: LLM could not match to an open tender")
                manual_review_count += 1
            else:
                print(f"  \u2192 No match \u2014 tender not in open tenders. Discarding.")
                mark_discarded(db, mid)
                unmatched_count += 1
            continue

        confidence_note = ""
        if parsed["path"] == "B" and db_row.get("_match_confidence"):
            confidence_note = f"  (confidence: {db_row['_match_confidence']:.0%})"
        print(f"  Match: {db_row.get('title', '?')[:70]}{confidence_note}")

        if not parsed.get("requires_action"):
            print(f"  \u2192 Informational \u2014 skipping.")
            mark_skipped(db, mid)
            skipped_count += 1
            continue

        # Queue in gojep_email_actions
        cuid             = db_row.get("competition_unique_id") or db_row.get("resource_id")
        needs_extraction = update_type in _DOC_TYPES
        action_id = insert_queued(
            db,
            email_message_id      = mid,
            competition_unique_id = cuid,
            resource_id           = resource_id or db_row.get("resource_id"),
            tender_title          = db_row.get("title"),
            update_type           = update_type,
            action_url            = parsed.get("action_url"),
            action_url_type       = parsed.get("action_url_type"),
            needs_extraction      = needs_extraction,
        )
        mark_queued(db, mid)
        queued_count += 1

        # Stash db_row in parsed_lookup for Phase 3 (in-memory, same run only)
        parsed_lookup[mid]["_db_row"] = db_row

        print(f"  \u2192 Queued (action_id={action_id}, needs_extraction={needs_extraction}).")

    print(f"\nTriage complete: {queued_count} queued, {skipped_count} skipped, "
          f"{unmatched_count} discarded, {manual_review_count} manual review.\n")

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 3 — PROCESS gojep_email_actions QUEUE
    # ════════════════════════════════════════════════════════════════════════
    actioned_count = 0
    error_count    = 0
    reanalysis_queue: dict[str, dict] = {}

    pending_actions = get_pending_actions(db)
    if not pending_actions:
        print("No pending actions to process.")
    else:
        print(f"Processing {len(pending_actions)} queued action(s) ...")

    for i, action in enumerate(pending_actions, 1):
        action_id   = action["id"]
        mid         = action["email_message_id"]
        update_type = action.get("update_type", "")
        cuid        = action.get("competition_unique_id")
        resource_id = action.get("resource_id")

        # Retrieve stashed in-memory data from triage phase
        parsed = parsed_lookup.get(mid, {})
        db_row = parsed.pop("_db_row", None)

        # db_row fallback: re-query if Phase 2 data isn't in memory
        if not db_row:
            db_row = _match_resource_id_to_db(resource_id, db) if resource_id else None

        if not db_row:
            print(f"\n{SEP}")
            print(f"  [{i}/{len(pending_actions)}] action_id={action_id} — no DB match, skipping.")
            mark_action_failed(db, action_id, "no DB match for resource_id")
            mark_failed(db, mid, "no DB match for resource_id")
            error_count += 1
            continue

        # parsed fallback: reconstruct from action row if not in memory
        if not parsed:
            parsed = {
                "email_message_id": mid,
                "resource_id":      resource_id,
                "update_type":      update_type,
                "action_url":       action.get("action_url"),
                "action_url_type":  action.get("action_url_type"),
                "path":             "A",
                "body_plain":       "",
                "subject":          "",
                "extracted_dates":  {},
            }

        action_url_type = parsed.get("action_url_type") or action.get("action_url_type")

        print(f"\n{SEP}")
        print(f"  [{i}/{len(pending_actions)}] action_id={action_id}  type={update_type}  resource_id={resource_id}")
        print(f"  Tender : {db_row.get('title', '?')[:70]}")

        try:
            if update_type == "new_documents":
                result = _handle_new_documents(parsed, db_row, dry_run)
                if dry_run:
                    print(f"  \u2192 [dry-run] Would download contract documents")
                elif result.get("new_files_downloaded"):
                    print(f"  \u2192 Downloaded {result['new_files_downloaded']} file(s): {result.get('downloaded_files', [])}")
                elif result.get("error"):
                    print(f"  \u2192 Download failed: {result['error']}")
                else:
                    print(f"  \u2192 No new files (skipped={result.get('skipped_already_uploaded', 0)})")
                if not dry_run and cuid and result.get("downloaded_files"):
                    assessment = _assess_new_files(result["downloaded_files"], resource_id, update_type)
                    if assessment["needs_reanalysis"]:
                        print(f"     VLM: re-analysis required — {assessment['reason']}")
                        reanalysis_queue[cuid] = {"reason": assessment["reason"], "needs_extraction": True}
                    else:
                        print(f"     VLM: re-analysis not required — {assessment['reason']}")

            elif action_url_type == "list_clarification" or update_type == "clarification_response":
                result = _handle_list_clarification(parsed, db_row, dry_run)
                if dry_run:
                    print(f"  \u2192 [dry-run] Would download clarification documents")
                elif result.get("new_attachments_downloaded"):
                    print(f"  \u2192 Downloaded {result['new_attachments_downloaded']} attachment(s): {result.get('downloaded_files', [])}")
                else:
                    print(f"  \u2192 No new attachments (skipped={result.get('skipped_already_uploaded', 0)})")
                if not dry_run and cuid and result.get("downloaded_files"):
                    assessment = _assess_new_files(result["downloaded_files"], resource_id, update_type)
                    if assessment["needs_reanalysis"]:
                        print(f"     VLM: re-analysis required — {assessment['reason']}")
                        reanalysis_queue[cuid] = {"reason": assessment["reason"], "needs_extraction": True}
                    else:
                        print(f"     VLM: re-analysis not required — {assessment['reason']}")

            elif action_url_type == "prepare_view" or update_type == "modifications":
                result = _handle_prepare_view(parsed, db_row, db, dry_run)
                if dry_run:
                    print(f"  \u2192 [dry-run] Would re-fetch tender detail page")
                else:
                    print(f"  \u2192 Detail re-fetched. Changed: {result.get('fields_changed') or 'none'}")
                    if result.get("substantive_changes"):
                        print(f"     Substantive: {result['substantive_changes']} — re-analysis queued")
                    else:
                        print(f"     Administrative changes only — re-analysis skipped")
                if not dry_run and cuid and result.get("needs_reanalysis"):
                    reanalysis_queue.setdefault(cuid, {"reason": "substantive tender fields modified", "needs_extraction": False})

            elif update_type == "deadline_extension":
                result = _handle_deadline_extension(parsed, db_row, db, dry_run)
                if dry_run:
                    print(f"  \u2192 [dry-run] Would re-fetch detail page for deadline update")
                else:
                    print(f"  \u2192 Deadline extension. Changed: {result.get('fields_changed') or 'none'}")
                    if result.get("substantive_changes"):
                        print(f"     Substantive: {result['substantive_changes']} — re-analysis queued")
                    else:
                        print(f"     Administrative changes only — re-analysis skipped")
                if not dry_run and cuid and result.get("needs_reanalysis"):
                    reanalysis_queue.setdefault(cuid, {"reason": "deadline extension with substantive changes", "needs_extraction": False})

            elif update_type == "cancellation":
                result = _handle_cancellation(parsed, db_row, db, dry_run)
                if dry_run:
                    print(f"  \u2192 [dry-run] Would remove tender from gojep_tenders_current")
                else:
                    print(f"  \u2192 Cancellation: removed from gojep_tenders_current.")

            elif update_type == "addendum":
                result = _handle_addendum(parsed, db_row, db, dry_run)
                if dry_run:
                    print(f"  \u2192 [dry-run] Would download addendum documents")
                elif result.get("new_files_downloaded"):
                    print(f"  \u2192 Addendum: {result['new_files_downloaded']} file(s): {result.get('downloaded_files', [])}")
                else:
                    print(f"  \u2192 Addendum: no new files (skipped={result.get('skipped_already_uploaded', 0)})")
                if result.get("llm_summary"):
                    print(f"     Summary: {result['llm_summary'][:120]}")
                if not dry_run and cuid and result.get("downloaded_files"):
                    assessment = _assess_new_files(result["downloaded_files"], resource_id, update_type)
                    if assessment["needs_reanalysis"]:
                        print(f"     VLM: re-analysis required — {assessment['reason']}")
                        reanalysis_queue[cuid] = {"reason": assessment["reason"], "needs_extraction": True}
                    else:
                        print(f"     VLM: re-analysis not required — {assessment['reason']}")

            elif parsed.get("path") == "B":
                result = _handle_path_b_patch(parsed, db_row, db, dry_run)
                if dry_run:
                    print(f"  \u2192 [dry-run] Would patch date fields")
                else:
                    if db_row.get("_match_reasoning"):
                        print(f"     Match reason: {db_row['_match_reasoning']}")
                    print(f"  \u2192 Date fields patched: {result.get('fields_changed') or 'none'}")
                if not dry_run and cuid:
                    reanalysis_queue.setdefault(cuid, {"reason": "date fields patched", "needs_extraction": False})

            else:
                print(f"  \u2192 update_type='{update_type}' \u2014 no handler. Skipping.")
                result = {}

        except Exception as e:
            logger.error("Action failed for message %s: %s", mid, e, exc_info=True)
            print(f"  \u2192 ERROR: {e}")
            mark_action_failed(db, action_id, str(e))
            mark_failed(db, mid, str(e))
            print(f"  \u2717 Failed.")
            error_count += 1
            continue

        # Success — stamp the action row and update email_updates
        files_dl      = result.get("downloaded_files") or []
        fields_changed = result.get("patch_applied") or result.get("llm_patch") or result.get("fields_changed")
        mark_action_completed(
            db, action_id,
            files_downloaded = files_dl if files_dl else None,
            fields_changed   = fields_changed if isinstance(fields_changed, dict) else None,
        )
        mark_actioned(db, mid)
        print(f"  \u2713 Actioned.")
        actioned_count += 1

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 4 — REANALYSIS
    # Gmail cleanup is no longer needed here — emails were trashed from the
    # inbox immediately on ingest in Phase 1.
    # ════════════════════════════════════════════════════════════════════════
    if not dry_run:
        # Recover any tenders whose extraction was interrupted by a previous crash
        try:
            leftover_rows = (
                db.supabase.table("gojep_email_actions")
                .select("competition_unique_id, update_type")
                .eq("action_status", "completed")
                .eq("extraction_status", "pending")
                .execute()
            ).data or []
            for row in leftover_rows:
                cuid = row.get("competition_unique_id")
                utype = row.get("update_type", "")
                if cuid and cuid not in reanalysis_queue:
                    reanalysis_queue[cuid] = {
                        "reason": f"recovery: pending extraction from previous run ({utype})",
                        "needs_extraction": utype in _DOC_TYPES,
                    }
                    logger.info("Recovered pending extraction for %s", cuid)
        except Exception as _e:
            logger.warning("Could not load leftover pending extractions: %s", _e)

        _run_reanalysis_queue(reanalysis_queue, db)

    print(f"\n{'=' * 70}")
    print(f"  Email update pipeline complete.")
    print(f"  New emails ingested : {new_count}")
    print(f"  Queued              : {queued_count}")
    print(f"  Actioned            : {actioned_count}")
    print(f"  Skipped             : {skipped_count}  (informational)")
    print(f"  Discarded           : {unmatched_count + len(duplicate_ids)}  (no match / duplicate)")
    print(f"  Manual review       : {manual_review_count}")
    print(f"  Errors              : {error_count}")
    print(f"{'=' * 70}\n")

    return error_count == 0


def run_review_email_updates(args: argparse.Namespace) -> bool:
    """
    Fetch and parse emails without taking any action or modifying Gmail labels.
    Useful for inspecting what would be processed.
    """
    from modules.emails.gmail_client import get_gmail_service, fetch_unprocessed_emails
    from modules.emails.parse_update import parse_emails

    print("Authenticating with Gmail ...")
    try:
        service = get_gmail_service()
    except Exception as e:
        print(f"Gmail authentication failed: {e}")
        return False

    print("Fetching unprocessed GOJEP emails ...")
    raw_emails = fetch_unprocessed_emails(service)

    if not raw_emails:
        print("No unprocessed emails found.")
        return True

    parsed_emails = parse_emails(raw_emails)

    print(f"\nFound {len(parsed_emails)} email(s):\n")
    for i, p in enumerate(parsed_emails, 1):
        action_flag = "ACTION" if p.get("requires_action") else "info"
        print(f"  [{i}] [{action_flag}] {p['update_type']:<28} {p['subject'][:60]}")
        print(f"       resource_id={p.get('resource_id') or 'unknown':<12} "
              f"path={p['path']}  url_type={p.get('action_url_type') or 'none'}")
        print()

    return True


# ── Parser Registration ───────────────────────────────────────────────────────

def create_emails_parser(subparsers) -> None:

    # process-email-updates
    p1 = subparsers.add_parser(
        "process-email-updates",
        help="Fetch, parse, and act on GOJEP notification emails",
    )
    p1.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and match only — no DB writes, no Gmail label changes",
    )
    p1.set_defaults(func=run_process_email_updates)

    # review-email-updates
    p2 = subparsers.add_parser(
        "review-email-updates",
        help="Preview unprocessed emails without taking any action",
    )
    p2.set_defaults(func=run_review_email_updates)
