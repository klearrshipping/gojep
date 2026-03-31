"""
Maps extracted Award Listings and Award Details into clean database rows.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

def load_awards_array(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    raise ValueError(f"Expected array in {path}")

def load_award_details_payload(path: str) -> Dict[str, Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    records = []
    if isinstance(data, dict) and "records" in data:
        records = data["records"]
    elif isinstance(data, list):
        records = data
        
    out = {}
    for r in records:
        rid = r.get("resource_id")
        if rid:
            out[rid] = r
    return out

def merge_awards_and_details(
    awards: List[Dict[str, Any]], 
    details: Dict[str, Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """Merge base award entries with deeply extracted PDF properties."""
    
    merged: Dict[str, Dict[str, Any]] = {}
    
    for row in awards:
        rid = row.get("resource_id")
        if not rid:
            continue
            
        combined = dict(row)
        
        # Merge parsed date immediately
        award_date_raw = row.get("award_date_raw")
        if award_date_raw:
            # Already passed through `_parse_award_datetime` in get_awards.py, which outputs `award_date` as ISO.
            combined["award_date_parsed"] = row.get("award_date")
            
        detail_record = details.get(rid)
        if detail_record:
            extracted = detail_record.get("extracted", {})
            parsed = extracted.get("parsed_fields", {})
            
            # Map new deep fields safely
            for key in [
                "official_name", "postal_address", "tender_reference_number",
                "name_of_contractor", "contract_price_amount", "contract_price_currency",
                "level_of_competition", "contract_award_criteria", "funding_source",
                "funding_providers", "principal_site_of_performance", "commencement_date",
                "duration", "justification", "date_of_dispatch_of_notice"
            ]:
                if key in parsed:
                    combined[key] = parsed[key]
                    
            if "ppc_category_code_and_title" in parsed:
                combined["ppc_category_code_and_title"] = parsed["ppc_category_code_and_title"]
            if "cpv_codes" in parsed:
                combined["cpv_codes"] = parsed["cpv_codes"]
                
            # If the PDF provided a procurement_type from checkboxes
            if "procurement_type" in parsed:
                combined["procurement_type"] = parsed["procurement_type"]
                
        merged[rid] = combined
        
    return merged
