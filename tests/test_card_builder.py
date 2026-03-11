"""Tests for src/card_builder.py."""

import pytest

from src.card_builder import (
    _clean_subject,
    _format_date,
    build_card_description,
    generate_card_name,
)
from src.models import EmailRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = "Subject: {{subject}}\nBody: {{body_preview}}\nTask name:"


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


def llm_ok(name: str = "Review the document") -> callable:
    """Return an llm_client callable that always succeeds."""
    return lambda subject, body, template: (name, "llm")


def llm_down() -> callable:
    """Return an llm_client callable that always returns None (Ollama down)."""
    return lambda subject, body, template: None


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
    # A subject that is only a prefix (after stripping, result is empty)
    result = _clean_subject("Re:")
    assert result != ""


def test_clean_subject_preserves_re_within_subject() -> None:
    # "Re:" at the start should be stripped, but "re" elsewhere should not
    assert _clean_subject("Re: Renewal reminder") == "Renewal reminder"


# ---------------------------------------------------------------------------
# _format_date unit tests
# ---------------------------------------------------------------------------


def test_format_date_iso_utc() -> None:
    assert _format_date("2026-03-08T10:00:00+00:00") == "March 8, 2026"


def test_format_date_iso_no_tz() -> None:
    assert _format_date("2026-01-15T09:30:00") == "January 15, 2026"


def test_format_date_day_without_leading_zero() -> None:
    # Day should be 1, not 01
    result = _format_date("2026-03-01T00:00:00")
    assert "March 1, 2026" == result


def test_format_date_invalid_returns_original() -> None:
    raw = "not a date at all"
    assert _format_date(raw) == raw


def test_format_date_empty_string_returns_empty() -> None:
    assert _format_date("") == ""


# ---------------------------------------------------------------------------
# generate_card_name — LLM success
# ---------------------------------------------------------------------------


def test_generate_card_name_returns_llm_name_when_available() -> None:
    email = make_email(subject="Re: Q3 Board Deck")
    name, source = generate_card_name(email, llm_ok("Review Q3 board deck"), PROMPT_TEMPLATE)
    assert name == "Review Q3 board deck"
    assert source == "llm"


def test_generate_card_name_llm_source_is_llm() -> None:
    email = make_email()
    _, source = generate_card_name(email, llm_ok(), PROMPT_TEMPLATE)
    assert source == "llm"


def test_generate_card_name_llm_receives_correct_subject() -> None:
    """Verify the LLM callable receives the email subject."""
    received: list[str] = []

    def capturing_llm(subject: str, body: str, template: str):
        received.append(subject)
        return ("task", "llm")

    email = make_email(subject="My Email Subject")
    generate_card_name(email, capturing_llm, PROMPT_TEMPLATE)
    assert received[0] == "My Email Subject"


def test_generate_card_name_llm_receives_first_500_chars_of_body() -> None:
    """Verify the LLM callable receives at most 500 chars of the body."""
    long_body = "X" * 1000
    received_body: list[str] = []

    def capturing_llm(subject: str, body: str, template: str):
        received_body.append(body)
        return ("task", "llm")

    email = make_email(body=long_body)
    generate_card_name(email, capturing_llm, PROMPT_TEMPLATE)
    assert len(received_body[0]) == 500
    assert received_body[0] == "X" * 500


def test_generate_card_name_llm_receives_short_body_unchanged() -> None:
    short_body = "Short body."
    received_body: list[str] = []

    def capturing_llm(subject: str, body: str, template: str):
        received_body.append(body)
        return ("task", "llm")

    email = make_email(body=short_body)
    generate_card_name(email, capturing_llm, PROMPT_TEMPLATE)
    assert received_body[0] == short_body


def test_generate_card_name_llm_receives_prompt_template() -> None:
    received_template: list[str] = []

    def capturing_llm(subject: str, body: str, template: str):
        received_template.append(template)
        return ("task", "llm")

    email = make_email()
    generate_card_name(email, capturing_llm, PROMPT_TEMPLATE)
    assert received_template[0] == PROMPT_TEMPLATE


