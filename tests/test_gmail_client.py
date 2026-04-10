"""Tests for src/gmail_client.py.

All IMAP calls are mocked — no live network requests are made.
IMAP4_SSL is patched to return a MagicMock instance configured to
return realistic IMAP response structures.
"""

import email as email_lib
import email.mime.multipart
import email.mime.text
import imaplib
import socket
from unittest.mock import MagicMock, call, patch

import pytest

from src.gmail_client import (
    _extract_body_from_message,
    _parse_email_date,
    _strip_html,
    apply_label,
    check_imap_auth,
    fetch_starred_emails,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GMAIL_SENDER = "test@gmail.com"
GMAIL_PASSWORD = "app-password"


def make_plain_email(
    subject: str = "Test Subject",
    sender: str = "Alice <alice@example.com>",
    date: str = "Sat, 08 Mar 2026 10:00:00 +0000",
    body: str = "This is a plain text email body.",
    x_gm_msgid: str = "98765432100",
) -> tuple[bytes, bytes]:
    """Build (header_info_bytes, raw_rfc822_bytes) for a plain-text email."""
    msg = email_lib.mime.text.MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Date"] = date
    raw = msg.as_bytes()
    header_info = f'1 (X-GM-MSGID {x_gm_msgid} RFC822 {{{len(raw)}}})'.encode()
    return header_info, raw


def make_html_email(
    subject: str = "HTML Email",
    sender: str = "sender@example.com",
    date: str = "Sat, 08 Mar 2026 10:00:00 +0000",
    html_body: str = "<html><body><p>HTML content</p></body></html>",
    x_gm_msgid: str = "11111111111",
) -> tuple[bytes, bytes]:
    """Build (header_info_bytes, raw_rfc822_bytes) for an HTML-only email."""
    msg = email_lib.mime.text.MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Date"] = date
    raw = msg.as_bytes()
    header_info = f'1 (X-GM-MSGID {x_gm_msgid} RFC822 {{{len(raw)}}})'.encode()
    return header_info, raw


def make_multipart_email(
    subject: str = "Multipart Email",
    sender: str = "sender@example.com",
    date: str = "Sat, 08 Mar 2026 10:00:00 +0000",
    plain_body: str = "Plain text version.",
    html_body: str = "<p>HTML version.</p>",
    x_gm_msgid: str = "22222222222",
) -> tuple[bytes, bytes]:
    """Build (header_info_bytes, raw_rfc822_bytes) for a multipart/alternative email."""
    msg = email_lib.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Date"] = date
    msg.attach(email_lib.mime.text.MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(email_lib.mime.text.MIMEText(html_body, "html", "utf-8"))
    raw = msg.as_bytes()
    header_info = f'1 (X-GM-MSGID {x_gm_msgid} RFC822 {{{len(raw)}}})'.encode()
    return header_info, raw


def make_imap_mock(
    search_response: tuple = ("OK", [b""]),
    fetch_responses: list[tuple] | None = None,
    store_response: tuple = ("OK", [b""]),
    login_raises: Exception | None = None,
    connect_raises: Exception | None = None,
) -> MagicMock:
    """Build a MagicMock imaplib.IMAP4_SSL instance.

    Args:
        search_response: Return value of imap.uid('SEARCH', ...).
        fetch_responses: Sequential return values of imap.uid('FETCH', ...).
        store_response: Return value of imap.uid('STORE', ...).
        login_raises: If set, imap.login() raises this exception.
        connect_raises: If set, the IMAP4_SSL constructor raises this exception.
    """
    mock_imap = MagicMock()

    if login_raises is not None:
        mock_imap.login.side_effect = login_raises

    def uid_side_effect(command, *args):
        if command == "SEARCH":
            return search_response
        elif command == "FETCH":
            if fetch_responses:
                response = fetch_responses.pop(0)
                return response
            return ("OK", [None])
        elif command == "STORE":
            return store_response
        return ("NO", [b"unknown command"])

    mock_imap.uid.side_effect = uid_side_effect
    mock_imap.select.return_value = ("OK", [b"INBOX"])
    mock_imap.logout.return_value = ("BYE", [b"Logged out"])

    return mock_imap


# ---------------------------------------------------------------------------
# _strip_html unit tests
# ---------------------------------------------------------------------------


def test_strip_html_removes_tags() -> None:
    html = "<html><body><p>Hello world</p></body></html>"
    assert "Hello world" in _strip_html(html)
    assert "<" not in _strip_html(html)


def test_strip_html_preserves_text_content() -> None:
    html = "<p>First paragraph</p><p>Second paragraph</p>"
    result = _strip_html(html)
    assert "First paragraph" in result
    assert "Second paragraph" in result


def test_strip_html_plain_text_unchanged() -> None:
    plain = "No HTML here"
    assert _strip_html(plain) == plain


def test_strip_html_removes_style_block_contents() -> None:
    html = (
        "<html><head>"
        "<style>body { width: 100% !important; -webkit-text-size-adjust: 100%; }</style>"
        "</head><body><p>Readable content here.</p></body></html>"
    )
    result = _strip_html(html)
    assert "Readable content here." in result
    assert "width" not in result
    assert "-webkit-text-size-adjust" not in result
    assert "100%" not in result


def test_strip_html_removes_script_block_contents() -> None:
    html = (
        "<html><body>"
        "<script>function track() { window.analytics.send('open'); }</script>"
        "<p>Click here to unsubscribe.</p>"
        "</body></html>"
    )
    result = _strip_html(html)
    assert "Click here to unsubscribe." in result
    assert "window.analytics" not in result
    assert "function track" not in result


def test_strip_html_removes_head_block_contents() -> None:
    html = (
        "<html><head>"
        "<title>Email title</title>"
        "<meta charset='utf-8'>"
        "</head><body><p>Body text.</p></body></html>"
    )
    result = _strip_html(html)
    assert "Body text." in result
    assert "Email title" not in result


def test_strip_html_removes_html_comments() -> None:
    html = (
        "<p>Visible text.</p>"
        "<!-- This is a hidden comment with internal notes -->"
        "<p>More visible text.</p>"
    )
    result = _strip_html(html)
    assert "Visible text." in result
    assert "More visible text." in result
    assert "hidden comment" not in result
    assert "internal notes" not in result


def test_strip_html_decodes_html_entities() -> None:
    html = "<p>Tom &amp; Jerry &mdash; &lt;heroes&gt; with &nbsp;spaces</p>"
    result = _strip_html(html)
    assert "&amp;" not in result
    assert "&mdash;" not in result
    assert "&lt;" not in result
    assert "&gt;" not in result
    assert "Tom & Jerry" in result
    assert "<heroes>" in result


def test_strip_html_collapses_excessive_blank_lines() -> None:
    html = "<p>First paragraph</p>\n\n\n\n\n<p>Second paragraph</p>"
    result = _strip_html(html)
    assert "\n\n\n\n" not in result
    assert "First paragraph" in result
    assert "Second paragraph" in result


def test_strip_html_realistic_marketing_email() -> None:
    html = """<!DOCTYPE html>
<html>
<head>
  <title>Your Invoice</title>
  <style type="text/css">
    body { font-family: Arial, sans-serif; }
    .container { width: 600px; margin: 0 auto; }
    @media only screen and (max-width: 600px) {
      .container { width: 100% !important; }
    }
  </style>
</head>
<body>
  <!-- Tracking pixel comment: do not remove -->
  <div class="container">
    <h1>Invoice #1234</h1>
    <p>Dear Customer,</p>
    <p>Please find attached your invoice for &pound;49.99.</p>
    <p>Thank you &amp; have a great day!</p>
  </div>
  <script type="text/javascript">
    document.addEventListener('load', function() { beacon.fire('email_open'); });
  </script>
</body>
</html>"""
    result = _strip_html(html)
    assert "Invoice #1234" in result
    assert "Dear Customer" in result
    assert "£49.99" in result
    assert "Thank you & have a great day!" in result
    assert "font-family" not in result
    assert "max-width" not in result
    assert "!important" not in result
    assert "Tracking pixel" not in result
    assert "beacon.fire" not in result
    assert "addEventListener" not in result
    assert "<style" not in result
    assert "<script" not in result
    assert "<div" not in result


# ---------------------------------------------------------------------------
# _extract_body_from_message unit tests
# ---------------------------------------------------------------------------


def test_extract_body_plain_text_message() -> None:
    msg = email_lib.message_from_bytes(
        email_lib.mime.text.MIMEText("Plain text content here.", "plain", "utf-8").as_bytes()
    )
    result = _extract_body_from_message(msg)
    assert result == "Plain text content here."


def test_extract_body_html_only_strips_tags() -> None:
    html_content = "Important message content"
    msg = email_lib.message_from_bytes(
        email_lib.mime.text.MIMEText(
            f"<p>{html_content}</p>", "html", "utf-8"
        ).as_bytes()
    )
    result = _extract_body_from_message(msg)
    assert html_content in result
    assert "<p>" not in result


def test_extract_body_multipart_prefers_plain() -> None:
    plain = "This is the plain text version."
    html = "<p>This is the HTML version.</p>"
    msg_mp = email_lib.mime.multipart.MIMEMultipart("alternative")
    msg_mp.attach(email_lib.mime.text.MIMEText(plain, "plain", "utf-8"))
    msg_mp.attach(email_lib.mime.text.MIMEText(html, "html", "utf-8"))
    msg = email_lib.message_from_bytes(msg_mp.as_bytes())
    assert _extract_body_from_message(msg) == plain


def test_extract_body_multipart_falls_back_to_html_when_no_plain() -> None:
    html_text = "HTML only content"
    msg_mp = email_lib.mime.multipart.MIMEMultipart("alternative")
    msg_mp.attach(
        email_lib.mime.text.MIMEText(f"<p>{html_text}</p>", "html", "utf-8")
    )
    msg = email_lib.message_from_bytes(msg_mp.as_bytes())
    result = _extract_body_from_message(msg)
    assert html_text in result


def test_extract_body_empty_message_returns_empty() -> None:
    msg = email_lib.message_from_string("")
    result = _extract_body_from_message(msg)
    assert result == ""


# ---------------------------------------------------------------------------
# _parse_email_date unit tests
# ---------------------------------------------------------------------------


def test_parse_email_date_valid_rfc2822() -> None:
    result = _parse_email_date("Mon, 01 Jan 2024 12:00:00 +0000")
    assert "2024-01-01" in result


def test_parse_email_date_empty_returns_empty() -> None:
    assert _parse_email_date("") == ""


def test_parse_email_date_invalid_returns_empty() -> None:
    assert _parse_email_date("not a date") == ""


# ---------------------------------------------------------------------------
# check_imap_auth
# ---------------------------------------------------------------------------


def test_check_imap_auth_success_does_not_raise() -> None:
    mock_imap = make_imap_mock()
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        check_imap_auth(GMAIL_SENDER, GMAIL_PASSWORD)
    mock_imap.login.assert_called_once_with(GMAIL_SENDER, GMAIL_PASSWORD)
    mock_imap.logout.assert_called_once()


def test_check_imap_auth_login_failure_raises() -> None:
    mock_imap = make_imap_mock(
        login_raises=imaplib.IMAP4.error("Invalid credentials")
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        with pytest.raises(imaplib.IMAP4.error):
            check_imap_auth(GMAIL_SENDER, GMAIL_PASSWORD)


def test_check_imap_auth_connection_timeout_raises() -> None:
    with patch("imaplib.IMAP4_SSL", side_effect=socket.timeout("timed out")):
        with pytest.raises(socket.timeout):
            check_imap_auth(GMAIL_SENDER, GMAIL_PASSWORD)


def test_check_imap_auth_connection_refused_raises() -> None:
    with patch("imaplib.IMAP4_SSL", side_effect=ConnectionRefusedError("refused")):
        with pytest.raises(ConnectionRefusedError):
            check_imap_auth(GMAIL_SENDER, GMAIL_PASSWORD)


def test_check_imap_auth_logout_not_called_when_login_fails() -> None:
    """logout() should NOT be called when login() raises.

    _imap_connect() raises before returning, so `imap` in check_imap_auth
    remains None and the finally block skips logout.
    """
    mock_imap = make_imap_mock(
        login_raises=imaplib.IMAP4.error("bad credentials")
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        with pytest.raises(imaplib.IMAP4.error):
            check_imap_auth(GMAIL_SENDER, GMAIL_PASSWORD)
    mock_imap.logout.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_starred_emails
# ---------------------------------------------------------------------------


def test_fetch_starred_emails_plain_text_message() -> None:
    header_info, raw_email = make_plain_email(
        x_gm_msgid="98765432100",
        body="This is a plain text email body.",
        subject="Test Subject",
        sender="Alice <alice@example.com>",
    )
    mock_imap = make_imap_mock(
        search_response=("OK", [b"1"]),
        fetch_responses=[("OK", [(header_info, raw_email), b")"])],
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        results = fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD)

    assert len(results) == 1
    assert results[0].gmail_message_id == "98765432100"
    assert results[0].subject == "Test Subject"
    assert results[0].sender == "Alice <alice@example.com>"
    assert results[0].body == "This is a plain text email body."


def test_fetch_starred_emails_extracts_sender_and_subject() -> None:
    header_info, raw_email = make_plain_email(
        subject="Re: Q3 Board Deck - Final Review",
        sender="Bob Smith <bob@example.com>",
        x_gm_msgid="11111",
    )
    mock_imap = make_imap_mock(
        search_response=("OK", [b"1"]),
        fetch_responses=[("OK", [(header_info, raw_email), b")"])],
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        results = fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD)

    assert results[0].subject == "Re: Q3 Board Deck - Final Review"
    assert results[0].sender == "Bob Smith <bob@example.com>"


def test_fetch_starred_emails_html_only_strips_tags() -> None:
    html_content = "Important email content"
    header_info, raw_email = make_html_email(
        html_body=f"<html><body><p>{html_content}</p></body></html>",
        x_gm_msgid="22222",
    )
    mock_imap = make_imap_mock(
        search_response=("OK", [b"1"]),
        fetch_responses=[("OK", [(header_info, raw_email), b")"])],
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        results = fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD)

    assert html_content in results[0].body
    assert "<" not in results[0].body


def test_fetch_starred_emails_multipart_prefers_plain() -> None:
    plain = "Plain text version of the email."
    header_info, raw_email = make_multipart_email(
        plain_body=plain,
        html_body="<p>HTML version</p>",
        x_gm_msgid="33333",
    )
    mock_imap = make_imap_mock(
        search_response=("OK", [b"1"]),
        fetch_responses=[("OK", [(header_info, raw_email), b")"])],
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        results = fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD)

    assert results[0].body == plain


def test_fetch_starred_emails_empty_returns_empty_list() -> None:
    mock_imap = make_imap_mock(search_response=("OK", [b""]))
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        results = fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD)
    assert results == []


def test_fetch_starred_emails_sorted_oldest_first() -> None:
    """Emails are sorted by Date header; oldest should be first."""
    header_old, raw_old = make_plain_email(
        subject="Old Email",
        date="Mon, 01 Jan 2024 10:00:00 +0000",
        x_gm_msgid="111",
    )
    header_new, raw_new = make_plain_email(
        subject="New Email",
        date="Sat, 08 Mar 2026 10:00:00 +0000",
        x_gm_msgid="222",
    )
    mock_imap = make_imap_mock(
        search_response=("OK", [b"2 1"]),  # newest UID first (IMAP order)
        fetch_responses=[
            ("OK", [(header_new, raw_new), b")"]),  # UID 2 fetched first
            ("OK", [(header_old, raw_old), b")"]),  # UID 1 fetched second
        ],
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        results = fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD)

    assert len(results) == 2
    assert results[0].subject == "Old Email"
    assert results[1].subject == "New Email"


def test_fetch_starred_emails_with_max_age_adds_since_to_search() -> None:
    """max_age_days should add SINCE to the IMAP search criteria."""
    mock_imap = make_imap_mock(search_response=("OK", [b""]))
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD, max_age_days=7)

    # Find the SEARCH call and verify SINCE is present
    search_calls = [
        c for c in mock_imap.uid.call_args_list if c[0][0] == "SEARCH"
    ]
    assert len(search_calls) == 1
    criteria_arg = search_calls[0][0][2]  # third positional arg
    assert "SINCE" in criteria_arg


def test_fetch_starred_emails_without_max_age_no_since() -> None:
    mock_imap = make_imap_mock(search_response=("OK", [b""]))
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD, max_age_days=None)

    search_calls = [
        c for c in mock_imap.uid.call_args_list if c[0][0] == "SEARCH"
    ]
    criteria_arg = search_calls[0][0][2]
    assert "SINCE" not in criteria_arg


def test_fetch_starred_emails_with_processed_label_excludes_it() -> None:
    """processed_label adds NOT X-GM-LABELS to the IMAP search."""
    mock_imap = make_imap_mock(search_response=("OK", [b""]))
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        fetch_starred_emails(
            GMAIL_SENDER, GMAIL_PASSWORD, processed_label="Agent/Added-To-Trello"
        )

    search_calls = [
        c for c in mock_imap.uid.call_args_list if c[0][0] == "SEARCH"
    ]
    criteria_arg = search_calls[0][0][2]
    assert "NOT X-GM-LABELS" in criteria_arg
    assert "Agent/Added-To-Trello" in criteria_arg


def test_fetch_starred_emails_without_processed_label_no_exclusion() -> None:
    mock_imap = make_imap_mock(search_response=("OK", [b""]))
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD, processed_label=None)

    search_calls = [
        c for c in mock_imap.uid.call_args_list if c[0][0] == "SEARCH"
    ]
    criteria_arg = search_calls[0][0][2]
    assert "X-GM-LABELS" not in criteria_arg


