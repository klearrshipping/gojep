"""
GOJEP Tender API
================
Public read-only API for tender listings, detail, and LLM analysis results.

Run:
    uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from config import settings as config
from db.client.supabase_client import SupabaseClient
from api.models import (
    TenderSummary,
    TenderDetail,
    TenderListResponse,
    StatsResponse,
    Milestone,
)

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="GOJEP Tender API",
    description="Search and retrieve tender opportunities from the Government of Jamaica Electronic Procurement (GOJEP) portal.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    redirect_slashes=True,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_db: Optional[SupabaseClient] = None


def get_db() -> SupabaseClient:
    global _db
    if _db is None:
        _db = SupabaseClient()
    return _db


# ── Helpers ───────────────────────────────────────────────────────────────────

TABLE_TENDERS   = config.SUPABASE_TABLE_TENDERS_CURRENT   # gojep_tenders_current
TABLE_ANALYSIS  = config.SUPABASE_TABLE_ANALYSIS_RESULTS   # gojep_analysis_results

TENDERS_LIST_COLS = (
    "competition_unique_id,resource_id,title,procuring_entity,procuring_entity_url,"
    "procurement_type,procedure,procurement_method,evaluation_mechanism,funding_source,"
    "cpv_codes,ppc_ncc_categories,retender_flag,non_petroleum_indicator,"
    "special_differential_treatment,framework_agreement_establishment,"
    "contract_awarded_in_lots,number_of_stages,submission_deadline,"
    "original_submission_deadline,clarification_period_end,bid_opening_date,"
    "site_visit_date,publication_date,bid_deadline_days_remaining,"
    "pdf_url,detail_url,description,detailed_description,"
    "country_contract_performance,project_reference_number,"
    "services_subtype,procurement_technique"
)

ANALYSIS_COLS = (
    "competition_unique_id,resource_id,contract_title,procuring_entity,"
    "contract_type,contract_value,contract_duration,scope_of_work,"
    "submission_deadline,eligibility_requirements,experience_requirements,"
    "financial_requirements,mandatory_documents,evaluation_criteria,"
    "key_milestones,lots,special_conditions,suitability_summary,"
    "source_files,analysis_timestamp"
)


def _days_remaining(deadline_str: Optional[str]) -> Optional[int]:
    if not deadline_str:
        return None
    try:
        dt = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        return max(0, delta.days)
    except Exception:
        return None


def _clean_list(val: Any) -> List[str]:
    """Normalise an analysis array field — remove None entries and duplicates."""
    if not val:
        return []
    if isinstance(val, list):
        seen = set()
        out = []
        for item in val:
            if item is None:
                continue
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out
    return [str(val).strip()] if str(val).strip() else []


def _clean_milestones(val: Any) -> List[Milestone]:
    if not val or not isinstance(val, list):
        return []
    out = []
    seen = set()
    for item in val:
        if not isinstance(item, dict):
            continue
        event = (item.get("event") or "").strip()
        date  = item.get("date")
        key   = (event, date)
        if event and key not in seen:
            seen.add(key)
            out.append(Milestone(event=event, date=date))
    return out


def _fetch_all_tenders(db: SupabaseClient) -> List[Dict]:
    """Paginate through all rows in gojep_tenders_current."""
    rows = []
    page_size = 1000
    offset = 0
    while True:
        batch = (
            db.supabase.table(TABLE_TENDERS)
            .select(TENDERS_LIST_COLS)
            .range(offset, offset + page_size - 1)
            .execute()
            .data or []
        )
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def _fetch_all_analysis(db: SupabaseClient) -> Dict[str, Dict]:
    """Return analysis rows keyed by competition_unique_id."""
    rows = []
    page_size = 1000
    offset = 0
    while True:
        batch = (
            db.supabase.table(TABLE_ANALYSIS)
            .select(ANALYSIS_COLS)
            .range(offset, offset + page_size - 1)
            .execute()
            .data or []
        )
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return {r["competition_unique_id"]: r for r in rows if r.get("competition_unique_id")}


def _merge_to_summary(t: Dict, a: Optional[Dict]) -> TenderSummary:
    uid = t.get("competition_unique_id") or ""
    title = t.get("title") or (a or {}).get("contract_title") or "Untitled"
    deadline = t.get("submission_deadline") or (a or {}).get("submission_deadline")
    return TenderSummary(
        competition_unique_id=uid,
        resource_id=t.get("resource_id"),
        title=title,
        procuring_entity=t.get("procuring_entity") or (a or {}).get("procuring_entity"),
        procurement_type=t.get("procurement_type"),
        procedure=t.get("procedure"),
        description=t.get("description"),
        submission_deadline=deadline,
        publication_date=t.get("publication_date"),
        days_remaining=_days_remaining(deadline),
        contract_type=(a or {}).get("contract_type"),
        contract_title=(a or {}).get("contract_title"),
        scope_of_work=(a or {}).get("scope_of_work"),
        contract_value=(a or {}).get("contract_value"),
        contract_duration=(a or {}).get("contract_duration"),
        eligibility_requirements=_clean_list((a or {}).get("eligibility_requirements")),
        experience_requirements=_clean_list((a or {}).get("experience_requirements")),
        financial_requirements=_clean_list((a or {}).get("financial_requirements")),
        mandatory_documents=_clean_list((a or {}).get("mandatory_documents")),
        evaluation_criteria=_clean_list((a or {}).get("evaluation_criteria")),
        special_conditions=_clean_list((a or {}).get("special_conditions")),
        suitability_summary=(a or {}).get("suitability_summary"),
        procurement_method=t.get("procurement_method"),
        funding_source=t.get("funding_source"),
        cpv_codes=t.get("cpv_codes") or [],
        ppc_ncc_categories=t.get("ppc_ncc_categories") or [],
        retender_flag=t.get("retender_flag"),
        pdf_url=t.get("pdf_url"),
        detail_url=t.get("detail_url"),
        has_analysis=a is not None,
    )


def _merge_to_detail(t: Dict, a: Optional[Dict]) -> TenderDetail:
    uid = t.get("competition_unique_id") or ""
    deadline = t.get("submission_deadline") or (a or {}).get("submission_deadline")
    return TenderDetail(
        competition_unique_id=uid,
        resource_id=t.get("resource_id"),
        title=t.get("title") or (a or {}).get("contract_title") or "Untitled",
        procuring_entity=t.get("procuring_entity") or (a or {}).get("procuring_entity"),
        procuring_entity_url=t.get("procuring_entity_url"),
        contract_type=(a or {}).get("contract_type"),
        procurement_type=t.get("procurement_type"),
        procedure=t.get("procedure"),
        services_subtype=t.get("services_subtype"),
        procurement_method=t.get("procurement_method"),
        evaluation_mechanism=t.get("evaluation_mechanism"),
        procurement_technique=t.get("procurement_technique"),
        funding_source=t.get("funding_source"),
        cpv_codes=t.get("cpv_codes") or [],
        ppc_ncc_categories=t.get("ppc_ncc_categories") or [],
        retender_flag=t.get("retender_flag"),
        non_petroleum_indicator=t.get("non_petroleum_indicator"),
        special_differential_treatment=t.get("special_differential_treatment"),
        framework_agreement_establishment=t.get("framework_agreement_establishment"),
        contract_awarded_in_lots=t.get("contract_awarded_in_lots"),
        number_of_stages=t.get("number_of_stages"),
        description=t.get("description"),
        detailed_description=t.get("detailed_description"),
        contract_title=(a or {}).get("contract_title"),
        scope_of_work=(a or {}).get("scope_of_work"),
        contract_value=(a or {}).get("contract_value"),
        contract_duration=(a or {}).get("contract_duration"),
        submission_deadline=deadline,
        original_submission_deadline=t.get("original_submission_deadline"),
        clarification_period_end=t.get("clarification_period_end"),
        bid_opening_date=t.get("bid_opening_date"),
        site_visit_date=t.get("site_visit_date"),
        publication_date=t.get("publication_date"),
        days_remaining=_days_remaining(deadline),
        key_milestones=_clean_milestones((a or {}).get("key_milestones")),
        eligibility_requirements=_clean_list((a or {}).get("eligibility_requirements")),
        experience_requirements=_clean_list((a or {}).get("experience_requirements")),
        financial_requirements=_clean_list((a or {}).get("financial_requirements")),
        mandatory_documents=_clean_list((a or {}).get("mandatory_documents")),
        evaluation_criteria=_clean_list((a or {}).get("evaluation_criteria")),
        special_conditions=_clean_list((a or {}).get("special_conditions")),
        lots=[x for x in ((a or {}).get("lots") or []) if x is not None],
        country_contract_performance=t.get("country_contract_performance"),
        project_reference_number=t.get("project_reference_number"),
        pdf_url=t.get("pdf_url"),
        detail_url=t.get("detail_url"),
        source_files=(a or {}).get("source_files") or [],
        suitability_summary=(a or {}).get("suitability_summary"),
        analysis_timestamp=(a or {}).get("analysis_timestamp"),
        has_analysis=a is not None,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "name": "GOJEP Tender API",
        "version": "1.0.0",
        "docs": "/docs",
    }


@app.get("/api/v1/tenders", response_model=TenderListResponse, tags=["Tenders"])
def list_tenders(
    q: Optional[str] = Query(None, description="Search term — matches title, procuring entity, description, scope of work"),
    contract_type: Optional[str] = Query(None, description="Filter by contract type: Works, Goods, Services, Consultancy"),
    procurement_type: Optional[str] = Query(None, description="Filter by procurement type from GOJEP (e.g. Goods, Works)"),
    funding_source: Optional[str] = Query(None, description="Filter by funding source (partial match)"),
    cpv: Optional[str] = Query(None, description="Filter by CPV code prefix (e.g. '45350000')"),
    deadline_after: Optional[str] = Query(None, description="Only tenders with deadline after this date (ISO 8601, e.g. 2026-04-01)"),
    deadline_before: Optional[str] = Query(None, description="Only tenders with deadline before this date (ISO 8601)"),
    closing_within_days: Optional[int] = Query(None, description="Only tenders closing within N days from now"),
    has_analysis: Optional[bool] = Query(None, description="Filter to tenders with (true) or without (false) LLM analysis"),
    sort: str = Query("deadline_asc", description="Sort order: deadline_asc, deadline_desc, published_desc, entity_asc"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=100, description="Results per page"),
):
    """
    List open tenders with optional search and filtering.
    Results are a merged view of procurement data and LLM analysis.
    """
    db = get_db()

    tenders = _fetch_all_tenders(db)
    analysis_map = _fetch_all_analysis(db)

    now = datetime.now(timezone.utc)

    # ── Apply filters ─────────────────────────────────────────────────────────
    results = []
    q_lower = q.lower().strip() if q else None

    for t in tenders:
        uid = t.get("competition_unique_id") or ""
        a = analysis_map.get(uid)

        # has_analysis filter
        if has_analysis is True and a is None:
            continue
        if has_analysis is False and a is not None:
            continue

        # contract_type filter (from analysis)
        if contract_type:
            ct = (a or {}).get("contract_type") or ""
            if contract_type.lower() not in ct.lower():
                continue

        # procurement_type filter (from tenders_current)
        if procurement_type:
            pt = t.get("procurement_type") or ""
            if procurement_type.lower() not in pt.lower():
                continue

        # funding_source filter
        if funding_source:
            fs = t.get("funding_source") or ""
            if funding_source.lower() not in fs.lower():
                continue

        # CPV filter
        if cpv:
            codes = t.get("cpv_codes") or []
            if not any(cpv in str(c) for c in codes):
                continue

        # Deadline filters
        deadline_str = t.get("submission_deadline")
        deadline_dt = None
        if deadline_str:
            try:
                deadline_dt = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
                if deadline_dt.tzinfo is None:
                    deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        if closing_within_days is not None:
            if deadline_dt is None:
                continue
            delta_days = (deadline_dt - now).days
            if delta_days < 0 or delta_days > closing_within_days:
                continue

        if deadline_after:
            try:
                da = datetime.fromisoformat(deadline_after).replace(tzinfo=timezone.utc)
                if deadline_dt is None or deadline_dt < da:
                    continue
            except Exception:
                pass

        if deadline_before:
            try:
                db_ = datetime.fromisoformat(deadline_before).replace(tzinfo=timezone.utc)
                if deadline_dt is None or deadline_dt > db_:
                    continue
            except Exception:
                pass

        # Text search
        if q_lower:
            searchable = " ".join(filter(None, [
                t.get("title") or "",
                t.get("procuring_entity") or "",
                t.get("description") or "",
                t.get("detailed_description") or "",
                (a or {}).get("scope_of_work") or "",
                (a or {}).get("contract_title") or "",
            ])).lower()
            if q_lower not in searchable:
                continue

        results.append((t, a, deadline_dt))

    # ── Sort ──────────────────────────────────────────────────────────────────
    EPOCH = datetime.min.replace(tzinfo=timezone.utc)
    EPOCH_LATE = datetime.max.replace(tzinfo=timezone.utc)

    if sort == "deadline_asc":
        results.sort(key=lambda x: x[2] or EPOCH_LATE)
    elif sort == "deadline_desc":
        results.sort(key=lambda x: x[2] or EPOCH, reverse=True)
    elif sort == "published_desc":
        def _pub(x):
            s = x[0].get("publication_date") or ""
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except Exception:
                return EPOCH
        results.sort(key=_pub, reverse=True)
    elif sort == "entity_asc":
        results.sort(key=lambda x: (x[0].get("procuring_entity") or "").lower())

    # ── Paginate ──────────────────────────────────────────────────────────────
    total = len(results)
    offset = (page - 1) * per_page
    page_results = results[offset: offset + per_page]

    return TenderListResponse(
        total=total,
        page=page,
        per_page=per_page,
        has_next=(offset + per_page) < total,
        data=[_merge_to_summary(t, a) for t, a, _ in page_results],
    )


@app.get("/api/v1/tenders/stats", response_model=StatsResponse, tags=["Tenders"])
def tender_stats():
    """
    Summary statistics for dashboard widgets.
    Returns counts by contract type, procurement type, and funding source,
    plus urgency buckets (closing within 7 / 30 days).
    """
    db = get_db()

    tenders = _fetch_all_tenders(db)
    analysis_map = _fetch_all_analysis(db)

    now = datetime.now(timezone.utc)

    total_open = 0
    closing_7  = 0
    closing_30 = 0
    by_contract_type: Dict[str, int] = defaultdict(int)
    by_procurement_type: Dict[str, int] = defaultdict(int)
    by_funding_source: Dict[str, int] = defaultdict(int)
    analysed = 0
    not_analysed = 0

    for t in tenders:
        uid = t.get("competition_unique_id") or ""
        a = analysis_map.get(uid)

        total_open += 1

        # Urgency
        dl = t.get("submission_deadline")
        if dl:
            try:
                dt = datetime.fromisoformat(dl.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = (dt - now).days
                if 0 <= days <= 7:
                    closing_7 += 1
                if 0 <= days <= 30:
                    closing_30 += 1
            except Exception:
                pass

        # Contract type (from analysis)
        ct = (a or {}).get("contract_type") or t.get("procurement_type") or "Unknown"
        by_contract_type[ct] += 1

        # Procurement type
        pt = t.get("procurement_type") or "Unknown"
        by_procurement_type[pt] += 1

        # Funding source
        fs = t.get("funding_source") or "Unknown"
        by_funding_source[fs] += 1

        if a:
            analysed += 1
        else:
            not_analysed += 1

    return StatsResponse(
        total_open=total_open,
        closing_within_7_days=closing_7,
        closing_within_30_days=closing_30,
        by_contract_type=dict(by_contract_type),
        by_procurement_type=dict(by_procurement_type),
        by_funding_source=dict(by_funding_source),
        analysed=analysed,
        not_yet_analysed=not_analysed,
    )


@app.get("/api/v1/tenders/{tender_id:path}", response_model=TenderDetail, tags=["Tenders"])
def get_tender(tender_id: str):
    """
    Full detail for a single tender, including LLM analysis where available.

    `tender_id` can be supplied in either format:
    - Folder name with underscore: `1104_2926`
    - competition_unique_id with slash: `1104/2926`

    Both resolve to the same record.
    """
    db = get_db()

    tender_id = tender_id.strip("/")
    if not tender_id:
        raise HTTPException(status_code=400, detail="tender_id is required.")

    # Normalise: accept underscore (folder name) or slash (competition_unique_id)
    # competition_unique_id format is always <number>/<number>
    if "/" not in tender_id:
        # Convert folder name 1104_2926 → 1104/2926 (replace first underscore only)
        competition_unique_id = tender_id.replace("_", "/", 1)
    else:
        competition_unique_id = tender_id

    # Fetch all tenders and filter in Python to avoid URL-encoding issues
    # with slashes in PostgREST query params
    all_tenders = _fetch_all_tenders(db)
    t_rows = [t for t in all_tenders if t.get("competition_unique_id") == competition_unique_id]

    if not t_rows:
        raise HTTPException(status_code=404, detail=f"Tender '{competition_unique_id}' not found.")
    t = t_rows[0]

    # Fetch analysis — filter in Python using the already-loaded map
    all_analysis = _fetch_all_analysis(db)
    a = all_analysis.get(competition_unique_id)

    return _merge_to_detail(t, a)