# ---------------------------------------------------------------------------
# generate_card_name — fallback path
# ---------------------------------------------------------------------------


def test_generate_card_name_falls_back_when_llm_returns_none() -> None:
    email = make_email(subject="Invoice from supplier")
    name, source = generate_card_name(email, llm_down(), PROMPT_TEMPLATE)
    assert name == "Invoice from supplier"
    assert source == "fallback"


def test_generate_card_name_fallback_strips_re_prefix() -> None:
    email = make_email(subject="Re: Q3 Board Deck - Final Review")
    name, source = generate_card_name(email, llm_down(), PROMPT_TEMPLATE)
    assert name == "Q3 Board Deck - Final Review"
    assert source == "fallback"


def test_generate_card_name_fallback_strips_fwd_prefix() -> None:
    email = make_email(subject="Fwd: Invoice attached")
    name, source = generate_card_name(email, llm_down(), PROMPT_TEMPLATE)
    assert name == "Invoice attached"
    assert source == "fallback"


def test_generate_card_name_fallback_strips_fw_prefix() -> None:
    email = make_email(subject="FW: Action required")
    name, source = generate_card_name(email, llm_down(), PROMPT_TEMPLATE)
    assert name == "Action required"
    assert source == "fallback"


def test_generate_card_name_fallback_strips_multiple_prefixes() -> None:
    email = make_email(subject="Re: Fwd: Re: Meeting notes")
    name, source = generate_card_name(email, llm_down(), PROMPT_TEMPLATE)
    assert name == "Meeting notes"
    assert source == "fallback"


def test_generate_card_name_fallback_source_is_fallback() -> None:
    email = make_email(subject="Plain subject")
    _, source = generate_card_name(email, llm_down(), PROMPT_TEMPLATE)
    assert source == "fallback"


def test_generate_card_name_fallback_when_llm_raises() -> None:
    """If the LLM callable raises an exception, fall back gracefully."""

    def crashing_llm(subject: str, body: str, template: str):
        raise RuntimeError("Unexpected crash")

    email = make_email(subject="Re: Important update")
    name, source = generate_card_name(email, crashing_llm, PROMPT_TEMPLATE)
    assert source == "fallback"
    assert name == "Important update"


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
    # Line 0: bullet metadata, line 1: blank, line 2: "------", line 3: blank, line 4+: body
    assert lines[0] == '\u2022 See "Q3 Board Deck" email from Alice <alice@example.com> on March 8, 2026'
    assert lines[1] == ""
    assert lines[2] == "------"
    assert lines[3] == ""


def test_build_card_description_has_blank_line_before_and_after_separator() -> None:
    email = make_email()
    desc = build_card_description(email)
    lines = desc.split("\n")
    # Line 0: "• See ...", line 1: blank, line 2: "------", line 3: blank, line 4+: body
    assert lines[1] == ""
    assert lines[2] == "------"
    assert lines[3] == ""


def test_build_card_description_body_appears_after_separator() -> None:
    body = "This is the email body content."
    email = make_email(body=body)
    desc = build_card_description(email)
    assert body in desc
    # Full string: "• See ...\n\n------\n\n[body]"
    # Body starts immediately after the "\n\n------\n\n" separator.
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
    # Under limit — no truncation notice
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
    # max_chars=300 is large enough for the header ("• See ..." ~76 chars) +
    # separator ("\n\n------\n\n" = 9 chars) + truncation notice (~75 chars)
    # = ~160 chars, leaving ~140 chars of body.
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
    """The start of the body should appear in the description even after truncation."""
    body_start = "First paragraph of the email."
    email = make_email(body=body_start + " " + "filler " * 5000)
    desc = build_card_description(email, max_chars=500)
    assert body_start in desc


def test_build_card_description_header_always_present_after_truncation() -> None:
    email = make_email(subject="Critical task", body="A" * 20000)
    desc = build_card_description(email, max_chars=16384)
    assert 'See "Critical task"' in desc
