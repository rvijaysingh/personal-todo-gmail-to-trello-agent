"""Gmail API client: fetch starred emails, unstar, apply label.

Run with --reauth to trigger the full OAuth consent flow.
"""

import base64
import email.utils
import logging
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.models import EmailRecord

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
]


class AuthError(RuntimeError):
    """Raised when Gmail OAuth authentication fails or token is expired."""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def build_service(credentials_path: str, token_path: str):
    """Build and return an authenticated Gmail API service object.

    Loads the OAuth2 token from token_path. If the token is expired it is
    refreshed automatically and the updated token is written back to disk.
    If the token is missing or the refresh fails, raises AuthError with
    instructions to run --reauth.

    Args:
        credentials_path: Path to the OAuth2 credentials.json file.
        token_path: Path to the OAuth2 token.json file.

    Returns:
        A Google API Resource object for the Gmail v1 API.

    Raises:
        AuthError: If the token is missing, invalid, or cannot be refreshed.
    """
    creds: Optional[Credentials] = None
    token_p = Path(token_path)

    if token_p.exists():
        creds = Credentials.from_authorized_user_file(str(token_p), SCOPES)
        logger.debug("Loaded OAuth2 token from %s", token_path)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("OAuth2 token expired — refreshing")
            try:
                creds.refresh(Request())
                token_p.write_text(creds.to_json(), encoding="utf-8")
                logger.info("Token refreshed and saved to %s", token_path)
            except Exception as exc:
                raise AuthError(
                    f"Gmail OAuth token refresh failed: {exc}. "
                    "Run: python -m src.gmail_client --reauth"
                ) from exc
        else:
            raise AuthError(
                "Gmail OAuth token is missing or invalid. "
                "Run: python -m src.gmail_client --reauth"
            )

    return build("gmail", "v1", credentials=creds)


def reauth(credentials_path: str, token_path: str) -> None:
    """Run the full OAuth consent flow and save the resulting token.

    Args:
        credentials_path: Path to credentials.json downloaded from Google Cloud Console.
        token_path: Path where the new token.json will be written.
    """
    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
    creds = flow.run_local_server(port=0)
    Path(token_path).write_text(creds.to_json(), encoding="utf-8")
    logger.info("Re-authentication successful. Token saved to %s", token_path)


# ---------------------------------------------------------------------------
# Body extraction helpers
# ---------------------------------------------------------------------------


def _decode_base64url(data: str) -> str:
    """Decode a base64url-encoded string as returned by the Gmail API body data field.

    Args:
        data: Base64url-encoded string (without padding).

    Returns:
        Decoded UTF-8 string, with replacement chars for undecodable bytes.
    """
    if not data:
        return ""
    # Add padding so the length is a multiple of 4
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Base64url decode failed: %s", exc)
        return ""