def test_fetch_starred_emails_search_always_starts_with_flagged() -> None:
    mock_imap = make_imap_mock(search_response=("OK", [b""]))
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        fetch_starred_emails(
            GMAIL_SENDER,
            GMAIL_PASSWORD,
            max_age_days=7,
            processed_label="Agent/Added-To-Trello",
        )

    search_calls = [
        c for c in mock_imap.uid.call_args_list if c[0][0] == "SEARCH"
    ]
    criteria_arg = search_calls[0][0][2]
    assert criteria_arg.startswith("FLAGGED")


def test_fetch_starred_emails_missing_body_uses_placeholder() -> None:
    """A message with no extractable body should use the placeholder text."""
    # Create an empty message (no body parts)
    import email.mime.base
    msg_empty = email_lib.mime.multipart.MIMEMultipart("mixed")
    msg_empty["Subject"] = "No Body"
    msg_empty["From"] = "sender@example.com"
    msg_empty["Date"] = "Sat, 08 Mar 2026 10:00:00 +0000"
    # Attach only an application/pdf (no text)
    part = email_lib.mime.base.MIMEBase("application", "pdf")
    part.set_payload(b"fake pdf")
    msg_empty.attach(part)
    raw = msg_empty.as_bytes()
    header_info = b'1 (X-GM-MSGID 99999 RFC822 {100})'

    mock_imap = make_imap_mock(
        search_response=("OK", [b"1"]),
        fetch_responses=[("OK", [(header_info, raw), b")"])],
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        results = fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD)

    assert "[Email body could not be extracted]" in results[0].body


