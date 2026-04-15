"""
DB metadata fetch: retrieve and format tender metadata from Supabase.
"""

import logging
import os
from typing import Any, Dict, Optional

from config import settings as config

logger = logging.getLogger(__name__)

# ── DB field lists ─────────────────────────────────────────────────────────────

DB_SELECT_FIELDS = ",".join([
    "resource_id", "competition_unique_id", "title", "procuring_entity",
    "procurement_type", "services_subtype", "procurement_method",
    "evaluation_mechanism", "description", "detailed_description",
    "funding_source", "submission_deadline", "bid_opening_date",
    "site_visit_date", "clarification_period_end",
    "ppc_ncc_categories", "cpv_codes",
])

DB_META_FIELDS = [
    "title", "procuring_entity", "procurement_type", "services_subtype",
    "procurement_method", "evaluation_mechanism", "description",
    "detailed_description", "funding_source", "submission_deadline",
    "bid_opening_date", "site_visit_date", "clarification_period_end",
    "ppc_ncc_categories", "cpv_codes",
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_resource_id_from_folder(tender_folder: str, folder_name: str) -> Optional[str]:
    """
    Derive the DB resource_id from the tender_data/json_documents PDF filename.
    Files are named {entity_code}_{resource_id}.pdf.json — e.g. 1000_9145038.pdf.json.
    Returns the long numeric resource_id, or None if not found.
    """
    # Import folder constants from chunk to avoid duplication
    from modules.analysis.chunk import TENDER_DATA, JSON_DOCUMENTS

    json_docs_dir = os.path.join(tender_folder, TENDER_DATA, JSON_DOCUMENTS)
    if not os.path.exists(json_docs_dir):
        return None
    entity_prefix = folder_name.split("_")[0] + "_"
    for fname in os.listdir(json_docs_dir):
        if fname.startswith(entity_prefix) and fname.endswith(".pdf.json"):
            stem = fname[len(entity_prefix):].replace(".pdf.json", "")
            if stem.isdigit():
                return stem
    return None


def _fetch_db_metadata(folder_name: str, db, tender_folder: str = "") -> Optional[Dict[str, Any]]:
    """
    Fetch tender metadata for a given folder.

    Lookup strategy (in order):
      1. competition_unique_id = folder_name with '/' (e.g. '1000/972' from '1000_972')
      2. resource_id = long numeric ID extracted from the PDF filename in extracted_docs
    Checks gojep_tenders_current then gojep_tenders_all for each strategy.
    """
    competition_uid = folder_name.replace("_", "/", 1)
    file_resource_id = _extract_resource_id_from_folder(tender_folder, folder_name) if tender_folder else None

    for table in [config.SUPABASE_TABLE_TENDERS_CURRENT, config.SUPABASE_TABLE_TENDERS_ALL]:
        try:
            result = db.supabase.table(table)\
                .select(DB_SELECT_FIELDS)\
                .eq("competition_unique_id", competition_uid)\
                .limit(1)\
                .execute()
            logger.debug(f"[{table}] competition_unique_id='{competition_uid}' → {len(result.data)} row(s)")
            if result.data:
                return result.data[0]
        except Exception as e:
            logger.warning(f"competition_unique_id lookup failed in {table} for '{competition_uid}': {e}")

        if file_resource_id:
            try:
                result = db.supabase.table(table)\
                    .select(DB_SELECT_FIELDS)\
                    .eq("resource_id", file_resource_id)\
                    .limit(1)\
                    .execute()
                logger.debug(f"[{table}] resource_id='{file_resource_id}' → {len(result.data)} row(s)")
                if result.data:
                    return result.data[0]
            except Exception as e:
                logger.warning(f"resource_id lookup failed in {table} for '{file_resource_id}': {e}")

    logger.warning(
        f"No DB metadata found for '{folder_name}' — "
        f"tried competition_unique_id='{competition_uid}', resource_id='{file_resource_id}'"
    )
    return None


def _format_metadata_header(meta: Dict[str, Any]) -> str:
    """Format DB metadata as a readable block to prepend to the document context."""
    def _val(key):
        v = meta.get(key)
        if v is None:
            return "Not stated"
        if isinstance(v, list):
            return ", ".join(str(x) for x in v) if v else "None"
        return str(v)

    return "\n".join([
        "=== STRUCTURED METADATA FROM PROCUREMENT DATABASE ===",
        f"Title              : {_val('title')}",
        f"Procuring Entity   : {_val('procuring_entity')}",
        f"Procurement Type   : {_val('procurement_type')}",
        f"Services Subtype   : {_val('services_subtype')}",
        f"Procurement Method : {_val('procurement_method')}",
        f"Evaluation Method  : {_val('evaluation_mechanism')}",
        f"Funding Source     : {_val('funding_source')}",
        f"Submission Deadline: {_val('submission_deadline')}",
        f"Bid Opening Date   : {_val('bid_opening_date')}",
        f"Site Visit Date    : {_val('site_visit_date')}",
        f"Clarification End  : {_val('clarification_period_end')}",
        f"PPC/NCC Categories : {_val('ppc_ncc_categories')}",
        f"CPV Codes          : {_val('cpv_codes')}",
        f"Description        : {_val('description')}",
        f"Detailed Description: {_val('detailed_description')}",
        "=== END METADATA ===",
    ])