class _TagStripper(HTMLParser):
    """Minimal HTML tag stripper using the stdlib html.parser."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def get_text(self) -> str:
        return " ".join(chunk for chunk in self._chunks if chunk.strip()).strip()


def _strip_html(html_text: str) -> str:
    """Strip HTML tags and return plain text.

    Args:
        html_text: HTML string to strip.

    Returns:
        Plain text with HTML tags removed.
    """
    stripper = _TagStripper()
    try:
        stripper.feed(html_text)
        return stripper.get_text()
    except Exception as exc:
        logger.warning("HTML stripping failed: %s — returning raw text", exc)
        return html_text


def _extract_body(payload: dict) -> str:
    """Recursively extract the best text representation from a Gmail message payload.

    Prefers text/plain. Falls back to HTML-stripped text/html.
    Recurses into nested multipart structures.

    Args:
        payload: The 'payload' dict from a Gmail messages.get response.

    Returns:
        Extracted body text, or empty string if nothing could be extracted.
    """
    mime = payload.get("mimeType", "")

    # Direct single-part bodies
    if mime == "text/plain":
        return _decode_base64url(payload.get("body", {}).get("data", ""))

    if mime == "text/html":
        raw = _decode_base64url(payload.get("body", {}).get("data", ""))
        return _strip_html(raw) if raw else ""

    # Multipart: search parts in preference order
    parts = payload.get("parts", [])
    if not parts:
        return ""

    # Pass 1: direct text/plain child
    for part in parts:
        if part.get("mimeType") == "text/plain":
            body = _decode_base64url(part.get("body", {}).get("data", ""))
            if body:
                return body

    # Pass 2: recurse into nested multipart
    for part in parts:
        if part.get("mimeType", "").startswith("multipart/"):
            result = _extract_body(part)
            if result:
                return result

    # Pass 3: fall back to text/html
    for part in parts:
        if part.get("mimeType") == "text/html":
            raw = _decode_base64url(part.get("body", {}).get("data", ""))
            if raw:
                return _strip_html(raw)

    return ""


def _parse_email_date(date_header: str, internal_date_ms: Optional[str]) -> str:
    """Parse an email date to ISO 8601 UTC string.

    Tries the Date header first; falls back to internalDate (milliseconds).

    Args:
        date_header: Value of the Date: header (RFC 2822 format).
        internal_date_ms: Gmail internalDate as a millisecond epoch string.

    Returns:
        ISO 8601 date string, or empty string if neither source parses.
    """
    if date_header:
        try:
            dt = email.utils.parsedate_to_datetime(date_header)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc)
            return dt.isoformat()
        except Exception:
            pass

    if internal_date_ms:
        try:
            ts = int(internal_date_ms) / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception:
            pass

    return ""


def _build_email_record(msg: dict) -> EmailRecord:
    """Extract an EmailRecord from a full Gmail messages.get response.

    Args:
        msg: Full message dict from Gmail API (format='full').

    Returns:
        Populated EmailRecord.
    """
    msg_id = msg["id"]
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])

    def get_header(name: str) -> str:
        name_lower = name.lower()
        for h in headers:
            if h.get("name", "").lower() == name_lower:
                return h.get("value", "")
        return ""

    subject = get_header("subject") or "(No Subject)"
    sender = get_header("from") or "(Unknown Sender)"
    date_str = get_header("date")
    email_date = _parse_email_date(date_str, msg.get("internalDate"))

    try:
        body = _extract_body(payload)
    except Exception as exc:
        logger.warning("Body extraction failed for message %s: %s", msg_id, exc)
        body = ""

    if not body:
        logger.warning("No body extracted for message %s", msg_id)
        body = "[Email body could not be extracted]"

    return EmailRecord(
        gmail_message_id=msg_id,
        subject=subject,
        sender=sender,
        email_date=email_date,
        body=body,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_starred_emails(
    service,
    max_age_days: Optional[int] = None,
) -> list[EmailRecord]:
    """Fetch all currently starred emails, returning them sorted oldest first.

    On first run, max_age_days limits the lookback window so the agent
    doesn't process the user's entire starred history.

    Args:
        service: Authenticated Gmail API service object from build_service().
        max_age_days: If provided, only fetch emails newer than this many days.

    Returns:
        List of EmailRecord objects sorted oldest first by internalDate.
    """
    query = "is:starred"
    if max_age_days is not None:
        query += f" newer_than:{max_age_days}d"

    logger.info("Querying Gmail: %r", query)

    # Collect all matching message IDs (paginated)
    message_stubs: list[dict] = []
    page_token: Optional[str] = None
    while True:
        params: dict = {"userId": "me", "q": query}
        if page_token:
            params["pageToken"] = page_token
        result = service.users().messages().list(**params).execute()
        batch = result.get("messages", [])
        message_stubs.extend(batch)
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    if not message_stubs:
        logger.info("No starred emails found")
        return []

    logger.info("Found %d starred email(s) — fetching details", len(message_stubs))

    raw: list[tuple[int, EmailRecord]] = []
    for stub in message_stubs:
        msg_id = stub["id"]
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()
            record = _build_email_record(msg)
            internal_date = int(msg.get("internalDate", 0))
            raw.append((internal_date, record))
        except HttpError as exc:
            logger.error("Gmail API error fetching message %s: %s", msg_id, exc)
        except Exception as exc:
            logger.error("Unexpected error fetching message %s: %s", msg_id, exc)

    # Sort oldest first by internalDate (millisecond epoch)
    raw.sort(key=lambda x: x[0])
    records = [r for _, r in raw]

    if len(records) < len(message_stubs):
        logger.warning(
            "Fetched %d/%d messages successfully", len(records), len(message_stubs)
        )
    return records


def unstar_email(service, message_id: str) -> None:
    """Remove the STARRED label from a Gmail message.

    Args:
        service: Authenticated Gmail API service object.
        message_id: Gmail message ID to unstar.

    Raises:
        HttpError: If the Gmail API returns an error.
    """
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["STARRED"]},
    ).execute()
    logger.info("Removed STARRED label from message %s", message_id)


def apply_label(service, message_id: str, label_name: str) -> None:
    """Apply a named Gmail label to a message.

    Looks up the label ID by name. If the label does not exist, logs an
    error and returns without raising so the pipeline can continue.
    The label must be pre-created by the user in Gmail settings.

    Args:
        service: Authenticated Gmail API service object.
        message_id: Gmail message ID to label.
        label_name: Exact display name of the label (e.g. "Agent/Added-To-Trello").
    """
    try:
        result = service.users().labels().list(userId="me").execute()
        labels = result.get("labels", [])
    except HttpError as exc:
        logger.error("Failed to list Gmail labels: %s", exc)
        return

    label_id: Optional[str] = next(
        (lbl["id"] for lbl in labels if lbl.get("name") == label_name),
        None,
    )

    if label_id is None:
        logger.error(
            "Gmail label '%s' not found. Create it manually in Gmail settings "
            "then re-run. Message %s was NOT labeled.",
            label_name,
            message_id,
        )
        return

    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()
        logger.info("Applied label '%s' to message %s", label_name, message_id)
    except HttpError as exc:
        logger.error(
            "Failed to apply label '%s' to message %s: %s", label_name, message_id, exc
        )


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Gmail client for the Gmail-to-Trello agent"
    )
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="Run the full OAuth consent flow to create/replace token.json",
    )
    args = parser.parse_args()

    try:
        from src.config_loader import load_config

        gc, _ = load_config()
    except Exception as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    if args.reauth:
        reauth(gc.gmail_oauth_credentials_path, gc.gmail_oauth_token_path)
        print("Re-authentication complete.")
        sys.exit(0)

    try:
        svc = build_service(gc.gmail_oauth_credentials_path, gc.gmail_oauth_token_path)
    except AuthError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    emails = fetch_starred_emails(svc)
    if not emails:
        print("No starred emails found.")
    else:
        print(f"Found {len(emails)} starred email(s):")
        for em in emails:
            print(f"  [{em.email_date}] {em.subject!r} — {em.sender}")
