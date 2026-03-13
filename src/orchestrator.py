"""Main entry point and pipeline coordinator."""

import functools
import logging
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from src import card_builder, gmail_client
from src.card_builder import LlmClientFn
from src.models import CardPayload, EmailRecord, ProcessingResult
from agent_shared.infra.config_loader import AgentConfig, ConfigError, GlobalConfig, load_config
from agent_shared.infra import db
from agent_shared.llm import client as llm_client
from agent_shared.trello import client as trello_client
from agent_shared.trello.client import TrelloError

logger = logging.getLogger(__name__)

_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_LOG_BACKUP_COUNT = 3
_PROMPT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "card_name.md"
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _setup_rotating_logger(log_file: str) -> None:
    """Configure the root logger with a rotating file handler.

    Creates parent directories for the log file automatically.

    Args:
        log_file: Filesystem path to the agent log file.
    """
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        log_file,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logger.info("Logging initialised to %s", log_file)


def _days_since_last_run(last_run: datetime) -> int:
    """Return the number of complete days between last_run and now (UTC).

    Treats naive datetimes as UTC (SQLite's datetime('now') returns UTC without
    timezone info).

    Args:
        last_run: Timestamp returned by db.get_last_run_time().

    Returns:
        Number of complete days since last_run (0 if run earlier today).
    """
    now = datetime.now(timezone.utc)
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)
    return (now - last_run).days


def _load_prompt_template() -> str:
    """Load the card_name.md LLM prompt template from disk.

    Returns:
        Template string with {{subject}} and {{body_preview}} placeholders.

    Raises:
        SystemExit: If the template file is not found.
    """
    if not _PROMPT_TEMPLATE_PATH.exists():
        logger.error("Prompt template not found at %s", _PROMPT_TEMPLATE_PATH)
        sys.exit(1)
    return _PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")


