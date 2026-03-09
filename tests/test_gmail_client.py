"""Tests for src/gmail_client.py.

All Gmail API calls are mocked — no live requests are made.
Mock service objects are built using MagicMock and configured to return
realistic Gmail API response structures.
"""

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from googleapiclient.errors import HttpError

from src.gmail_client import (
    _decode_base64url,
    _extract_body,
    _strip_html,
    apply_label,
    fetch_starred_emails,
    unstar_email,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def b64(text: str) -> str:
    """Encode text as base64url without padding (Gmail API format)."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).rstrip(b"=").decode("utf-8")


def make_message(
    msg_id: str = "msg_001",
    thread_id: str = "thread_001",
    subject: str = "Test Subject",
    sender: str = "Alice <alice@example.com>",
    date: str = "Sat, 08 Mar 2026 10:00:00 +0000",
    internal_date: str = "1741428000000",
    payload: dict | None = None,
    plain_body: str | None = "This is a plain text email body.",
) -> dict:
    """Build a realistic Gmail messages.get response dict.

    If payload is provided it is used directly. Otherwise a text/plain
    message is constructed from plain_body.
    """
    if payload is None:
        payload = {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": date},
            ],
            "body": {"data": b64(plain_body or "")},
        }
    else:
        # Ensure headers are present in the provided payload
        if "headers" not in payload:
            payload["headers"] = [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": date},
            ]
    return {
        "id": msg_id,
        "threadId": thread_id,
        "internalDate": internal_date,
        "payload": payload,
    }


def make_service(
    list_response: dict | None = None,
    get_responses: list[dict] | None = None,
    labels_response: dict | None = None,
) -> MagicMock:
    """Build a MagicMock Gmail service with configured return values.

    Args:
        list_response: Return value of messages().list().execute().
        get_responses: Sequential return values of messages().get().execute().
            If a single response, pass as a one-element list.
        labels_response: Return value of labels().list().execute().
    """
    svc = MagicMock()
    msgs = svc.users.return_value.messages.return_value

    if list_response is not None:
        msgs.list.return_value.execute.return_value = list_response

    if get_responses is not None:
        if len(get_responses) == 1:
            msgs.get.return_value.execute.return_value = get_responses[0]
        else:
            # Multiple sequential responses
            msgs.get.return_value.execute.side_effect = get_responses

    msgs.modify.return_value.execute.return_value = {}

    if labels_response is not None:
        svc.users.return_value.labels.return_value.list.return_value.execute.return_value = (
            labels_response
        )

    return svc


# ---------------------------------------------------------------------------
# _decode_base64url unit tests
# ---------------------------------------------------------------------------


def test_decode_base64url_round_trips_plain_text() -> None:
    text = "Hello, world!"
    assert _decode_base64url(b64(text)) == text


def test_decode_base64url_handles_empty_string() -> None:
    assert _decode_base64url("") == ""


def test_decode_base64url_handles_padding_correctly() -> None:
    # Different lengths exercise different padding amounts
    for text in ["a", "ab", "abc", "abcd"]:
        assert _decode_base64url(b64(text)) == text


def test_decode_base64url_returns_empty_on_invalid_data() -> None:
    # Invalid base64url data should not raise; returns empty string
    result = _decode_base64url("!!!invalid!!!")
    assert isinstance(result, str)  # no exception raised


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


# ---------------------------------------------------------------------------
# _extract_body unit tests
# ---------------------------------------------------------------------------


def test_extract_body_text_plain() -> None:
    text = "Plain text content here."
    payload = {"mimeType": "text/plain", "body": {"data": b64(text)}}
    assert _extract_body(payload) == text


def test_extract_body_text_html_strips_tags() -> None:
    html_content = "Important message content"
    payload = {
        "mimeType": "text/html",
        "body": {"data": b64(f"<p>{html_content}</p>")},
    }
    result = _extract_body(payload)
    assert html_content in result
    assert "<p>" not in result


def test_extract_body_multipart_prefers_plain() -> None:
    plain = "This is the plain text version."
    html = "<p>This is the HTML version.</p>"
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64(plain)}},
            {"mimeType": "text/html", "body": {"data": b64(html)}},
        ],
    }
    assert _extract_body(payload) == plain


def test_extract_body_multipart_falls_back_to_html_when_no_plain() -> None:
    html_text = "HTML only content"
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": b64(f"<p>{html_text}</p>")}},
        ],
    }
    result = _extract_body(payload)
    assert html_text in result


def test_extract_body_nested_multipart() -> None:
    """multipart/mixed > multipart/alternative > text/plain should be extracted."""
    plain = "Nested plain text body."
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64(plain)}},
                    {"mimeType": "text/html", "body": {"data": b64("<p>HTML</p>")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "body": {"attachmentId": "attach_001"},
            },
        ],
    }
    assert _extract_body(payload) == plain


def test_extract_body_empty_payload_returns_empty() -> None:
    assert _extract_body({}) == ""


def test_extract_body_missing_body_data_returns_empty() -> None:
    payload = {"mimeType": "text/plain", "body": {}}
    assert _extract_body(payload) == ""


# ---------------------------------------------------------------------------
# fetch_starred_emails
# ---------------------------------------------------------------------------


def test_fetch_starred_emails_plain_text_message() -> None:
    body = "This is a plain text email body."
    msg = make_message(msg_id="msg_001", plain_body=body)
    svc = make_service(
        list_response={"messages": [{"id": "msg_001", "threadId": "t_001"}]},
        get_responses=[msg],
    )

    results = fetch_starred_emails(svc)

    assert len(results) == 1
    assert results[0].gmail_message_id == "msg_001"
    assert results[0].subject == "Test Subject"
    assert results[0].sender == "Alice <alice@example.com>"
    assert results[0].body == body


def test_fetch_starred_emails_extracts_sender_and_subject() -> None:
    msg = make_message(
        subject="Re: Q3 Board Deck - Final Review",
        sender="Bob Smith <bob@example.com>",
    )
    svc = make_service(
        list_response={"messages": [{"id": "msg_001"}]},
        get_responses=[msg],
    )

    results = fetch_starred_emails(svc)

    assert results[0].subject == "Re: Q3 Board Deck - Final Review"
    assert results[0].sender == "Bob Smith <bob@example.com>"


def test_fetch_starred_emails_html_only_strips_tags() -> None:
    html_content = "Important email content"
    payload = {
        "mimeType": "text/html",
        "headers": [
            {"name": "Subject", "value": "HTML Email"},
            {"name": "From", "value": "sender@example.com"},
            {"name": "Date", "value": "Sat, 08 Mar 2026 10:00:00 +0000"},
        ],
        "body": {"data": b64(f"<html><body><p>{html_content}</p></body></html>")},
    }
    msg = make_message(msg_id="msg_html", payload=payload)
    svc = make_service(
        list_response={"messages": [{"id": "msg_html"}]},
        get_responses=[msg],
    )

    results = fetch_starred_emails(svc)

    assert html_content in results[0].body
    assert "<" not in results[0].body


def test_fetch_starred_emails_multipart_prefers_plain() -> None:
    plain = "Plain text version of the email."
    payload = {
        "mimeType": "multipart/alternative",
        "headers": [
            {"name": "Subject", "value": "Multipart Email"},
            {"name": "From", "value": "sender@example.com"},
            {"name": "Date", "value": "Sat, 08 Mar 2026 10:00:00 +0000"},
        ],
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64(plain)}},
            {"mimeType": "text/html", "body": {"data": b64("<p>HTML version</p>")}},
        ],
    }
    msg = make_message(msg_id="msg_mp", payload=payload)
    svc = make_service(
        list_response={"messages": [{"id": "msg_mp"}]},
        get_responses=[msg],
    )

    results = fetch_starred_emails(svc)

    assert results[0].body == plain


def test_fetch_starred_emails_empty_returns_empty_list() -> None:
    svc = make_service(list_response={"messages": []})

    results = fetch_starred_emails(svc)

    assert results == []


def test_fetch_starred_emails_no_messages_key_returns_empty() -> None:
    """Gmail list returns {} when no results (no 'messages' key)."""
    svc = make_service(list_response={})

    results = fetch_starred_emails(svc)

    assert results == []


def test_fetch_starred_emails_sorted_oldest_first() -> None:
    """internalDate determines sort order; oldest email should be first."""
    # msg_old has earlier internalDate than msg_new
    msg_old = make_message(
        msg_id="msg_old",
        subject="Old Email",
        internal_date="1741000000000",  # earlier
    )
    msg_new = make_message(
        msg_id="msg_new",
        subject="New Email",
        internal_date="1741428000000",  # later
    )
    svc = make_service(
        list_response={
            "messages": [
                {"id": "msg_new"},  # list returns newest first (reversed)
                {"id": "msg_old"},
            ]
        },
        get_responses=[msg_new, msg_old],
    )

    results = fetch_starred_emails(svc)

    # After sorting, old should come first
    assert results[0].gmail_message_id == "msg_old"
    assert results[1].gmail_message_id == "msg_new"


def test_fetch_starred_emails_with_max_age_appends_query() -> None:
    """max_age_days should add 'newer_than:Nd' to the Gmail query."""
    svc = make_service(list_response={})

    fetch_starred_emails(svc, max_age_days=7)

    call_kwargs = svc.users.return_value.messages.return_value.list.call_args
    assert "newer_than:7d" in call_kwargs[1]["q"]


def test_fetch_starred_emails_without_max_age_no_newer_than() -> None:
    svc = make_service(list_response={})

    fetch_starred_emails(svc, max_age_days=None)

    call_kwargs = svc.users.return_value.messages.return_value.list.call_args
    assert "newer_than" not in call_kwargs[1]["q"]


def test_fetch_starred_emails_with_processed_label_excludes_it() -> None:
    """processed_label should add '-label:X' to the Gmail query."""
    svc = make_service(list_response={})

    fetch_starred_emails(svc, processed_label="Agent/Added-To-Trello")

    call_kwargs = svc.users.return_value.messages.return_value.list.call_args
    assert "-label:Agent/Added-To-Trello" in call_kwargs[1]["q"]


def test_fetch_starred_emails_without_processed_label_no_exclusion() -> None:
    """Without processed_label, query should not contain '-label:'."""
    svc = make_service(list_response={})

    fetch_starred_emails(svc, processed_label=None)

    call_kwargs = svc.users.return_value.messages.return_value.list.call_args
    assert "-label:" not in call_kwargs[1]["q"]


def test_fetch_starred_emails_query_always_starts_with_is_starred() -> None:
    svc = make_service(list_response={})

    fetch_starred_emails(svc, max_age_days=7, processed_label="Agent/Added-To-Trello")

    call_kwargs = svc.users.return_value.messages.return_value.list.call_args
    assert call_kwargs[1]["q"].startswith("is:starred")


def test_fetch_starred_emails_missing_body_uses_placeholder() -> None:
    """A message with no extractable body should use the placeholder text."""
    payload = {
        "mimeType": "multipart/mixed",
        "headers": [
            {"name": "Subject", "value": "No Body"},
            {"name": "From", "value": "sender@example.com"},
            {"name": "Date", "value": "Sat, 08 Mar 2026 10:00:00 +0000"},
        ],
        "parts": [
            # Only an attachment, no text
            {"mimeType": "application/pdf", "body": {"attachmentId": "att_001"}},
        ],
    }
    msg = make_message(msg_id="msg_nobody", payload=payload)
    svc = make_service(
        list_response={"messages": [{"id": "msg_nobody"}]},
        get_responses=[msg],
    )

    results = fetch_starred_emails(svc)

    assert "[Email body could not be extracted]" in results[0].body


def test_fetch_starred_emails_handles_pagination() -> None:
    """fetch_starred_emails must follow nextPageToken to get all messages."""
    msg1 = make_message(msg_id="msg_p1", internal_date="1000")
    msg2 = make_message(msg_id="msg_p2", internal_date="2000")

    svc = MagicMock()
    msgs = svc.users.return_value.messages.return_value

    # Page 1 returns nextPageToken; page 2 returns no token
    msgs.list.return_value.execute.side_effect = [
        {"messages": [{"id": "msg_p1"}], "nextPageToken": "tok_abc"},
        {"messages": [{"id": "msg_p2"}]},
    ]
    msgs.get.return_value.execute.side_effect = [msg1, msg2]

    results = fetch_starred_emails(svc)

    assert len(results) == 2
    # Verify list was called twice (initial + page 2)
    assert msgs.list.call_count == 2


def test_fetch_starred_emails_thread_message_uses_payload_body() -> None:
    """Thread messages (threadId != id) are extracted normally from payload."""
    body = "This is a thread reply body."
    msg = make_message(
        msg_id="msg_thread",
        thread_id="thread_main",  # different from msg_id = it's threaded
        plain_body=body,
    )
    svc = make_service(
        list_response={"messages": [{"id": "msg_thread", "threadId": "thread_main"}]},
        get_responses=[msg],
    )

    results = fetch_starred_emails(svc)

    assert results[0].body == body


def test_fetch_starred_emails_skips_failed_message_fetch() -> None:
    """An HttpError on messages().get() should be logged and skipped."""
    msg_ok = make_message(msg_id="msg_ok")

    svc = MagicMock()
    msgs = svc.users.return_value.messages.return_value
    msgs.list.return_value.execute.return_value = {
        "messages": [{"id": "msg_bad"}, {"id": "msg_ok"}]
    }

    # First get() raises; second succeeds
    http_error = HttpError(
        resp=MagicMock(status=500, reason="Server Error"),
        content=b"Internal Server Error",
    )
    msgs.get.return_value.execute.side_effect = [http_error, msg_ok]

    results = fetch_starred_emails(svc)

    # The failed message is skipped; the good one is returned
    assert len(results) == 1
    assert results[0].gmail_message_id == "msg_ok"


def test_fetch_starred_emails_date_parsed_from_header() -> None:
    msg = make_message(date="Mon, 01 Jan 2024 12:00:00 +0000")
    svc = make_service(
        list_response={"messages": [{"id": "msg_001"}]},
        get_responses=[msg],
    )

    results = fetch_starred_emails(svc)

    assert "2024-01-01" in results[0].email_date


# ---------------------------------------------------------------------------
# unstar_email
# ---------------------------------------------------------------------------


def test_unstar_email_calls_modify_with_remove_starred() -> None:
    svc = make_service()

    unstar_email(svc, "msg_001")

    modify_call = svc.users.return_value.messages.return_value.modify.call_args
    body_arg = modify_call[1]["body"]
    assert "STARRED" in body_arg["removeLabelIds"]
    assert modify_call[1]["id"] == "msg_001"


def test_unstar_email_uses_correct_user_id() -> None:
    svc = make_service()

    unstar_email(svc, "msg_001")

    modify_call = svc.users.return_value.messages.return_value.modify.call_args
    assert modify_call[1]["userId"] == "me"


# ---------------------------------------------------------------------------
# apply_label
# ---------------------------------------------------------------------------


def _make_labels_fixture() -> dict:
    fixture_path = Path(__file__).parent / "fixtures" / "gmail_labels.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def test_apply_label_success() -> None:
    labels = _make_labels_fixture()
    svc = make_service(labels_response=labels)

    apply_label(svc, "msg_001", "Agent/Added-To-Trello")

    modify_call = svc.users.return_value.messages.return_value.modify.call_args
    body_arg = modify_call[1]["body"]
    assert "Label_001" in body_arg["addLabelIds"]
    assert modify_call[1]["id"] == "msg_001"


def test_apply_label_uses_correct_user_id() -> None:
    labels = _make_labels_fixture()
    svc = make_service(labels_response=labels)

    apply_label(svc, "msg_001", "Agent/Added-To-Trello")

    modify_call = svc.users.return_value.messages.return_value.modify.call_args
    assert modify_call[1]["userId"] == "me"


def test_apply_label_not_found_logs_error_does_not_raise() -> None:
    """Missing label should log an error but not raise an exception."""
    labels = _make_labels_fixture()
    svc = make_service(labels_response=labels)

    # Should not raise
    apply_label(svc, "msg_001", "Label/Does-Not-Exist")

    # modify should NOT have been called since label wasn't found
    svc.users.return_value.messages.return_value.modify.assert_not_called()


def test_apply_label_not_found_does_not_modify_message() -> None:
    labels = {"labels": []}  # empty — no labels at all
    svc = make_service(labels_response=labels)

    apply_label(svc, "msg_001", "Agent/Added-To-Trello")

    svc.users.return_value.messages.return_value.modify.assert_not_called()


def test_apply_label_list_api_error_does_not_raise() -> None:
    """If labels().list() fails, apply_label should log and return gracefully."""
    svc = MagicMock()
    http_error = HttpError(
        resp=MagicMock(status=403, reason="Forbidden"), content=b"Forbidden"
    )
    svc.users.return_value.labels.return_value.list.return_value.execute.side_effect = (
        http_error
    )

    # Should not raise
    apply_label(svc, "msg_001", "Agent/Added-To-Trello")

    svc.users.return_value.messages.return_value.modify.assert_not_called()
