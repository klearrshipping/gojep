"""
Gmail API client for fetching GOJEP tender update notifications.

Authentication:
  - Uses OAuth2 desktop app flow (credentials_gojep.json).
  - On first run, opens a browser for consent and saves the token to gmail_token.json.
  - Subsequent runs load the cached token silently, refreshing if expired.
  - Scope: gmail.modify (required to apply the processed label).

Run once interactively to generate token:
    python -m modules.emails.gmail_client

Then all subsequent calls are silent (token auto-refreshes for up to 7 days;
after expiry delete gmail_token.json and re-run the above).
"""

from __future__ import annotations

import base64
import logging
import os
from email.utils import parsedate_to_datetime
from typing import Any

from config import settings as config

logger = logging.getLogger(__name__)

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_gmail_service():
    """
    Authenticate and return a Gmail API Resource object.

    Flow:
      1. If gmail_token.json exists, load credentials from it.
      2. If credentials are expired and have a refresh token, refresh silently.
      3. If no valid credentials exist, run the browser OAuth2 flow and save
         the resulting token to gmail_token.json.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None

    if os.path.exists(config.GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(
            config.GMAIL_TOKEN_FILE, config.GMAIL_SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Gmail token expired — refreshing silently ...")
            creds.refresh(Request())
        else:
            logger.info("No valid Gmail token found — launching browser OAuth flow ...")
            if not os.path.exists(config.GMAIL_CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {config.GMAIL_CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GMAIL_CREDENTIALS_FILE, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Persist the token for next run
        with open(config.GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        logger.info("Gmail token saved to %s", config.GMAIL_TOKEN_FILE)

    return build("gmail", "v1", credentials=creds)


# ── Label helpers ─────────────────────────────────────────────────────────────

def _get_or_create_label(service, label_name: str) -> str:
    """Return the Gmail label ID for label_name, creating it if absent."""
    result = service.users().labels().list(userId="me").execute()
    for label in result.get("labels", []):
        if label["name"] == label_name:
            return label["id"]

    # Create it
    new_label = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute()
    )
    logger.info("Created Gmail label '%s' (id=%s)", label_name, new_label["id"])
    return new_label["id"]


def mark_email_processed(service, message_id: str) -> None:
    """Apply the 'tender-update-processed' label to a message."""
    label_id = _get_or_create_label(service, config.GMAIL_PROCESSED_LABEL)
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id]},
    ).execute()
    logger.debug("Marked message %s as processed.", message_id)


def delete_email(service, message_id: str) -> None:
    """Move a message to Trash (recoverable for 30 days)."""
    service.users().messages().trash(userId="me", id=message_id).execute()
    logger.debug("Trashed message %s.", message_id)


# ── Message decoding ──────────────────────────────────────────────────────────

def _decode_base64(data: str) -> str:
    """Decode URL-safe base64 Gmail payload."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> tuple[str, str]:
    """
    Recursively extract plain-text and HTML body parts from a Gmail message payload.
    Returns (body_plain, body_html).
    """
    body_plain = ""
    body_html = ""
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            body_plain = _decode_base64(data)

    elif mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            body_html = _decode_base64(data)

    elif mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            p, h = _extract_body(part)
            body_plain = body_plain or p
            body_html = body_html or h

    return body_plain, body_html


def _extract_attachments(payload: dict) -> list[str]:
    """Return list of attachment filenames in the message."""
    names: list[str] = []
    mime = payload.get("mimeType", "")
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            filename = part.get("filename", "")
            if filename:
                names.append(filename)
            names.extend(_extract_attachments(part))
    return names


def _parse_raw_message(msg: dict) -> dict[str, Any]:
    """
    Convert a full Gmail API message object into a flat dict:
      message_id, subject, sender, received_at (ISO UTC),
      body_plain, body_html, attachment_filenames
    """
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

    subject = headers.get("subject", "")
    sender = headers.get("from", "")
    date_str = headers.get("date", "")

    received_at = None
    if date_str:
        try:
            received_at = parsedate_to_datetime(date_str).isoformat()
        except Exception:
            received_at = date_str

    body_plain, body_html = _extract_body(payload)
    attachments = _extract_attachments(payload)

    return {
        "message_id": msg["id"],
        "subject": subject,
        "sender": sender,
        "received_at": received_at,
        "body_plain": body_plain,
        "body_html": body_html,
        "attachment_filenames": attachments,
    }


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_unprocessed_emails(service) -> list[dict[str, Any]]:
    """
    Fetch all unprocessed GOJEP notification emails from the inbox.

    Query targets:
      - System emails from jamaica-eproc-noreply@eurodyn.com
      - Direct entity emails from *.gov.jm senders with relevant subject keywords

    Excludes anything already labelled 'tender-update-processed'.

    Returns a list of parsed message dicts (see _parse_raw_message).
    """
    # Ensure the processed label exists before querying (creates it if absent)
    _get_or_create_label(service, config.GMAIL_PROCESSED_LABEL)

    # Query 1 — GOJEP platform system emails (precise sender match).
    # Query 2 — Any sender, but body must contain a gojep.gov.jm URL.
    #   This catches direct entity emails (e.g. @moh.gov.jm site visit notices)
    #   while reliably excluding unrelated gov.jm emails (customs, permits, etc.)
    #   that have no portal link. System emails are excluded from Query 2 to
    #   avoid duplicates.
    queries = [
        f"from:{config.GMAIL_SYSTEM_SENDER} -label:{config.GMAIL_PROCESSED_LABEL}",
        (
            f"gojep.gov.jm "
            f"-from:{config.GMAIL_SYSTEM_SENDER} "
            f"-label:{config.GMAIL_PROCESSED_LABEL}"
        ),
    ]

    seen_ids: set[str] = set()
    messages: list[dict[str, Any]] = []

    for query in queries:
        logger.info("Gmail query: %s", query)
        page_token = None
        while True:
            kwargs = {"userId": "me", "q": query, "maxResults": 500}
            if page_token:
                kwargs["pageToken"] = page_token
            response = service.users().messages().list(**kwargs).execute()

            for stub in response.get("messages", []):
                mid = stub["id"]
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)

                full = (
                    service.users()
                    .messages()
                    .get(userId="me", id=mid, format="full")
                    .execute()
                )
                parsed = _parse_raw_message(full)
                messages.append(parsed)
                logger.info(
                    "Fetched: [%s] %s (from: %s)",
                    parsed["message_id"],
                    parsed["subject"],
                    parsed["sender"],
                )

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    logger.info("Total unprocessed emails fetched: %d", len(messages))
    return messages


# ── Standalone auth / smoke-test ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print("Authenticating with Gmail API ...")
    svc = get_gmail_service()
    print("Authentication successful.\n")

    if "--auth-only" in sys.argv:
        print("Token saved. Exiting (--auth-only).")
        sys.exit(0)

    print("Fetching unprocessed GOJEP emails ...")
    emails = fetch_unprocessed_emails(svc)

    if not emails:
        print("No unprocessed emails found.")
    else:
        print(f"\nFound {len(emails)} email(s):\n")
        for i, e in enumerate(emails, 1):
            print(f"  [{i}] {e['subject']}")
            print(f"       From   : {e['sender']}")
            print(f"       At     : {e['received_at']}")
            print(f"       Body   : {e['body_plain'][:120].strip()!r}...")
            if e["attachment_filenames"]:
                print(f"       Attach : {e['attachment_filenames']}")
            print()
