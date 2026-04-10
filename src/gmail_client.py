"""Gmail IMAP client: fetch starred emails, apply label.

Uses IMAP with an app password (no OAuth2 required). Connects to
imap.gmail.com:993 and uses Gmail's X-GM-LABELS and X-GM-MSGID IMAP
extensions for label operations.

Run standalone to verify credentials and list starred emails:
    python -m src.gmail_client
"""

import email as email_lib
import email.utils
import imaplib
import logging
import re
import socket
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Optional

from src.models import EmailRecord

logger = logging.getLogger(__name__)

_IMAP_HOST = "imap.gmail.com"
_IMAP_PORT = 993


# ---------------------------------------------------------------------------
# Body extraction helpers
# ---------------------------------------------------------------------------


class _TagStripper(HTMLParser):
    """HTML tag stripper that removes style/script/head block contents and HTML comments.

    Uses convert_charrefs=True so HTML entities (&amp;, &nbsp;, etc.) are
    automatically decoded in handle_data rather than surfaced as raw text.
    """

    _SKIP_TAGS: frozenset[str] = frozenset({"style", "script", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth: int = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def handle_comment(self, data: str) -> None:
        pass  # Drop HTML comments entirely

    def get_text(self) -> str:
        """Return cleaned plain text from collected data chunks.

        - Replaces non-breaking spaces (\\xa0) with regular spaces.
        - Strips leading/trailing whitespace from each line.
        - Collapses runs of blank lines to at most two consecutive blank lines.
        - Strips leading/trailing blank lines from the final result.
        """
        raw = "".join(self._chunks)
        raw = raw.replace("\xa0", " ")  # non-breaking space -> regular space
        lines = raw.splitlines()
        stripped = [line.strip() for line in lines]

        # Collapse consecutive blank lines to at most 2
        result: list[str] = []
        blank_run = 0
        for line in stripped:
            if not line:
                blank_run += 1
                if blank_run <= 2:
                    result.append(line)
            else:
                blank_run = 0
                result.append(line)

        return "\n".join(result).strip()


def _strip_html(html_text: str) -> str:
    """Strip HTML tags and return plain text.

    Removes the *contents* of <style>, <script>, and <head> elements entirely.
    Drops HTML comments. Decodes HTML entities. Collapses excessive blank lines.

    Args:
        html_text: HTML string to process.

    Returns:
        Plain text with HTML markup removed and whitespace normalised.
    """
    stripper = _TagStripper()
    try:
        stripper.feed(html_text)
        return stripper.get_text()
    except Exception as exc:
        logger.warning("HTML stripping failed: %s — returning raw text", exc)
        return html_text


def _parse_email_date(date_header: str) -> str:
    """Parse an email Date header to ISO 8601 UTC string.

    Args:
        date_header: Value of the Date: header (RFC 2822 format).

    Returns:
        ISO 8601 date string, or empty string if parsing fails.
    """
    if not date_header:
        return ""
    try:
        dt = email.utils.parsedate_to_datetime(date_header)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()
    except Exception:
        return ""


def _extract_body_from_message(msg: email_lib.message.Message) -> str:
    """Extract the best text representation from a parsed email.message.Message.

    Prefers text/plain. Falls back to HTML-stripped text/html.
    Walks multipart structures to find text parts.

    Args:
        msg: Parsed email.message.Message object (from email.message_from_bytes).

    Returns:
        Extracted body text, or empty string if nothing could be extracted.
    """
    plain: Optional[str] = None
    html_text: Optional[str] = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and plain is None:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    plain = payload.decode(charset, errors="replace")
            elif ct == "text/html" and html_text is None:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html_text = payload.decode(charset, errors="replace")
    else:
        ct = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            content = payload.decode(charset, errors="replace")
            if ct == "text/plain":
                plain = content
            elif ct == "text/html":
                html_text = content

    if plain:
        return plain
    if html_text:
        return _strip_html(html_text)
    return ""


# ---------------------------------------------------------------------------
# IMAP connection helper
# ---------------------------------------------------------------------------


def _imap_connect(gmail_sender: str, gmail_password: str) -> imaplib.IMAP4_SSL:
    """Create an authenticated IMAP4_SSL connection to Gmail.

    Args:
        gmail_sender: Gmail address (e.g. user@gmail.com).
        gmail_password: App password from Google Account → Security → App passwords.

    Returns:
        Authenticated IMAP4_SSL object.

    Raises:
        imaplib.IMAP4.error: On login failure (bad credentials or IMAP disabled).
        socket.timeout: On connection timeout.
        ConnectionRefusedError: If the server port is not accepting connections.
        OSError: On other network-level failures.
    """
    imap = imaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT)
    imap.login(gmail_sender, gmail_password)
    return imap


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_imap_auth(gmail_sender: str, gmail_password: str) -> None:
    """Verify IMAP credentials by connecting and immediately disconnecting.

    Call once at agent startup to catch bad credentials before processing
    begins. On success, returns normally. On failure, raises.

    Args:
        gmail_sender: Gmail address.
        gmail_password: Gmail app password.

    Raises:
        imaplib.IMAP4.error: If authentication fails (bad credentials, IMAP disabled).
        socket.timeout: If the server is unreachable (timeout).
        ConnectionRefusedError: If the server port is closed.
        OSError: On other network-level failures.
    """
    imap = None
    try:
        imap = _imap_connect(gmail_sender, gmail_password)
        logger.debug("IMAP auth check successful for %s", gmail_sender)
    finally:
        if imap:
            try:
                imap.logout()
            except Exception:
                pass


def fetch_starred_emails(
    gmail_sender: str,
    gmail_password: str,
    max_age_days: Optional[int] = None,
    processed_label: Optional[str] = None,
) -> list[EmailRecord]:
    """Fetch starred emails that have not yet been processed, oldest first.

    Connects via IMAP to Gmail, searches for FLAGGED (starred) messages,
    optionally excluding those with a given label and/or filtering by age.
    Uses Gmail's X-GM-LABELS IMAP extension to exclude the processed label
    at the server level.

    Args:
        gmail_sender: Gmail address to authenticate with.
        gmail_password: Gmail app password.
        max_age_days: If provided, only fetch emails received within this many
            days (SINCE date filter).
        processed_label: Gmail label to exclude (e.g. "Agent/Added-To-Trello").
            Uses Gmail IMAP extension NOT X-GM-LABELS to exclude at the server.

    Returns:
        List of EmailRecord objects sorted oldest first by email Date header.
    """
    imap = None
    try:
        imap = _imap_connect(gmail_sender, gmail_password)
        imap.select("INBOX")

        # Build IMAP search criteria (all criteria are implicitly ANDed)
        criteria_parts = ["FLAGGED"]
        if processed_label:
            criteria_parts.append(f'NOT X-GM-LABELS "{processed_label}"')
        if max_age_days is not None:
            since_date = (
                datetime.now(timezone.utc) - timedelta(days=max_age_days)
            ).strftime("%d-%b-%Y")
            criteria_parts.append(f"SINCE {since_date}")

        criteria = " ".join(criteria_parts)
        logger.info("IMAP search: %r", criteria)

        typ, data = imap.uid("SEARCH", None, criteria)
        if typ != "OK":
            logger.error("IMAP SEARCH failed: %s", data)
            return []

        uid_list: list[bytes] = data[0].split() if data[0] else []
        if not uid_list:
            logger.info("No starred emails found")
            return []

        logger.info("Found %d starred email(s) — fetching details", len(uid_list))

        raw: list[tuple[datetime, EmailRecord]] = []
        for uid in uid_list:
            try:
                typ, msg_data = imap.uid("FETCH", uid, "(RFC822 X-GM-MSGID)")
                if typ != "OK" or not msg_data or msg_data[0] is None:
                    logger.error("Failed to fetch UID %s — skipping", uid)
                    continue

                # msg_data[0] = (header_info_bytes, raw_email_bytes)
                header_info = msg_data[0][0].decode("utf-8", errors="replace")
                raw_email = msg_data[0][1]

                # Extract X-GM-MSGID from the FETCH response header line
                msgid_match = re.search(r"X-GM-MSGID\s+(\d+)", header_info)
                gmail_message_id = (
                    msgid_match.group(1) if msgid_match else uid.decode("ascii")
                )

                # Parse email headers and body
                msg = email_lib.message_from_bytes(raw_email)
                subject = str(msg.get("Subject", "") or "") or "(No Subject)"
                sender = str(msg.get("From", "") or "") or "(Unknown Sender)"
                date_str = str(msg.get("Date", "") or "")
                email_date = _parse_email_date(date_str)

                try:
                    body = _extract_body_from_message(msg)
                except Exception as exc:
                    logger.warning(
                        "Body extraction failed for %s: %s", gmail_message_id, exc
                    )
                    body = ""

                if not body:
                    logger.warning("No body extracted for %s", gmail_message_id)
                    body = "[Email body could not be extracted]"

                # Parse date for oldest-first sorting
                try:
                    sort_dt = (
                        email.utils.parsedate_to_datetime(date_str)
                        if date_str
                        else datetime.min.replace(tzinfo=timezone.utc)
                    )
                    if sort_dt.tzinfo is None:
                        sort_dt = sort_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    sort_dt = datetime.min.replace(tzinfo=timezone.utc)

                record = EmailRecord(
                    gmail_message_id=gmail_message_id,
                    subject=subject,
                    sender=sender,
                    email_date=email_date,
                    body=body,
                )
                raw.append((sort_dt, record))

            except Exception as exc:
                logger.error("Unexpected error fetching UID %s: %s", uid, exc)

        # Sort oldest first by email Date header
        raw.sort(key=lambda x: x[0])
        records = [r for _, r in raw]

        if len(records) < len(uid_list):
            logger.warning(
                "Fetched %d/%d messages successfully", len(records), len(uid_list)
            )
        return records

    finally:
        if imap:
            try:
                imap.logout()
            except Exception:
                pass


def apply_label(
    gmail_sender: str,
    gmail_password: str,
    message_id: str,
    label_name: str,
) -> None:
    """Apply a Gmail label to a message identified by X-GM-MSGID.

    Connects to [Gmail]/All Mail (so the message is findable regardless of
    which folder it is in), searches by X-GM-MSGID, and applies the label
    using Gmail's IMAP X-GM-LABELS extension.

    If the message cannot be found or the label store fails, logs an error
    and returns without raising so the pipeline can continue.

    Args:
        gmail_sender: Gmail address to authenticate with.
        gmail_password: Gmail app password.
        message_id: X-GM-MSGID value as a decimal string (set on EmailRecord
            by fetch_starred_emails).
        label_name: Gmail label name (e.g. "Agent/Added-To-Trello").
    """
    imap = None
    try:
        imap = _imap_connect(gmail_sender, gmail_password)
        imap.select('"[Gmail]/All Mail"')

        # Search by X-GM-MSGID (Gmail IMAP extension)
        typ, data = imap.uid("SEARCH", None, f"X-GM-MSGID {message_id}")
        if typ != "OK":
            logger.error(
                "IMAP search for X-GM-MSGID %s failed: %s — label not applied",
                message_id,
                data,
            )
            return

        uid_list: list[bytes] = data[0].split() if data[0] else []
        if not uid_list:
            logger.error(
                "Message X-GM-MSGID %s not found in All Mail — label not applied",
                message_id,
            )
            return

        uid = uid_list[0]
        typ, store_data = imap.uid("STORE", uid, "+X-GM-LABELS", f'"{label_name}"')
        if typ != "OK":
            logger.error(
                "Failed to apply label '%s' to X-GM-MSGID %s: %s",
                label_name,
                message_id,
                store_data,
            )
        else:
            logger.info("Applied label '%s' to message %s", label_name, message_id)

    except imaplib.IMAP4.error as exc:
        logger.error(
            "IMAP error applying label '%s' to %s: %s", label_name, message_id, exc
        )
    except Exception as exc:
        logger.error(
            "Unexpected error applying label '%s' to %s: %s",
            label_name,
            message_id,
            exc,
        )
    finally:
        if imap:
            try:
                imap.logout()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        from agent_shared.infra.config_loader import load_config

        gc, _ = load_config()
    except Exception as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    try:
        check_imap_auth(gc.gmail_sender, gc.gmail_password)
        logger.info("IMAP connection verified for %s", gc.gmail_sender)
    except Exception as exc:
        logger.error("IMAP connection failed: %s", exc)
        sys.exit(1)

    emails = fetch_starred_emails(gc.gmail_sender, gc.gmail_password)
    if not emails:
        print("No starred emails found.")
    else:
        print(f"Found {len(emails)} starred email(s):")
        for em in emails:
            print(f"  [{em.email_date}] {em.subject!r} — {em.sender}")
