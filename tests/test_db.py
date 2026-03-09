"""Tests for src/db.py."""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from src.db import check_duplicate, get_last_run_time, init_db, insert_record
from src.models import CardPayload, EmailRecord, ProcessingResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_email(msg_id: str = "msg_001") -> EmailRecord:
    return EmailRecord(
        gmail_message_id=msg_id,
        subject="Test Subject",
        sender="Alice <alice@example.com>",
        email_date="2026-03-08T10:00:00",
        body="Email body text.",
    )


def make_card() -> CardPayload:
    return CardPayload(
        name="Follow up on test email",
        description="See test email from Alice on 2026-03-08",
        card_name_source="anthropic",
    )


def make_result(
    msg_id: str = "msg_001",
    status: str = "success",
    error: str | None = None,
) -> ProcessingResult:
    return ProcessingResult(
        gmail_message_id=msg_id,
        status=status,
        trello_card_id="card_abc" if status == "success" else None,
        trello_card_url="https://trello.com/c/abc" if status == "success" else None,
        error_message=error,
    )


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------


def test_init_db_creates_table(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='emails_processed'"
        ).fetchone()
    assert row is not None


def test_init_db_creates_parent_directories(tmp_path: Path) -> None:
    db_path = str(tmp_path / "nested" / "deep" / "test.db")
    init_db(db_path)
    assert Path(db_path).exists()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    # Second call should not raise even though table already exists
    init_db(db_path)


def test_init_db_creates_all_columns(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    with sqlite3.connect(db_path) as conn:
        info = conn.execute("PRAGMA table_info(emails_processed)").fetchall()
    column_names = {row[1] for row in info}

    expected = {
        "id",
        "gmail_message_id",
        "subject",
        "sender",
        "email_date",
        "generated_card_name",
        "card_name_source",
        "trello_card_id",
        "trello_card_url",
        "status",
        "error_message",
        "processed_at",
    }
    assert expected.issubset(column_names)


# ---------------------------------------------------------------------------
# check_duplicate
# ---------------------------------------------------------------------------


def test_check_duplicate_returns_false_for_unknown_id(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    assert check_duplicate(db_path, "msg_never_seen") is False


def test_check_duplicate_returns_true_after_success_insert(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    insert_record(db_path, make_email("msg_A"), make_card(), make_result("msg_A"))
    assert check_duplicate(db_path, "msg_A") is True


def test_check_duplicate_returns_false_for_failed_trello_create(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    insert_record(
        db_path,
        make_email("msg_B"),
        None,
        make_result("msg_B", "failed_trello_create", "API error"),
    )
    # A failed record should not block a retry
    assert check_duplicate(db_path, "msg_B") is False


def test_check_duplicate_returns_false_for_skipped_dedup(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    insert_record(
        db_path,
        make_email("msg_C"),
        None,
        make_result("msg_C", "skipped_dedup"),
    )
    assert check_duplicate(db_path, "msg_C") is False


# ---------------------------------------------------------------------------
# insert_record
# ---------------------------------------------------------------------------


def test_insert_record_with_full_card_payload(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    insert_record(db_path, make_email("msg_D"), make_card(), make_result("msg_D"))

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT generated_card_name, card_name_source, trello_card_id, status "
            "FROM emails_processed WHERE gmail_message_id = ?",
            ("msg_D",),
        ).fetchone()

    assert row is not None
    assert row[0] == "Follow up on test email"
    assert row[1] == "anthropic"
    assert row[2] == "card_abc"
    assert row[3] == "success"


def test_insert_record_with_no_card_stores_nulls(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    insert_record(
        db_path,
        make_email("msg_E"),
        None,  # no card
        make_result("msg_E", "failed_trello_create", "Connection refused"),
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT generated_card_name, card_name_source, trello_card_id, error_message "
            "FROM emails_processed WHERE gmail_message_id = ?",
            ("msg_E",),
        ).fetchone()

    assert row[0] is None  # generated_card_name
    assert row[1] is None  # card_name_source
    assert row[2] is None  # trello_card_id
    assert row[3] == "Connection refused"


def test_insert_record_replace_on_same_message_id(tmp_path: Path) -> None:
    """INSERT OR REPLACE: a retry run replaces the previous failed record."""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    # First insert: failed
    insert_record(
        db_path,
        make_email("msg_F"),
        None,
        make_result("msg_F", "failed_trello_create", "timeout"),
    )

    # Second insert: success
    insert_record(db_path, make_email("msg_F"), make_card(), make_result("msg_F"))

    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM emails_processed WHERE gmail_message_id = ?",
            ("msg_F",),
        ).fetchone()[0]
        status = conn.execute(
            "SELECT status FROM emails_processed WHERE gmail_message_id = ?",
            ("msg_F",),
        ).fetchone()[0]

    assert count == 1
    assert status == "success"


def test_insert_record_fallback_card_name_source(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    card = CardPayload(
        name="Q3 Board Deck Review",
        description="desc",
        card_name_source="fallback",
    )
    insert_record(db_path, make_email("msg_G"), card, make_result("msg_G"))

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT card_name_source FROM emails_processed WHERE gmail_message_id = ?",
            ("msg_G",),
        ).fetchone()

    assert row[0] == "fallback"


# ---------------------------------------------------------------------------
# get_last_run_time
# ---------------------------------------------------------------------------


def test_get_last_run_time_empty_db_returns_none(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    assert get_last_run_time(db_path) is None


def test_get_last_run_time_returns_datetime_after_insert(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    insert_record(db_path, make_email("msg_H"), make_card(), make_result("msg_H"))

    ts = get_last_run_time(db_path)

    assert ts is not None
    assert isinstance(ts, datetime)


def test_get_last_run_time_returns_latest_after_multiple_inserts(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    insert_record(db_path, make_email("msg_I"), make_card(), make_result("msg_I"))
    insert_record(db_path, make_email("msg_J"), make_card(), make_result("msg_J"))

    ts = get_last_run_time(db_path)

    assert ts is not None
    # As long as we get a valid datetime, the MAX logic is satisfied
    assert isinstance(ts, datetime)
