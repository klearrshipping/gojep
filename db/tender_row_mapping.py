"""
Map GOJEP JSON exports (tenders listing + tender_details) to rows compatible with
``TENDER_FIELD_DEFAULTS`` / ``gojep_tenders_*`` tables.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

JAMAICA_TZ = ZoneInfo("America/Jamaica")


def resource_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    q = parse_qs(urlparse(url).query)
    v = q.get("resourceId", [None])[0]
    return str(v).strip() if v else None


def _parse_dd_mm_yyyy_time(s: str) -> Optional[datetime]:
    """Parse DD/MM/YYYY HH:MM[:SS] as Jamaica local, return UTC-aware datetime."""
    if not s or not isinstance(s, str):
        return None
    s = s.replace("\xa0", " ").strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=JAMAICA_TZ).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _to_iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat(timespec="seconds")


def parse_timestamp_field(val: Any) -> Optional[str]:
    """Listing/detail timestamps to UTC ISO string for Supabase."""
    if val is None or val == "":
        return None
    if isinstance(val, list) and val:
        val = val[0]
    if not isinstance(val, str):
        return None
    val = val.strip()
    if not val:
        return None
    # ISO from listing
    if "T" in val and ("+" in val or val.endswith("Z") or "-" in val[10:]):
        try:
            v = val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=JAMAICA_TZ)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            pass
    # Strip trailing UTC-5 label
    val = re.sub(r"\s*UTC-5\s*$", "", val).strip()
    dt = _parse_dd_mm_yyyy_time(val)
    return _to_iso_utc(dt)


def parse_publication_invitation(s: Any) -> Optional[str]:
    if s is None or not isinstance(s, str):
        return None
    s = re.sub(r"\s*UTC-5\s*$", "", s.replace("\xa0", " ")).strip()
    dt = _parse_dd_mm_yyyy_time(s)
    return _to_iso_utc(dt)


def parse_days_hours(s: Any) -> tuple[Optional[int], Optional[int]]:
    if not s or not isinstance(s, str):
        return None, None
    m = re.match(r"^(\d+)/(\d+)$", s.strip())
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _norm_cpv(val: Any) -> List[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    s = str(val).strip()
    if not s:
        return []
    return [s]


def _norm_ppc(val: Any) -> List[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    s = str(val).strip()
    if not s:
        return []
    return [s]


def listing_json_row_to_tender(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One object from tenders_*.json (array of listing rows)."""
    title_url = obj.get("title_url")
    rid = resource_id_from_url(title_url)
    if not rid:
        return None
    title = (obj.get("title") or "").strip() or "Untitled"
    sub = parse_timestamp_field(obj.get("bids_submission_deadline"))
    pub = parse_timestamp_field(obj.get("publication_date"))
    pub_iso = obj.get("publication_date_iso_jamaica")
    pub_parsed = parse_timestamp_field(pub_iso) if pub_iso else pub
    ext = parse_timestamp_field(obj.get("extracted_at_jamaica"))

    row: Dict[str, Any] = {
        "resource_id": rid,
        "row_number": int(obj["row_number"]) if str(obj.get("row_number", "")).isdigit() else None,
        "title": title,
        "detail_url": title_url,
        "procuring_entity": obj.get("procuring_entity"),
        "description": obj.get("description"),
        "procurement_type": obj.get("procurement_type"),
        "procedure": obj.get("procedure"),
        "pdf_url": obj.get("notice_pdf_url"),
        "source_url": obj.get("source_url"),
        "submission_deadline": sub,
        "submission_deadline_parsed": sub,
        "publication_date": pub,
        "publication_date_parsed": pub_parsed,
        "extraction_timestamp": ext or None,
        "detail_page_extracted": False,
    }
    return row


