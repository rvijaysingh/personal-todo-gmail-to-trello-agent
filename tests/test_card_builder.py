"""Tests for src/card_builder.py."""

import json

import pytest

from src.card_builder import (
    _clean_subject,
    _format_date,
    _format_email_date_for_prompt,
    _validate_and_format_due_date,
    build_card_description,
    generate_card_name,
)
from src.models import EmailRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = (
    "Date: {{email_date}}\nSubject: {{subject}}\nBody: {{body_preview}}\nJSON:"
)


def make_email(
    subject: str = "Test Subject",
    sender: str = "Alice <alice@example.com>",
    email_date: str = "2026-03-08T10:00:00+00:00",
    body: str = "This is the email body.",
    msg_id: str = "msg_001",
) -> EmailRecord:
    return EmailRecord(
        gmail_message_id=msg_id,
        subject=subject,
        sender=sender,
        email_date=email_date,
        body=body,
    )


def llm_ok(
    name: str = "Review the document",
    due_date: str | None = None,
    source: str = "llm",
) -> callable:
    """Return an llm_client callable that returns a JSON string."""
    json_text = json.dumps({"card_name": name, "due_date": due_date})
    return lambda subject, body, template: (json_text, source)


def llm_down() -> callable:
    """Return an llm_client callable that always returns None (LLM unavailable)."""
    return lambda subject, body, template: None


def llm_raw(raw_text: str, source: str = "llm") -> callable:
    """Return an llm_client callable that returns the given raw text string."""
    return lambda subject, body, template: (raw_text, source)


# ---------------------------------------------------------------------------
# _clean_subject unit tests
# ---------------------------------------------------------------------------


def test_clean_subject_strips_re_prefix() -> None:
    assert _clean_subject("Re: Meeting notes") == "Meeting notes"


def test_clean_subject_strips_re_prefix_uppercase() -> None:
    assert _clean_subject("RE: Meeting notes") == "Meeting notes"


def test_clean_subject_strips_re_prefix_lowercase() -> None:
    assert _clean_subject("re: Meeting notes") == "Meeting notes"


def test_clean_subject_strips_fwd_prefix() -> None:
    assert _clean_subject("Fwd: Invoice attached") == "Invoice attached"


def test_clean_subject_strips_fwd_prefix_uppercase() -> None:
    assert _clean_subject("FWD: Invoice attached") == "Invoice attached"


def test_clean_subject_strips_fw_prefix() -> None:
    assert _clean_subject("FW: Invoice attached") == "Invoice attached"


def test_clean_subject_strips_fw_prefix_lowercase() -> None:
    assert _clean_subject("fw: Invoice attached") == "Invoice attached"


def test_clean_subject_strips_multiple_prefixes() -> None:
    assert _clean_subject("Re: Fwd: Re: Board deck review") == "Board deck review"


def test_clean_subject_strips_prefix_with_extra_spaces() -> None:
    assert _clean_subject("Re:   Meeting notes") == "Meeting notes"


def test_clean_subject_collapses_internal_spaces() -> None:
    assert _clean_subject("Subject   with   extra   spaces") == "Subject with extra spaces"


def test_clean_subject_trims_leading_trailing_whitespace() -> None:
    assert _clean_subject("  Subject  ") == "Subject"


def test_clean_subject_no_prefix_unchanged() -> None:
    assert _clean_subject("Q3 Board Deck") == "Q3 Board Deck"


def test_clean_subject_empty_result_falls_back_to_original() -> None:
    result = _clean_subject("Re:")
    assert result != ""


def test_clean_subject_preserves_re_within_subject() -> None:
    assert _clean_subject("Re: Renewal reminder") == "Renewal reminder"


# ---------------------------------------------------------------------------
# _format_date unit tests
# ---------------------------------------------------------------------------


def test_format_date_iso_utc() -> None:
    assert _format_date("2026-03-08T10:00:00+00:00") == "March 8, 2026"


def test_format_date_iso_no_tz() -> None:
    assert _format_date("2026-01-15T09:30:00") == "January 15, 2026"


def test_format_date_day_without_leading_zero() -> None:
    result = _format_date("2026-03-01T00:00:00")
    assert "March 1, 2026" == result


def test_format_date_invalid_returns_original() -> None:
    raw = "not a date at all"
    assert _format_date(raw) == raw