def test_fetch_starred_emails_skips_failed_fetch() -> None:
    """A failed FETCH for one UID is logged and skipped; others continue."""
    header_ok, raw_ok = make_plain_email(subject="Good Email", x_gm_msgid="555")
    mock_imap = make_imap_mock(
        search_response=("OK", [b"1 2"]),
        fetch_responses=[
            ("NO", [None]),  # UID 1 fails
            ("OK", [(header_ok, raw_ok), b")"]),  # UID 2 succeeds
        ],
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        results = fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD)

    assert len(results) == 1
    assert results[0].subject == "Good Email"


def test_fetch_starred_emails_logout_called_on_success() -> None:
    mock_imap = make_imap_mock(search_response=("OK", [b""]))
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD)
    mock_imap.logout.assert_called_once()


def test_fetch_starred_emails_logout_called_on_search_error() -> None:
    """logout() must be called even when SEARCH returns an error."""
    mock_imap = make_imap_mock(search_response=("NO", [b"search failed"]))
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        result = fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD)
    assert result == []
    mock_imap.logout.assert_called_once()


def test_fetch_starred_emails_msgid_falls_back_to_uid_when_missing() -> None:
    """If X-GM-MSGID is not in the FETCH response, fall back to UID."""
    header_info = b'1 (RFC822 {100})'  # no X-GM-MSGID
    header_info2, raw = make_plain_email(subject="No MSGID")
    mock_imap = make_imap_mock(
        search_response=("OK", [b"42"]),
        fetch_responses=[("OK", [(header_info, raw), b")"])],
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        results = fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD)

    assert len(results) == 1
    assert results[0].gmail_message_id == "42"  # fell back to UID