def load_listings_array(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array in {path}")
    return [x for x in data if isinstance(x, dict)]


def load_tender_details_payload(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "records" in data:
        rows = data["records"]
    elif isinstance(data, list):
        rows = data
    else:
        raise ValueError(f"Expected tender_details object with 'records' or array in {path}")
    if not isinstance(rows, list):
        raise ValueError("records must be a list")
    return [x for x in rows if isinstance(x, dict)]


def fields_to_tender_patch(fields: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    """Map detail page ``fields`` dict + record metadata to DB-shaped patch."""
    f = fields or {}
    patch: Dict[str, Any] = {
        "resource_id": (f.get("resource_id") or record.get("resource_id_from_url") or "").strip()
        or None,
        "competition_unique_id": f.get("competition_unique_id"),
        "title": f.get("title"),
        "procuring_entity": f.get("name_of_procuring_entity"),
        "procuring_entity_url": f.get("name_of_procuring_entity_url"),
        "evaluation_mechanism": f.get("evaluation_mechanism"),
        "description": f.get("description"),
        "detailed_description": f.get("description"),
        "procurement_type": f.get("procurement_type"),
        "services_subtype": f.get("services_subtype"),
        "procurement_method": f.get("procurement_method"),
        "procedure": f.get("procurement_method"),
        "retender_flag": f.get("retender_flag"),
        "procurement_technique": f.get("procurement_technique"),
        "ppc_ncc_categories": _norm_ppc(f.get("ppcncc_categories")),
        "cpv_codes": _norm_cpv(f.get("common_procurement_vocabulary_cpv")),
        "funding_source": f.get("funding_source"),
        "special_differential_treatment": f.get("special_and_differential_treatment_sdt"),
        "project_reference_number": f.get("project_reference_number"),
        "country_contract_performance": f.get("country_of_contract_performance"),
        "non_petroleum_indicator": f.get("nonpetroleum_indicator"),
        "original_submission_deadline": parse_timestamp_field(f.get("original_deadline_for_bid_submission")),
        "submission_deadline": parse_timestamp_field(f.get("deadline_for_bid_submission")),
        "submission_deadline_parsed": parse_timestamp_field(f.get("deadline_for_bid_submission")),
        "clarification_period_end": parse_timestamp_field(f.get("end_of_clarification_period")),
        "bid_opening_date": parse_timestamp_field(f.get("bid_opening_date")),
        "site_visit_date": parse_timestamp_field(f.get("site_visit_bidders_conference_date")),
        "publication_date": parse_publication_invitation(f.get("date_of_publicationinvitation")),
        "publication_date_parsed": parse_publication_invitation(f.get("date_of_publicationinvitation")),
        "detail_page_extracted": True,
    }

    ns = f.get("number_of_stages")
    if ns is not None and str(ns).strip() != "":
        try:
            patch["number_of_stages"] = int(str(ns).strip())
        except ValueError:
            patch["number_of_stages"] = None

    d, h = parse_days_hours(f.get("bid_submission_deadline_in_dayshours"))
    patch["bid_deadline_days_remaining"] = d
    patch["bid_deadline_hours_remaining"] = h

    # Booleans (strings Yes/No — SupabaseClient normalises)
    patch["framework_agreement_establishment"] = f.get("framework_agreement_establishment")
    patch["contract_awarded_in_lots"] = f.get("contract_awarded_in_lots")
    patch["pe_audit_correspondence"] = f.get("pe_audit_of_competition_correspondence")

    if record.get("fetched_at"):
        patch["extraction_timestamp"] = parse_timestamp_field(record["fetched_at"])

    if record.get("title_url"):
        patch["detail_url"] = record["title_url"]

    patch = {k: v for k, v in patch.items() if v is not None or k in ("detail_page_extracted",)}
    return patch


def merge_tender_rows(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Non-null overlay wins; lists replace when overlay non-empty."""
    out = dict(base)
    for k, v in overlay.items():
        if v is None:
            continue
        if isinstance(v, list) and not v:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        out[k] = v
    rid = out.get("resource_id") or overlay.get("resource_id")
    if rid:
        out["resource_id"] = rid
    return out


def merge_listings_and_details(
    listings: List[Dict[str, Any]],
    detail_records: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Return resource_id -> merged tender dict."""
    by_id: Dict[str, Dict[str, Any]] = {}

    for obj in listings:
        row = listing_json_row_to_tender(obj)
        if row:
            by_id[row["resource_id"]] = row

    for rec in detail_records:
        fields = rec.get("fields")
        if not isinstance(fields, dict):
            fields = {}
        patch = fields_to_tender_patch(fields, rec)
        rid = patch.get("resource_id") or rec.get("resource_id_from_url")
        if not rid:
            continue
        rid = str(rid).strip()
        patch["resource_id"] = rid
        if rid not in by_id:
            title = (patch.get("title") or "Untitled").strip() or "Untitled"
            by_id[rid] = {
                "resource_id": rid,
                "title": title,
                "detail_url": rec.get("title_url"),
                "detail_page_extracted": False,
            }
        by_id[rid] = merge_tender_rows(by_id[rid], patch)

    return by_id


def row_deadline_future(row: Dict[str, Any]) -> bool:
    """True if submission deadline (parsed) is in the future, or unknown."""
    dt = row.get("submission_deadline_parsed") or row.get("submission_deadline")
    if not dt:
        return True
    try:
        if isinstance(dt, str):
            dts = dt.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(dts)
        else:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed > datetime.now(timezone.utc)
    except Exception:
        return True