def test_format_date_empty_string_returns_empty() -> None:
    assert _format_date("") == ""


# ---------------------------------------------------------------------------
# _format_email_date_for_prompt unit tests
# ---------------------------------------------------------------------------


def test_format_email_date_for_prompt_includes_day_of_week() -> None:
    result = _format_email_date_for_prompt("2026-03-08T10:00:00+00:00")
    assert "Sunday" in result  # 2026-03-08 is a Sunday


def test_format_email_date_for_prompt_includes_month_and_year() -> None:
    result = _format_email_date_for_prompt("2026-04-09T10:00:00+00:00")
    assert "April" in result
    assert "2026" in result


def test_format_email_date_for_prompt_no_leading_zero_on_day() -> None:
    result = _format_email_date_for_prompt("2026-04-09T10:00:00+00:00")
    # Should be "9" not "09"
    assert " 9," in result


def test_format_email_date_for_prompt_invalid_returns_raw() -> None:
    raw = "not a date"
    assert _format_email_date_for_prompt(raw) == raw


# ---------------------------------------------------------------------------
# _validate_and_format_due_date unit tests
# ---------------------------------------------------------------------------


def test_validate_and_format_due_date_valid_date_returns_utc_noon() -> None:
    result = _validate_and_format_due_date("2026-05-09", "msg_001")
    assert result == "2026-05-09T12:00:00.000Z"


def test_validate_and_format_due_date_none_returns_none() -> None:
    assert _validate_and_format_due_date(None, "msg_001") is None


def test_validate_and_format_due_date_malformed_returns_none() -> None:
    assert _validate_and_format_due_date("May 9th", "msg_001") is None


def test_validate_and_format_due_date_partial_date_returns_none() -> None:
    assert _validate_and_format_due_date("2026-05", "msg_001") is None


def test_validate_and_format_due_date_non_string_returns_none() -> None:
    assert _validate_and_format_due_date(20260509, "msg_001") is None


def test_validate_and_format_due_date_empty_string_returns_none() -> None:
    assert _validate_and_format_due_date("", "msg_001") is None


def test_validate_and_format_due_date_preserves_date_value() -> None:
    result = _validate_and_format_due_date("2026-12-31", "msg_001")
    assert result is not None
    assert result.startswith("2026-12-31")


# ---------------------------------------------------------------------------
# generate_card_name — LLM success with JSON
# ---------------------------------------------------------------------------


def test_generate_card_name_returns_llm_name_and_source() -> None:
    email = make_email(subject="Re: Q3 Board Deck")
    name, source, due_date = generate_card_name(
        email, llm_ok("Review Q3 board deck"), PROMPT_TEMPLATE
    )
    assert name == "Review Q3 board deck"
    assert source == "llm"


def test_generate_card_name_returns_due_date_from_llm() -> None:
    email = make_email()
    name, source, due_date = generate_card_name(
        email, llm_ok("Buy concert tickets", due_date="2026-05-09"), PROMPT_TEMPLATE
    )
    assert due_date == "2026-05-09T12:00:00.000Z"


def test_generate_card_name_returns_none_for_null_due_date() -> None:
    email = make_email()
    name, source, due_date = generate_card_name(
        email, llm_ok("Review document", due_date=None), PROMPT_TEMPLATE
    )
    assert due_date is None


def test_generate_card_name_source_propagated_from_llm() -> None:
    email = make_email()
    _, source, _ = generate_card_name(
        email, llm_ok(source="anthropic"), PROMPT_TEMPLATE
    )
    assert source == "anthropic"


def test_generate_card_name_truncates_long_card_name() -> None:
    long_name = "A" * 150
    email = make_email()
    name, _, _ = generate_card_name(email, llm_ok(long_name), PROMPT_TEMPLATE)
    assert len(name) <= 100


def test_generate_card_name_exactly_100_chars_not_truncated() -> None:
    exact_name = "B" * 100
    email = make_email()
    name, _, _ = generate_card_name(email, llm_ok(exact_name), PROMPT_TEMPLATE)
    assert len(name) == 100


def test_generate_card_name_malformed_due_date_returns_none() -> None:
    email = make_email()
    json_text = json.dumps({"card_name": "Do something", "due_date": "May 9th"})
    name, _, due_date = generate_card_name(
        email, llm_raw(json_text), PROMPT_TEMPLATE
    )
    assert name == "Do something"
    assert due_date is None


