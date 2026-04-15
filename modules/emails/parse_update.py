"""
Parse raw email dicts (from gmail_client.fetch_unprocessed_emails) into
structured update records.

Two parsing paths:

  Path A — System emails from jamaica-eproc-noreply@eurodyn.com
    Pure regex. Resource ID extracted from subject or URL.
    Update type inferred from subject keywords.
    No LLM call needed.

  Path B — Direct entity emails (e.g. from @moh.gov.jm)
    LLM extraction via OpenRouter (gemma4_31b).
    Extracts tender title, update type, and any dates mentioned.

Email type taxonomy (covers all observed GOJEP email types):

  update_type                  action_url_type        Action required?
  ─────────────────────────    ────────────────────   ─────────────────
  clarification_response       list_clarification     YES — scrape clarification page
  modifications                prepare_view           YES — re-fetch detail page
  clarification_period_end     list_clarification     NO  — informational reminder
  time_limit                   view_etenders          NO  — deadline < 24h warning
  site_visit                   None                   DB patch only (Path B)
  deadline_extension           None / prepare_view    DB patch + re-fetch
  cancellation                 None                   DB patch
  other                        None                   Log only

Output schema per email:
{
  "email_message_id": str,
  "received_at": str,
  "sender": str,
  "subject": str,
  "path": "A" | "B",
  "resource_id": str | None,
  "tender_title": str | None,
  "update_type": str,
  "action_url": str | None,
  "action_url_type": str | None,   # "list_clarification" | "prepare_view" | "view_etenders" | None
  "extracted_dates": {             # any dates mentioned in the email body
    "submission_deadline": str | None,
    "clarification_period_end": str | None,
    "site_visit_date": str | None,
    "bid_opening_date": str | None,
  },
  "requires_action": bool,         # False for informational-only types
  "raw_summary": str,              # one-liner for logging
}
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

SYSTEM_SENDER = "jamaica-eproc-noreply@eurodyn.com"

# Subject keyword → update_type  (checked in order, first match wins)
_SUBJECT_TYPE_MAP: list[tuple[str, str]] = [
    (r"clarification response",           "clarification_response"),
    (r"modifications?",                   "modifications"),
    (r"request for clarifications? end",  "clarification_period_end"),
    (r"time limit for receipt",           "time_limit"),
    (r"extension",                        "deadline_extension"),
    (r"cancell?(ation|ed|led)",            "cancellation"),
    (r"addendum",                         "addendum"),
    (r"site visit",                       "site_visit"),
    (r"bidders? conference",              "site_visit"),
    (r"\bdocuments?\b",                   "new_documents"),
]

# action_url_type by URL path fragment
_URL_TYPE_MAP: list[tuple[str, str]] = [
    ("listClarification.do",    "list_clarification"),
    ("prepareViewCfTWS.do",     "prepare_view"),
    ("viewETenders.do",         "view_etenders"),
]

# Update types that require no downstream action
_INFORMATIONAL_TYPES = {"clarification_period_end", "time_limit"}


# ── Regex helpers ─────────────────────────────────────────────────────────────

_RE_RESOURCE_ID_SUBJECT = re.compile(
    r"\[Competition(?:\s+ref)?:\s*(\d+)\]", re.IGNORECASE
)
_RE_RESOURCE_ID_URL = re.compile(r"resourceId=(\d+)")
_RE_URL = re.compile(r"https://www\.gojep\.gov\.jm/\S+")
_RE_TENDER_TITLE_BODY = re.compile(
    r"competition titled[:\s]+([^\n\.]+)", re.IGNORECASE
)


def _extract_resource_id(subject: str, body: str) -> str | None:
    m = _RE_RESOURCE_ID_SUBJECT.search(subject)
    if m:
        return m.group(1)
    m = _RE_RESOURCE_ID_URL.search(body)
    if m:
        return m.group(1)
    return None


def _classify_update_type(subject: str) -> str:
    s = subject.lower()
    for pattern, utype in _SUBJECT_TYPE_MAP:
        if re.search(pattern, s):
            return utype
    return "other"


def _extract_action_url(body: str) -> tuple[str | None, str | None]:
    """Return (action_url, action_url_type) — first GOJEP URL found in body."""
    m = _RE_URL.search(body)
    if not m:
        return None, None
    url = m.group(0).rstrip(".")
    for fragment, utype in _URL_TYPE_MAP:
        if fragment in url:
            return url, utype
    return url, None


def _extract_tender_title_from_body(body: str) -> str | None:
    m = _RE_TENDER_TITLE_BODY.search(body)
    if m:
        return m.group(1).strip().rstrip(".")
    return None


# ── Path A — system email (regex only) ───────────────────────────────────────

def _parse_path_a(email: dict[str, Any]) -> dict[str, Any]:
    subject   = email.get("subject", "")
    body      = email.get("body_plain", "") or ""
    sender    = email.get("sender", "")

    resource_id  = _extract_resource_id(subject, body)
    update_type  = _classify_update_type(subject)
    action_url, action_url_type = _extract_action_url(body)
    tender_title = _extract_tender_title_from_body(body)

    requires_action = update_type not in _INFORMATIONAL_TYPES

    raw_summary = f"[{update_type}] resource_id={resource_id} — {subject[:80]}"

    return {
        "email_message_id": email["message_id"],
        "received_at":      email.get("received_at"),
        "sender":           sender,
        "subject":          subject,
        "body_plain":       body[:6000],
        "path":             "A",
        "resource_id":      resource_id,
        "tender_title":     tender_title,
        "update_type":      update_type,
        "action_url":       action_url,
        "action_url_type":  action_url_type,
        "extracted_dates": {
            "submission_deadline":      None,
            "clarification_period_end": None,
            "site_visit_date":          None,
            "bid_opening_date":         None,
        },
        "requires_action":  requires_action,
        "raw_summary":      raw_summary,
    }


# ── Path B — direct entity email (LLM extraction) ────────────────────────────

_PATH_B_SYSTEM_PROMPT = """\
You are parsing a procurement notification email sent directly by a Jamaican government entity.