def _process_email(
    email: EmailRecord,
    gc: GlobalConfig,
    ac: AgentConfig,
    gmail_svc: object,
    llm_fn: LlmClientFn,
    prompt_template: str,
) -> ProcessingResult:
    """Process a single starred email through Steps 2–7 of the pipeline.

    Generates a card name, builds the Trello card description, creates the
    card, removes the Gmail star, applies the processed label, and records the
    outcome in the database.  Each failure mode records its own DB row and
    returns immediately so the calling loop can move on.

    Args:
        email: The starred email to process.
        gc: Global configuration (API keys, OAuth paths).
        ac: Agent configuration (list ID, label name, limits).
        gmail_svc: Authenticated Gmail API service object from build_service().
        llm_fn: Callable (subject, body_excerpt, template) → (name, source) | None.
        prompt_template: Contents of prompts/card_name.md.

    Returns:
        ProcessingResult capturing the final status and card details.
    """
    logger.info(
        "Processing %s | subject: %r | from: %s",
        email.gmail_message_id,
        email.subject,
        email.sender,
    )

    # Step 2 — Generate card name (LLM or fallback)
    name, source = card_builder.generate_card_name(email, llm_fn, prompt_template)
    logger.info("Card name (%s): %r", source, name)

    # Step 3 — Build card description
    description = card_builder.build_card_description(
        email, max_chars=ac.trello_description_max_chars
    )
    card = CardPayload(name=name, description=description, card_name_source=source)

    # Step 5 — Create Trello card
    try:
        card_id, card_url = trello_client.create_card(
            ac.trello_list_id,
            card.name,
            card.description,
            gc.trello_api_key,
            gc.trello_api_token,
        )
    except TrelloError as exc:
        logger.error(
            "Trello card creation failed for %s: %s", email.gmail_message_id, exc
        )
        result = ProcessingResult(
            gmail_message_id=email.gmail_message_id,
            status="failed_trello_create",
            error_message=str(exc),
        )
        db.insert_record(ac.db_path, email, card, result)
        return result

    logger.info("Trello card created: %s", card_url)

    # Step 6 — Apply processed label (apply_label logs errors internally;
    # wrap in try/except as a safety net for unexpected failures)
    try:
        gmail_client.apply_label(
            gmail_svc, email.gmail_message_id, ac.gmail_processed_label
        )
    except Exception as exc:
        logger.error(
            "Failed to apply label for %s: %s", email.gmail_message_id, exc
        )
        result = ProcessingResult(
            gmail_message_id=email.gmail_message_id,
            status="failed_gmail_label",
            trello_card_id=card_id,
            trello_card_url=card_url,
            error_message=str(exc),
        )
        db.insert_record(ac.db_path, email, card, result)
        return result

    # Step 7 — Record success
    result = ProcessingResult(
        gmail_message_id=email.gmail_message_id,
        status="success",
        trello_card_id=card_id,
        trello_card_url=card_url,
    )
    db.insert_record(ac.db_path, email, card, result)
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(
    env_config_path: Optional[str] = None,
    agent_config_path: Optional[str] = None,
) -> None:
    """Execute one full agent run: fetch starred emails and create Trello cards.

    Startup sequence:
    1.  Load configuration — exit on ConfigError.
    2.  Set up rotating file logger.
    3.  Initialise the SQLite database.
    4.  Check Ollama health — warning only; continues on failure.
    5.  Validate the target Trello list — exit if not found.
    6.  Load the LLM prompt template.
    7.  Build the authenticated Gmail API service.
    8.  Detect first-run vs. subsequent run.
    9.  Fetch starred emails (sorted oldest first).
    10. Process each email through the pipeline.

    Args:
        env_config_path: Override path to global .env.json.  Defaults to the
            path resolved from ENV_CONFIG_PATH or the repo-local default.
        agent_config_path: Override path to agent_config.json.  Defaults to
            config/agent_config.json relative to the repo root.
    """
    # Step 1 — Load configuration
    try:
        gc, ac = load_config(env_config_path, agent_config_path)
    except ConfigError as exc:
        print(f"ERROR: Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Step 2 — Set up file logging
    _setup_rotating_logger(ac.log_file)
    logger.info("Gmail-to-Trello agent starting")

    # Step 3 — Initialise database
    db.init_db(ac.db_path)

    # Step 4 — Log LLM provider availability
    anthropic_api_key = gc.anthropic_api_keys.get("gmail-to-trello", "")
    ollama_ok = llm_client.health_check(gc)
    if anthropic_api_key:
        if ollama_ok:
            logger.info(
                "LLM: Anthropic API (Haiku 4.5) configured as primary, Ollama as fallback"
            )
        else:
            logger.info(
                "LLM: Anthropic API (Haiku 4.5) configured as primary, "
                "Ollama unreachable — no local fallback"
            )
    elif ollama_ok:
        logger.info("LLM: No Anthropic API key — using Ollama as primary")
    else:
        logger.warning(
            "LLM: No Anthropic API key and Ollama unreachable — "
            "using subject line fallback only"
        )

    # Step 5 — Validate Trello list
    try:
        list_found = trello_client.validate_list(
            ac.trello_list_id,
            gc.trello_board_id,
            gc.trello_api_key,
            gc.trello_api_token,
        )
    except TrelloError as exc:
        logger.error("Could not validate Trello list: %s — exiting", exc)
        sys.exit(1)

    if not list_found:
        logger.error(
            "Trello list %s not found on board %s — exiting",
            ac.trello_list_id,
            gc.trello_board_id,
        )
        sys.exit(1)

    # Step 6 — Load prompt template
    prompt_template = _load_prompt_template()

    # Step 7 — Build Gmail service
    gmail_svc = gmail_client.build_service(
        gc.gmail_oauth_credentials_path, gc.gmail_oauth_token_path
    )

    # Step 8 — First-run vs. subsequent-run detection
    last_run = db.get_last_run_time(ac.db_path)
    if last_run is None:
        logger.info(
            "First run detected — limiting to starred emails from the last %d days",
            ac.first_run_lookback_days,
        )
        max_age_days: Optional[int] = ac.first_run_lookback_days
    else:
        days_since = _days_since_last_run(last_run)
        max_age_days = days_since + 2  # +2 day buffer
        logger.info(
            "Subsequent run — last run %d day(s) ago, querying newer_than:%dd",
            days_since,
            max_age_days,
        )

    # Bind GlobalConfig, timeout, and anthropic_api_key into the LLM callable so
    # card_builder receives only (subject, body_excerpt, template) as required
    # by LlmClientFn.
    llm_fn: LlmClientFn = functools.partial(
        llm_client.generate_card_name,
        config=gc,
        timeout=ac.llm_timeout_seconds,
        anthropic_api_key=anthropic_api_key,
    )

    # Step 9 — Fetch starred emails (oldest first), excluding already-processed ones
    emails = gmail_client.fetch_starred_emails(
        gmail_svc,
        max_age_days=max_age_days,
        processed_label=ac.gmail_processed_label,
    )
    total = len(emails)

    if total > ac.max_emails_per_run:
        logger.warning(
            "Gmail returned %d emails — capping at max_emails_per_run=%d (oldest first)",
            total,
            ac.max_emails_per_run,
        )
        emails = emails[: ac.max_emails_per_run]
        total = ac.max_emails_per_run

    logger.info("Found %d starred email(s) to process", total)

    # Step 10 — Processing loop
    processed = 0
    failed = 0
    skipped = 0

    for i, email in enumerate(emails, 1):
        if total >= 10 and i % 10 == 0:
            logger.info("Progress: %d/%d", i, total)

        # Step 4 — Dedup check (before any API calls)
        if ac.dedup_enabled and db.check_duplicate(
            ac.db_path, email.gmail_message_id
        ):
            logger.info(
                "Already processed, skipping: %s (%r)",
                email.gmail_message_id,
                email.subject,
            )
            skipped += 1
            continue

        result = _process_email(email, gc, ac, gmail_svc, llm_fn, prompt_template)

        if result.status == "success":
            processed += 1
        else:
            failed += 1

        # Rate-limit delay between emails (skip after the last one)
        if ac.processing_delay_seconds > 0 and i < total:
            time.sleep(ac.processing_delay_seconds)

    logger.info(
        "Run complete — processed: %d  failed: %d  skipped: %d  total: %d",
        processed,
        failed,
        skipped,
        total,
    )


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    # Add a console handler so output is visible when run manually.
    _console = logging.StreamHandler()
    _console.setFormatter(
        logging.Formatter("%(levelname)-8s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(_console)
    logging.getLogger().setLevel(logging.INFO)

    _parser = argparse.ArgumentParser(
        description="Gmail-to-Trello agent: process starred emails into Trello cards"
    )
    _parser.add_argument("--env-config", help="Path to global .env.json")
    _parser.add_argument("--agent-config", help="Path to agent_config.json")
    _args = _parser.parse_args()

    run(env_config_path=_args.env_config, agent_config_path=_args.agent_config)