def test_generate_card_name_llm_receives_correct_subject() -> None:
    received: list[str] = []

    def capturing_llm(subject: str, body: str, template: str):
        received.append(subject)
        return (json.dumps({"card_name": "task", "due_date": None}), "llm")

    email = make_email(subject="My Email Subject")
    generate_card_name(email, capturing_llm, PROMPT_TEMPLATE)
    assert received[0] == "My Email Subject"


def test_generate_card_name_llm_receives_first_500_chars_of_body() -> None:
    long_body = "X" * 1000
    received_body: list[str] = []

    def capturing_llm(subject: str, body: str, template: str):
        received_body.append(body)
        return (json.dumps({"card_name": "task", "due_date": None}), "llm")

    email = make_email(body=long_body)
    generate_card_name(email, capturing_llm, PROMPT_TEMPLATE)
    assert len(received_body[0]) == 500


def test_generate_card_name_prompt_has_email_date_substituted() -> None:
    received_template: list[str] = []

    def capturing_llm(subject: str, body: str, template: str):
        received_template.append(template)
        return (json.dumps({"card_name": "task", "due_date": None}), "llm")

    email = make_email(email_date="2026-03-08T10:00:00+00:00")
    generate_card_name(email, capturing_llm, PROMPT_TEMPLATE)
    assert "{{email_date}}" not in received_template[0]
    assert "2026" in received_template[0]


# ---------------------------------------------------------------------------
# generate_card_name — fallback path
# ---------------------------------------------------------------------------


def test_generate_card_name_falls_back_when_llm_returns_none() -> None:
    email = make_email(subject="Invoice from supplier")
    name, source, due_date = generate_card_name(email, llm_down(), PROMPT_TEMPLATE)
    assert name == "Invoice from supplier"
    assert source == "fallback"
    assert due_date is None


def test_generate_card_name_fallback_strips_re_prefix() -> None:
    email = make_email(subject="Re: Q3 Board Deck - Final Review")
    name, source, _ = generate_card_name(email, llm_down(), PROMPT_TEMPLATE)
    assert name == "Q3 Board Deck - Final Review"
    assert source == "fallback"


def test_generate_card_name_fallback_strips_fwd_prefix() -> None:
    email = make_email(subject="Fwd: Invoice attached")
    name, source, _ = generate_card_name(email, llm_down(), PROMPT_TEMPLATE)
    assert name == "Invoice attached"
    assert source == "fallback"


def test_generate_card_name_fallback_strips_fw_prefix() -> None:
    email = make_email(subject="FW: Action required")
    name, source, _ = generate_card_name(email, llm_down(), PROMPT_TEMPLATE)
    assert name == "Action required"
    assert source == "fallback"


def test_generate_card_name_fallback_strips_multiple_prefixes() -> None:
    email = make_email(subject="Re: Fwd: Re: Meeting notes")
    name, _, _ = generate_card_name(email, llm_down(), PROMPT_TEMPLATE)
    assert name == "Meeting notes"


def test_generate_card_name_fallback_due_date_is_none() -> None:
    email = make_email(subject="Plain subject")
    _, _, due_date = generate_card_name(email, llm_down(), PROMPT_TEMPLATE)
    assert due_date is None


def test_generate_card_name_fallback_when_llm_raises() -> None:
    def crashing_llm(subject: str, body: str, template: str):
        raise RuntimeError("Unexpected crash")

    email = make_email(subject="Re: Important update")
    name, source, due_date = generate_card_name(email, crashing_llm, PROMPT_TEMPLATE)
    assert source == "fallback"
    assert name == "Important update"
    assert due_date is None


def test_generate_card_name_fallback_when_llm_returns_invalid_json() -> None:
    email = make_email(subject="Invalid JSON test")
    name, source, due_date = generate_card_name(
        email, llm_raw("not valid json at all"), PROMPT_TEMPLATE
    )
    assert source == "fallback"
    assert name == "Invalid JSON test"
    assert due_date is None


def test_generate_card_name_fallback_when_card_name_missing_from_json() -> None:
    email = make_email(subject="Re: Missing field")
    json_text = json.dumps({"due_date": "2026-05-09"})  # no card_name
    name, source, due_date = generate_card_name(
        email, llm_raw(json_text), PROMPT_TEMPLATE
    )
    assert source == "fallback"
    assert name == "Missing field"
    assert due_date is None


