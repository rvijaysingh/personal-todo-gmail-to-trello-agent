"""SQLite processing ledger: emails_processed table, dedup queries."""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.models import CardPayload, EmailRecord, ProcessingResult

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS emails_processed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT UNIQUE NOT NULL,
    subject TEXT,
    sender TEXT,
    email_date TEXT,
    generated_card_name TEXT,
    card_name_source TEXT CHECK(card_name_source IN ('anthropic', 'ollama', 'fallback')),
    trello_card_id TEXT,
    trello_card_url TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'success',
        'failed_trello_create',
        'failed_gmail_label',
        'skipped_dedup'
    )),
    error_message TEXT,
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_INSERT_SQL = """
INSERT OR REPLACE INTO emails_processed
    (gmail_message_id, subject, sender, email_date,
     generated_card_name, card_name_source,
     trello_card_id, trello_card_url, status, error_message)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def init_db(db_path: str) -> None:
    """Create the emails_processed table if it does not exist.

    Args:
        db_path: Filesystem path to the SQLite database file.
            Parent directories are created automatically if absent.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(_CREATE_TABLE_SQL)
        conn.commit()
    logger.info("Database initialized at %s", db_path)


def insert_record(
    db_path: str,
    email: EmailRecord,
    card: Optional[CardPayload],
    result: ProcessingResult,
) -> None:
    """Insert or replace a processing record in the database.

    Uses INSERT OR REPLACE so a retry run that re-processes the same message ID
    overwrites the previous (failed) record.

    Args:
        db_path: Filesystem path to the SQLite database file.
        email: The email that was processed.
        card: The card payload that was built, or None if card creation was
            not attempted (e.g. a Trello failure before the card was named).
        result: The outcome of processing the email.
    """
    row = (
        email.gmail_message_id,
        email.subject,
        email.sender,
        email.email_date,
        card.name if card else None,
        card.card_name_source if card else None,
        result.trello_card_id,
        result.trello_card_url,
        result.status,
        result.error_message,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(_INSERT_SQL, row)
        conn.commit()
    logger.info(
        "Recorded gmail_message_id=%s status=%s",
        email.gmail_message_id,
        result.status,
    )


def check_duplicate(db_path: str, gmail_message_id: str) -> bool:
    """Return True if this email was already successfully processed.

    Only rows with status='success' count as processed. Failed or skipped
    records allow the email to be retried.

    Args:
        db_path: Filesystem path to the SQLite database file.
        gmail_message_id: Gmail message ID to look up.

    Returns:
        True if a successful record exists for this message ID.
    """
    sql = (
        "SELECT 1 FROM emails_processed "
        "WHERE gmail_message_id = ? AND status = 'success' LIMIT 1"
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(sql, (gmail_message_id,)).fetchone()
    is_dup = row is not None
    logger.debug("check_duplicate(%s) -> %s", gmail_message_id, is_dup)
    return is_dup


def get_last_run_time(db_path: str) -> Optional[datetime]:
    """Return the timestamp of the most recently processed record, or None.

    Used by the orchestrator to detect first-run vs. subsequent-run behavior.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        The maximum processed_at timestamp as a datetime, or None if the
        table is empty (indicating this is the first run).
    """
    sql = "SELECT MAX(processed_at) FROM emails_processed"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(sql).fetchone()
    if row and row[0]:
        ts = datetime.fromisoformat(row[0])
        logger.debug("get_last_run_time -> %s", ts)
        return ts
    logger.debug("get_last_run_time -> None (empty table, first run)")
    return None


if __name__ == "__main__":
    import tempfile

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    print(f"Using temp DB: {db_path}")
    init_db(db_path)
    print(f"Last run time (empty): {get_last_run_time(db_path)}")

    email = EmailRecord(
        gmail_message_id="msg_demo_001",
        subject="Re: Q3 Board Deck - Final Review",
        sender="Alice <alice@example.com>",
        email_date="2026-03-08T10:00:00",
        body="Please review the attached deck before Friday.",
    )
    card = CardPayload(
        name="Review and approve Q3 board deck",
        description="See email from Alice...",
        card_name_source="llm",
    )
    result = ProcessingResult(
        gmail_message_id="msg_demo_001",
        status="success",
        trello_card_id="card_abc123",
        trello_card_url="https://trello.com/c/abc123",
    )
    insert_record(db_path, email, card, result)
    print(f"Duplicate check (msg_demo_001): {check_duplicate(db_path, 'msg_demo_001')}")
    print(f"Duplicate check (msg_unknown):  {check_duplicate(db_path, 'msg_unknown')}")
    print(f"Last run time: {get_last_run_time(db_path)}")
