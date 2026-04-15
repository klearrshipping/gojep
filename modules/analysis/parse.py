"""
LLM response parsing, validation, and multi-chunk result merging.
"""

import json
import logging
from typing import Any, Dict, List

from modules.analysis.prompt import ANALYSIS_OUTPUT_FIELDS, LIST_FIELDS, REQUIRED_FIELDS

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