def test_generate_card_name_fallback_when_card_name_is_empty_string() -> None:
    email = make_email(subject="Re: Empty name")
    json_text = json.dumps({"card_name": "   ", "due_date": None})
    name, source, _ = generate_card_name(email, llm_raw(json_text), PROMPT_TEMPLATE)
    assert source == "fallback"
    assert name == "Empty name"


# ---------------------------------------------------------------------------
# build_card_description — format
# ---------------------------------------------------------------------------


def test_build_card_description_starts_with_bullet_see_header() -> None:
    email = make_email(subject="Invoice from supplier", sender="Bob <bob@example.com>")
    desc = build_card_description(email)
    assert desc.startswith('\u2022 See "Invoice from supplier" email from Bob <bob@example.com>')


def test_build_card_description_header_format() -> None:
    email = make_email(
        subject="Q3 Board Deck",
        sender="Alice <alice@example.com>",
        email_date="2026-03-08T10:00:00+00:00",
    )
    desc = build_card_description(email)
    lines = desc.split("\n")
    assert lines[0] == '\u2022 See "Q3 Board Deck" email from Alice <alice@example.com> on March 8, 2026'
    assert lines[1] == ""
    assert lines[2] == "------"
    assert lines[3] == ""


def test_build_card_description_has_blank_line_before_and_after_separator() -> None:
    email = make_email()
    desc = build_card_description(email)
    lines = desc.split("\n")
    assert lines[1] == ""
    assert lines[2] == "------"
    assert lines[3] == ""


def test_build_card_description_body_appears_after_separator() -> None:
    body = "This is the email body content."
    email = make_email(body=body)
    desc = build_card_description(email)
    assert body in desc
    sep = "\n\n------\n\n"
    sep_pos = desc.index(sep)
    assert desc[sep_pos + len(sep):].startswith(body)


def test_build_card_description_formats_date_human_readable() -> None:
    email = make_email(email_date="2026-01-15T09:00:00+00:00")
    desc = build_card_description(email)
    assert "January 15, 2026" in desc


# ---------------------------------------------------------------------------
# build_card_description — no truncation
# ---------------------------------------------------------------------------


def test_build_card_description_no_truncation_when_under_limit() -> None:
    email = make_email(body="Short body.")
    desc = build_card_description(email, max_chars=16384)
    assert "[Email body truncated" not in desc


def test_build_card_description_no_truncation_at_exact_limit() -> None:
    email = make_email(subject="S", sender="s@s.com", email_date="2026-01-01T00:00:00", body="B")
    desc = build_card_description(email, max_chars=16384)
    assert "[Email body truncated" not in desc


# ---------------------------------------------------------------------------
# build_card_description — truncation
# ---------------------------------------------------------------------------


def test_build_card_description_truncation_notice_present_when_over_limit() -> None:
    email = make_email(body="X" * 20000)
    desc = build_card_description(email, max_chars=16384)
    assert "[Email body truncated" in desc


def test_build_card_description_total_length_within_max_chars_after_truncation() -> None:
    email = make_email(body="X" * 20000)
    desc = build_card_description(email, max_chars=16384)
    assert len(desc) <= 16384


def test_build_card_description_truncation_with_custom_max_chars() -> None:
    email = make_email(body="Y" * 500)
    desc = build_card_description(email, max_chars=300)
    assert len(desc) <= 300
    assert "[Email body truncated" in desc


def test_build_card_description_truncation_notice_exact_text() -> None:
    email = make_email(body="Z" * 20000)
    desc = build_card_description(email, max_chars=16384)
    expected_notice = "[Email body truncated -- original exceeds Trello's 16,384 character limit]"
    assert expected_notice in desc


def test_build_card_description_body_prefix_preserved_before_truncation() -> None:
    body_start = "First paragraph of the email."
    email = make_email(body=body_start + " " + "filler " * 5000)
    desc = build_card_description(email, max_chars=500)
    assert body_start in desc


def test_build_card_description_header_always_present_after_truncation() -> None:
    email = make_email(subject="Critical task", body="A" * 20000)
    desc = build_card_description(email, max_chars=16384)
    assert 'See "Critical task"' in desc
