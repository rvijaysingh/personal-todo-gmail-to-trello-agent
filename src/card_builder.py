"""Builds Trello card name (LLM or fallback) and description."""

import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Optional

from src.models import EmailRecord

logger = logging.getLogger(__name__)

# Body excerpt length passed to the LLM (per business rules)
_BODY_PREVIEW_CHARS = 500

# Truncation notice appended when the description exceeds the Trello limit
_TRUNCATION_NOTICE = (
    "\n[Email body truncated -- original exceeds Trello's 16,384 character limit]"
)

# Matches Re:, Fwd:, FW:, re:, fw:, fwd: at the start of a subject (any case)
_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:re|fwd?)\s*:\s*", re.IGNORECASE)

# Type alias: callable that takes (subject, body_excerpt, prompt_template)
# and returns (name, source) or None on failure.
LlmClientFn = Callable[[str, str, str], Optional[tuple[str, str]]]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clean_subject(subject: str) -> str:
    """Strip Re:/Fwd:/FW: prefixes and normalise whitespace.

    Strips all leading reply/forward prefixes iteratively (handles chains
    like "Re: Fwd: Re: Subject"). Collapses internal runs of whitespace.
    Falls back to the original subject if cleaning produces an empty string.

    Args:
        subject: Raw email subject line.

    Returns:
        Cleaned subject suitable for use as a Trello card name.
    """
    cleaned = subject
    while True:
        stripped = _SUBJECT_PREFIX_RE.sub("", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else subject


def _format_date(email_date: str) -> str:
    """Format an ISO 8601 date string to a human-readable form (e.g. March 8, 2026).

    Args:
        email_date: ISO 8601 date string as stored in EmailRecord.email_date.

    Returns:
        Human-readable date, or the original string if parsing fails.
    """
    try:
        dt = datetime.fromisoformat(email_date)
        return f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    except (ValueError, TypeError):
        logger.warning("Could not parse email_date %r for formatting", email_date)
        return email_date


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_card_name(
    email: EmailRecord,
    llm_client: LlmClientFn,
    prompt_template: str,
) -> tuple[str, str]:
    """Generate an actionable Trello card name for an email.

    Tries the LLM first. Falls back to a cleaned subject line if the LLM
    is unavailable or returns None. Always returns a non-empty name.

    Args:
        email: The email being processed.
        llm_client: Callable with signature
            (subject: str, body_excerpt: str, prompt_template: str)
            -> tuple[str, str] | None.
            Should return (name, "llm") on success, or None on any failure.
        prompt_template: The card_name.md prompt template string.

    Returns:
        Tuple of (card_name, source) where source is "llm" or "fallback".
    """
    body_excerpt = email.body[:_BODY_PREVIEW_CHARS]

    try:
        result = llm_client(email.subject, body_excerpt, prompt_template)
    except Exception as exc:
        logger.warning("LLM client raised unexpectedly: %s — using fallback", exc)
        result = None

    if result is not None:
        name, source = result
        logger.info("Card name from LLM: %r", name)
        return name, source

    # LLM unavailable or failed — clean the subject line
    name = _clean_subject(email.subject)
    logger.warning(
        "LLM unavailable — using fallback subject: %r (original: %r)",
        name,
        email.subject,
    )
    return name, "fallback"


def build_card_description(email: EmailRecord, max_chars: int = 16384) -> str:
    """Build the Trello card description from an email.

    Format:
        • See "<subject>" email from <sender> on <formatted date>

        ------

        <email body>

    The blank line between the bullet(s) and "------" prevents Markdown Setext
    heading interpretation (a line followed immediately by "------" renders as
    an h2 heading). Future metadata bullets go between the first bullet and the
    blank line (e.g. "• Possible duplicate of: [link]").

    If the total length exceeds max_chars, the body is truncated and a
    notice is appended.

    Args:
        email: The email being processed.
        max_chars: Maximum total description length (default matches Trello's
            16,384 character limit).

    Returns:
        Formatted description string, at most max_chars characters long.
    """
    formatted_date = _format_date(email.email_date)
    header = (
        f'\u2022 See "{email.subject}" email from {email.sender} on {formatted_date}'
    )
    separator = "\n\n------\n\n"

    full_description = header + separator + email.body
    if len(full_description) <= max_chars:
        return full_description

    # Truncate the body to fit within max_chars
    available_for_body = (
        max_chars - len(header) - len(separator) - len(_TRUNCATION_NOTICE)
    )
    if available_for_body < 0:
        available_for_body = 0

    truncated_body = email.body[:available_for_body]
    description = header + separator + truncated_body + _TRUNCATION_NOTICE

    logger.warning(
        "Description for message %s truncated from %d to %d chars",
        email.gmail_message_id,
        len(full_description),
        len(description),
    )
    return description


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Demo with a synthetic email
    sample = EmailRecord(
        gmail_message_id="demo_001",
        subject="Re: Fwd: Q3 Board Deck - Final Review",
        sender="Alice <alice@example.com>",
        email_date="2026-03-08T10:00:00+00:00",
        body="Please review the attached deck before Friday's board meeting. "
        "The key sections to focus on are slides 12-15 (financials) and "
        "the risk matrix on slide 22.",
    )

    template_path = Path(__file__).parent.parent / "prompts" / "card_name.md"
    template = template_path.read_text(encoding="utf-8")

    # Demonstrate fallback (no live LLM call)
    def _llm_unavailable(subject: str, body: str, tmpl: str) -> None:
        return None

    name, source = generate_card_name(sample, _llm_unavailable, template)
    print(f"Card name ({source}): {name}")

    desc = build_card_description(sample)
    print(f"\nCard description ({len(desc)} chars):\n{desc}")
