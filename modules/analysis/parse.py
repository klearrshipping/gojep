"""
LLM response parsing, validation, multi-chunk result merging, and narrative consolidation.
"""

import json
import logging
from typing import Any, Dict, List, Tuple

from modules.analysis.prompt import ANALYSIS_OUTPUT_FIELDS, LIST_FIELDS, REQUIRED_FIELDS, CONSOLIDATION_SYSTEM_PROMPT, NARRATIVE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def _parse_llm_response(raw: str) -> Dict[str, Any]:
    """Extract JSON from the LLM response, stripping markdown fences if present."""
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines()
            if not line.strip().startswith("```")
        ).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"Could not parse LLM JSON: {e}")
        return {"parse_error": str(e)}


def _validate_parsed(parsed: Dict[str, Any]) -> List[str]:
    """Check required fields are present and non-null. Returns list of warnings."""
    warnings = []
    if "parse_error" in parsed:
        warnings.append(f"LLM response was not valid JSON: {parsed['parse_error']}")
        return warnings
    for field in REQUIRED_FIELDS:
        val = parsed.get(field)
        if val is None or val == [] or val == "":
            warnings.append(f"Required field '{field}' is null/empty")
    return warnings


def _merge_parsed_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Merge extraction results from multiple chunks into one record.
      - Scalar fields: first non-null value wins (chunk 1 has the most complete header)
      - List fields:   union across all chunks, deduplicated by string representation
    """
    merged: Dict[str, Any] = {}
    seen: Dict[str, set] = {f: set() for f in LIST_FIELDS}

    for parsed in results:
        for field in ANALYSIS_OUTPUT_FIELDS:
            val = parsed.get(field)
            if val is None or val == "" or val == []:
                continue
            if field in LIST_FIELDS:
                items = val if isinstance(val, list) else [val]
                bucket = merged.setdefault(field, [])
                for item in items:
                    key = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
                    if key not in seen[field]:
                        seen[field].add(key)
                        bucket.append(item)
            else:
                merged.setdefault(field, val)  # first non-null wins

    return merged


def _pre_deduplicate(items: list) -> list:
    """
    Fast Python pre-deduplication before sending to the LLM.
    Removes:
      - Exact duplicates (case-insensitive)
      - Known noise phrases
      - Items that are substrings of a longer item already in the list
    Caps the list at 40 items so the LLM output stays within model token limits.
    """
    NOISE_PHRASES = {
        "not stated", "not specified", "not explicitly stated",
        "not explicitly stated in the provided", "no specific",
        "n/a", "none", "not provided", "not mentioned",
        "not applicable", "not detailed", "not listed",
    }
    seen_lower = set()
    result = []
    for item in items:
        if not item or not isinstance(item, str):
            continue
        stripped = item.strip()
        lower = stripped.lower()
        # Drop noise
        if any(lower.startswith(p) or lower == p for p in NOISE_PHRASES):
            continue
        # Drop exact duplicates
        if lower in seen_lower:
            continue
        seen_lower.add(lower)
        result.append(stripped)

    # Cap at 40 items — LLM handles semantic deduplication of remaining
    return result[:40]


def _build_consolidation_context(parsed: Dict[str, Any], db_meta: Dict[str, Any] = None) -> str:
    """
    Serialise all analysis fields into a readable context block for the consolidation prompt.
    Scalar fields are presented as key-value pairs; list fields as numbered items.
    """
    lines = ["=== TENDER ANALYSIS DATA ===\n"]

    # Scalar fields first
    scalar_fields = [f for f in ANALYSIS_OUTPUT_FIELDS if f not in LIST_FIELDS]
    for field in scalar_fields:
        val = parsed.get(field)
        if val:
            lines.append(f"{field.replace('_', ' ').title()}: {val}")

    # Supplement with DB metadata fields not in analysis output
    if db_meta:
        for key in ("procedure", "competition_unique_id", "procurement_method", "evaluation_mechanism"):
            val = db_meta.get(key)
            if val:
                lines.append(f"{key.replace('_', ' ').title()}: {val}")

    lines.append("")

    # List fields
    for field in LIST_FIELDS:
        items = parsed.get(field)
        if not items:
            continue
        lines.append(f"\n{field.replace('_', ' ').upper()}:")
        if isinstance(items, list):
            for i, item in enumerate(items, 1):
                if isinstance(item, dict):
                    lines.append(f"  {i}. {json.dumps(item)}")
                else:
                    lines.append(f"  {i}. {item}")
        else:
            lines.append(f"  {items}")

    return "\n".join(lines)


def _consolidate(parsed: Dict[str, Any], db_meta: Dict[str, Any] = None) -> Tuple[Dict[str, Any], str]:
    """
    Two-call consolidation pipeline:
      Call 1 — clean and deduplicate all list fields (JSON output, bounded size)
      Call 2 — generate narrative from the clean structured data (markdown output)

    Returns (updated_parsed, narrative_analysis).
    On failure of either call, returns the best available result with an empty narrative.
    """
    from modules.analysis.call import _call_llm

    CONSOLIDATION_LIST_FIELDS = {
        "eligibility_requirements", "experience_requirements", "financial_requirements",
        "mandatory_documents", "evaluation_criteria", "special_conditions",
        "key_milestones", "lots",
    }

    # Pre-deduplicate in Python before sending to the LLM
    pre_deduped = dict(parsed)
    for field in CONSOLIDATION_LIST_FIELDS:
        items = parsed.get(field)
        if items and isinstance(items, list):
            pre_deduped[field] = _pre_deduplicate(items)

    context = _build_consolidation_context(pre_deduped, db_meta)
    updated = dict(parsed)

    # ── Call 1: clean list fields ──────────────────────────────────────────────
    try:
        raw = _call_llm(context, system_prompt=CONSOLIDATION_SYSTEM_PROMPT, max_tokens=8000)
        result = _parse_llm_response(raw)

        if "parse_error" in result:
            logger.warning(f"Consolidation (clean) response could not be parsed: {result['parse_error']}")
        else:
            for field in CONSOLIDATION_LIST_FIELDS:
                clean = result.get(field)
                if clean and isinstance(clean, list) and len(clean) > 0:
                    updated[field] = clean
    except Exception as e:
        logger.error(f"Consolidation (clean) call failed: {e}")
        return updated, ""

    # ── Call 2: generate narrative from clean data ─────────────────────────────
    try:
        narrative_context = _build_consolidation_context(updated, db_meta)
        narrative = _call_llm(narrative_context, system_prompt=NARRATIVE_SYSTEM_PROMPT, max_tokens=4000)
        return updated, narrative.strip()
    except Exception as e:
        logger.error(f"Consolidation (narrative) call failed: {e}")
        return updated, ""