Extract the following fields from the email body and return ONLY a valid JSON object.
If a field cannot be determined, use null.

{
  "tender_title": "Full name of the tender or project as mentioned in the email",
  "update_type": "One of: site_visit | deadline_extension | cancellation | addendum | other",
  "submission_deadline": "Bid deadline if stated — format exactly as written in the email, null if not mentioned",
  "site_visit_date": "Site visit or bidders conference date/time if stated — format exactly as written, null if not mentioned",
  "bid_opening_date": "Bid opening date if stated, null if not mentioned",
  "venue": "Location of site visit or bidders conference if stated, null if not mentioned",
  "contact_email": "Reply-to or contact email address if stated, null if not mentioned",
  "one_line_summary": "One sentence describing what this email is about"
}

Return ONLY the JSON object — no markdown fences, no commentary.
"""


def _parse_path_b(email: dict[str, Any]) -> dict[str, Any]:
    """LLM-based extraction for direct entity emails."""
    import json
    import requests
    from config import settings as config

    subject = email.get("subject", "")
    body    = email.get("body_plain", "") or email.get("body_html", "") or ""
    sender  = email.get("sender", "")

    # Trim body to avoid excessive tokens
    trimmed_body = body[:4000]

    payload = {
        "model":       config.OPENROUTER_MODELS[config.CLASSIFIER_MODEL],
        "messages":    [
            {"role": "system", "content": _PATH_B_SYSTEM_PROMPT},
            {"role": "user",   "content": f"Subject: {subject}\n\n{trimmed_body}"},
        ],
        "max_tokens":  512,
        "temperature": 0.0,
    }

    extracted: dict[str, Any] = {}
    try:
        resp = requests.post(
            config.OPENROUTER_URL,
            headers=config.OPENROUTER_HEADERS,
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()

        # Strip markdown fences if present
        if content.startswith("```"):
            content = re.sub(r"^```[^\n]*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)

        extracted = json.loads(content)
    except Exception as e:
        logger.warning("Path B LLM extraction failed for message %s: %s", email["message_id"], e)

    update_type  = extracted.get("update_type") or "other"
    tender_title = extracted.get("tender_title")
    requires_action = update_type not in _INFORMATIONAL_TYPES

    raw_summary = extracted.get("one_line_summary") or f"[{update_type}] {subject[:80]}"

    return {
        "email_message_id": email["message_id"],
        "received_at":      email.get("received_at"),
        "sender":           sender,
        "subject":          subject,
        "body_plain":       body[:6000],
        "path":             "B",
        "resource_id":      None,
        "tender_title":     tender_title,
        "update_type":      update_type,
        "action_url":       None,
        "action_url_type":  None,
        "extracted_dates": {
            "submission_deadline":      extracted.get("submission_deadline"),
            "clarification_period_end": None,
            "site_visit_date":          extracted.get("site_visit_date"),
            "bid_opening_date":         extracted.get("bid_opening_date"),
        },
        "venue":            extracted.get("venue"),
        "contact_email":    extracted.get("contact_email"),
        "requires_action":  requires_action,
        "raw_summary":      raw_summary,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def parse_email(email: dict[str, Any]) -> dict[str, Any]:
    """
    Route an email to Path A (system) or Path B (entity) parser and return
    a structured update dict.
    """
    sender = email.get("sender", "").lower()

    if SYSTEM_SENDER in sender:
        result = _parse_path_a(email)
    else:
        result = _parse_path_b(email)

    logger.info("Parsed: %s", result["raw_summary"])
    return result


def parse_emails(emails: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse a list of raw email dicts. Returns list of structured update dicts."""
    results = []
    for email in emails:
        try:
            results.append(parse_email(email))
        except Exception as e:
            logger.error(
                "Failed to parse email %s (%s): %s",
                email.get("message_id"), email.get("subject", "")[:60], e,
            )
    return results
