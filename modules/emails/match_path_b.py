"""
Match Path B (direct entity) emails to open tenders in gojep_tenders_current.

Strategy:
  1. Extract candidate tenders using keyword search on title + procuring_entity
     (using words from the email's tender_title and procuring entity if present).
  2. Feed the email body + candidate list to the LLM.
  3. LLM returns the best matching resource_id with a confidence score,
     or null if no confident match found.
  4. If confidence >= threshold: return the matched DB row.
  5. If no match or low confidence: return None → caller flags for manual review.
"""

from __future__ import annotations

import json
import logging
import re
import requests

logger = logging.getLogger(__name__)

MATCH_CONFIDENCE_THRESHOLD = 0.70   # below this → manual review

_MATCH_SYSTEM_PROMPT = """\
You are matching a procurement notification email to an open tender in a database.

You will be given:
1. The email subject and body
2. A list of open tenders (resource_id, title, procuring_entity, submission_deadline)

Your task: identify which tender (if any) this email is about.

Return ONLY a valid JSON object:
{
  "resource_id": "the matching resource_id as a string, or null if no confident match",
  "confidence": 0.0 to 1.0,
  "reasoning": "one sentence explaining your match or why no match was found"
}

Rules:
- Only return a match if you are confident (confidence >= 0.70).
- If multiple tenders are plausible but none is clear, return null.
- If the email mentions a tender title or reference number that clearly matches one entry, return it.
- Return ONLY the JSON object — no markdown, no commentary.
"""


def _fetch_candidate_tenders(db, tender_title: str | None, procuring_entity: str | None) -> list[dict]:
    """
    Pull up to 20 candidate tenders from gojep_tenders_current using keyword
    search on title. Falls back to all current tenders (up to 50) if no title.
    """
    from config import settings as config

    table = db.supabase.table(config.SUPABASE_TABLE_TENDERS_CURRENT)

    try:
        if tender_title:
            # Extract meaningful keywords (words > 4 chars)
            keywords = [w for w in re.split(r"\s+", tender_title) if len(w) > 4]
            keyword = keywords[0] if keywords else tender_title[:20]

            result = table.select(
                "resource_id, title, procuring_entity, submission_deadline"
            ).ilike("title", f"%{keyword}%").limit(20).execute()
        else:
            result = table.select(
                "resource_id, title, procuring_entity, submission_deadline"
            ).limit(50).execute()

        return result.data or []
    except Exception as e:
        logger.warning("Candidate tender fetch failed: %s", e)
        return []


def match_path_b_to_tender(db, parsed: dict) -> dict | None:
    """
    Attempt to match a Path B email to an open tender via LLM.

    Returns the matched DB row dict (with _source_table set) if confident,
    or None if no confident match found.
    """
    from config import settings as config

    subject      = parsed.get("subject", "")
    body         = parsed.get("body_plain", "") or ""
    tender_title = parsed.get("tender_title")
    procuring_entity = parsed.get("sender", "")

    candidates = _fetch_candidate_tenders(db, tender_title, procuring_entity)

    if not candidates:
        logger.info("Path B match: no candidates found in gojep_tenders_current")
        return None

    # Format candidate list for the LLM
    candidate_lines = "\n".join(
        f"  - resource_id={c['resource_id']} | title={c['title']} "
        f"| entity={c.get('procuring_entity', '')} "
        f"| deadline={c.get('submission_deadline', '')}"
        for c in candidates
    )

    user_content = (
        f"Subject: {subject}\n\n"
        f"Email body:\n{body[:3000]}\n\n"
        f"Open tenders ({len(candidates)}):\n{candidate_lines}"
    )

    payload = {
        "model":       config.OPENROUTER_MODELS[config.CLASSIFIER_MODEL],
        "messages":    [
            {"role": "system", "content": _MATCH_SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
        "max_tokens":  256,
        "temperature": 0.0,
    }

    try:
        resp = requests.post(
            config.OPENROUTER_URL,
            headers=config.OPENROUTER_HEADERS,
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()

        if content.startswith("```"):
            content = re.sub(r"^```[^\n]*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)

        match_result = json.loads(content)
    except Exception as e:
        logger.warning("Path B LLM match failed: %s", e)
        return None

    resource_id = match_result.get("resource_id")
    confidence  = float(match_result.get("confidence", 0.0))
    reasoning   = match_result.get("reasoning", "")

    logger.info(
        "Path B match result: resource_id=%s confidence=%.2f reasoning=%s",
        resource_id, confidence, reasoning
    )

    if not resource_id or confidence < MATCH_CONFIDENCE_THRESHOLD:
        logger.info("Path B: no confident match (confidence=%.2f)", confidence)
        return None

    # Look up the full DB row for the matched resource_id
    matched = next((c for c in candidates if str(c["resource_id"]) == str(resource_id)), None)
    if matched:
        matched["_source_table"] = config.SUPABASE_TABLE_TENDERS_CURRENT
        matched["_match_confidence"] = confidence
        matched["_match_reasoning"]  = reasoning

    return matched