def test_fetch_starred_emails_date_parsed_from_header() -> None:
    header_info, raw_email = make_plain_email(
        date="Mon, 01 Jan 2024 12:00:00 +0000", x_gm_msgid="12345"
    )
    mock_imap = make_imap_mock(
        search_response=("OK", [b"1"]),
        fetch_responses=[("OK", [(header_info, raw_email), b")"])],
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        results = fetch_starred_emails(GMAIL_SENDER, GMAIL_PASSWORD)

    assert "2024-01-01" in results[0].email_date


# ---------------------------------------------------------------------------
# apply_label
# ---------------------------------------------------------------------------


def test_apply_label_success() -> None:
    mock_imap = make_imap_mock(
        search_response=("OK", [b"12345"]),
        store_response=("OK", [b""]),
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        apply_label(GMAIL_SENDER, GMAIL_PASSWORD, "98765432100", "Agent/Added-To-Trello")

    # Verify select was called with All Mail
    mock_imap.select.assert_called_once_with('"[Gmail]/All Mail"')

    # Verify STORE was called with +X-GM-LABELS
    store_calls = [
        c for c in mock_imap.uid.call_args_list if c[0][0] == "STORE"
    ]
    assert len(store_calls) == 1
    assert store_calls[0][0][2] == "+X-GM-LABELS"
    assert "Agent/Added-To-Trello" in store_calls[0][0][3]


def test_apply_label_searches_by_x_gm_msgid() -> None:
    mock_imap = make_imap_mock(
        search_response=("OK", [b"12345"]),
        store_response=("OK", [b""]),
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        apply_label(GMAIL_SENDER, GMAIL_PASSWORD, "98765432100", "Agent/Added-To-Trello")

    search_calls = [
        c for c in mock_imap.uid.call_args_list if c[0][0] == "SEARCH"
    ]
    assert len(search_calls) == 1
    # Criteria should include X-GM-MSGID and the message ID
    criteria = search_calls[0][0][2]
    assert "X-GM-MSGID" in criteria
    assert "98765432100" in criteria


def test_apply_label_message_not_found_does_not_raise() -> None:
    """If X-GM-MSGID search returns no UIDs, log and return gracefully."""
    mock_imap = make_imap_mock(search_response=("OK", [b""]))
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        apply_label(GMAIL_SENDER, GMAIL_PASSWORD, "98765432100", "Agent/Added-To-Trello")

    # STORE should NOT have been called
    store_calls = [
        c for c in mock_imap.uid.call_args_list if c[0][0] == "STORE"
    ]
    assert len(store_calls) == 0


def test_apply_label_search_failure_does_not_raise() -> None:
    """If the SEARCH fails, apply_label logs and returns without raising."""
    mock_imap = make_imap_mock(search_response=("NO", [b"search failed"]))
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        apply_label(GMAIL_SENDER, GMAIL_PASSWORD, "98765432100", "Agent/Added-To-Trello")

    store_calls = [
        c for c in mock_imap.uid.call_args_list if c[0][0] == "STORE"
    ]
    assert len(store_calls) == 0


def test_apply_label_imap_error_does_not_raise() -> None:
    """imaplib.IMAP4.error during label application is caught and logged."""
    mock_imap = MagicMock()
    mock_imap.login.return_value = ("OK", [b"logged in"])
    mock_imap.select.return_value = ("OK", [b"[Gmail]/All Mail"])
    mock_imap.uid.side_effect = imaplib.IMAP4.error("unexpected error")

    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        apply_label(GMAIL_SENDER, GMAIL_PASSWORD, "98765432100", "Agent/Added-To-Trello")

    # Should not raise — error is caught and logged


def test_apply_label_logout_called_on_success() -> None:
    mock_imap = make_imap_mock(
        search_response=("OK", [b"12345"]),
        store_response=("OK", [b""]),
    )
    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        apply_label(GMAIL_SENDER, GMAIL_PASSWORD, "98765432100", "Agent/Added-To-Trello")
    mock_imap.logout.assert_called_once()


def test_apply_label_logout_called_on_failure() -> None:
    """logout() must be called even when an exception is caught."""
    mock_imap = MagicMock()
    mock_imap.login.return_value = ("OK", [b"logged in"])
    mock_imap.select.return_value = ("OK", [b"[Gmail]/All Mail"])
    mock_imap.uid.side_effect = imaplib.IMAP4.error("server error")

    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        apply_label(GMAIL_SENDER, GMAIL_PASSWORD, "98765432100", "Agent/Added-To-Trello")

    mock_imap.logout.assert_called_once()
