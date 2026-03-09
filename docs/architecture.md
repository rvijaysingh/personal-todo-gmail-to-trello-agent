# Architecture

## Purpose

An automated agent that monitors Gmail for starred emails, generates an
actionable Trello card for each one using a local LLM (with a subject-line
fallback), then cleans up the email by removing the star and applying a
processed label. Runs unattended on a schedule via Windows Task Scheduler.

---

## Pipeline Diagram

```
  Gmail API
  (starred emails, oldest first)
       |
       v
  gmail_client.fetch_starred_emails()
  - OAuth2 auth (token auto-refresh)
  - MIME body extraction (plain text preferred; HTML fallback)
  - Sorted by internalDate ascending
       |
       v
  orchestrator: dedup check (SQLite)
  - Skip if gmail_message_id already has status='success'
       |
       v
  card_builder.generate_card_name()
  - llm_client.generate_card_name() via Ollama /api/generate
  - Fallback: _clean_subject() strips Re:/Fwd: prefixes
       |
       v
  card_builder.build_card_description()
  - Header line: See "<subject>" email from <sender> on <date>
  - Full email body (truncated with notice if > 16,384 chars)
       |
       v
  trello_client.create_card()
  - POST to Trello REST API (key + token auth)
  - pos="top" so newest email ends up at list top after batch
       |
       v
  Gmail post-processing
  - gmail_client.unstar_email()
  - gmail_client.apply_label("Agent/Added-To-Trello")
       |
       v
  db.insert_record()
  - SQLite: emails_processed table
  - Captures status, card URL, card name source, timestamps
```

---

## Module Responsibilities

**`src/orchestrator.py`** — Main entry point. Owns the startup sequence
(load config, init DB, health-check Ollama, validate Trello list, detect
first/subsequent run) and the per-email processing loop. Delegates every
domain concern to a dedicated module. Collects run statistics and logs a
summary on completion. No external API calls of its own.

**`src/config_loader.py`** — Loads and validates the two-tier configuration:
a global `.env.json` (secrets, shared across agents) and a local
`agent_config.json` (agent-specific tuning). Raises `ConfigError` on any
missing or invalid field so the agent fails fast at startup rather than
mid-run. Resolves the global config path from the `ENV_CONFIG_PATH`
environment variable with a sensible repo-local default.

**`src/gmail_client.py`** — Wraps the Google API Python client for Gmail.
Handles OAuth2 token refresh automatically; provides a `--reauth` CLI flag
for when the token has expired. Fetches starred messages, extracts the body
from nested MIME parts (prefers `text/plain`; strips HTML tags as fallback),
removes stars, and applies labels. All operations use the `gmail.modify`
scope. Depends on: `google-api-python-client`, `google-auth-oauthlib`.

**`src/trello_client.py`** — Thin wrapper over the Trello REST API using
`requests`. Exposes `validate_list` (startup sanity check) and `create_card`
(always posts with `pos="top"` for correct ordering). Raises `TrelloError`
on any HTTP error or connection failure so the orchestrator can record the
failure and preserve the Gmail star for retry. Depends on: `requests`.

**`src/card_builder.py`** — Pure functions for building card content from an
`EmailRecord`. `generate_card_name` calls the `LlmClientFn` callable and
falls back to `_clean_subject` on any failure. `build_card_description`
assembles the header line and full body, truncating with a notice if the
total exceeds Trello's 16,384-character limit. No external dependencies.

**`src/llm_client.py`** — Ollama API wrapper using only `urllib.request`
(no `requests` dependency). `health_check` pings `/api/tags` at startup.
`generate_card_name` sends the filled prompt template to `/api/generate`,
strips `<think>` tags produced by qwen3's reasoning mode, and returns
`(name, "llm")` or `None` on any failure. Depends on: stdlib only.

**`src/db.py`** — SQLite processing ledger using `sqlite3` stdlib. Creates
the `emails_processed` table on first run. Provides `insert_record` (uses
`INSERT OR REPLACE` so retries overwrite failed rows), `check_duplicate`
(only `status='success'` rows count), and `get_last_run_time` (returns
`None` on an empty table, signalling first run). Depends on: stdlib only.

**`src/models.py`** — Shared dataclasses: `EmailRecord` (raw email fields),
`CardPayload` (name + description + source), `ProcessingResult` (outcome,
card IDs, error). No logic; imported by all other modules.

---

## Data Flow

1. **Fetch** — `gmail_client.fetch_starred_emails()` returns a list of
   `EmailRecord` objects sorted oldest-first. Each record holds the Gmail
   message ID, subject, sender, date, and decoded plain-text body.

2. **Name generation** — `card_builder.generate_card_name(email, llm_fn,
   template)` receives the `EmailRecord` and an `LlmClientFn` callable
   (created with `functools.partial` binding the `GlobalConfig`). Returns
   `(name: str, source: "llm" | "fallback")`.

3. **Description** — `card_builder.build_card_description(email)` returns a
   formatted string ready to POST to Trello.

4. **Card creation** — `trello_client.create_card(...)` returns
   `(card_id, card_url)` which flow into the `ProcessingResult`.

5. **Post-processing** — `gmail_client.unstar_email` and `apply_label` mark
   the email as done in Gmail. If unstar fails, the card URL is still
   preserved in the `ProcessingResult` so the next run's dedup check
   prevents a duplicate card.

6. **Record** — `db.insert_record(db_path, email, card, result)` persists the
   full outcome. The `ProcessingResult.status` is one of:
   `success`, `failed_trello_create`, `failed_gmail_unstar`,
   `failed_gmail_label`, `skipped_dedup`.

---

## Key Design Decisions

### OAuth2 over IMAP for Gmail access

**Context:** The agent needs to read emails, remove stars, and apply labels.
IMAP could read and flag messages but cannot apply Gmail labels (a
Gmail-specific concept). Managing OAuth2 credentials adds setup friction.

**Options considered:** IMAP with App Passwords; Gmail REST API with OAuth2.

**Chosen:** Gmail REST API with OAuth2 via `google-api-python-client`.

**Tradeoffs:** OAuth2 provides full access to Gmail labels (required for the
processed-label feature) and aligns with Google's recommended approach.
Sacrifices: initial setup requires a Google Cloud project and an OAuth
consent flow. Revisit if Google deprecates the current credential model.

---

### SQLite over JSON file for the processing ledger

**Context:** The agent needs to track which emails have been processed to
prevent duplicate Trello cards across runs.

**Options considered:** Flat JSON file; SQLite database.

**Chosen:** SQLite via the `sqlite3` stdlib module.

**Tradeoffs:** SQLite provides atomic writes, supports concurrent reads for
ad-hoc queries, and naturally expresses the `UNIQUE` constraint on
`gmail_message_id`. `INSERT OR REPLACE` makes retry semantics simple. The
cost is a slightly more complex schema than a JSON dict. Revisit only if the
agent moves to a cloud environment where local disk persistence is
unavailable.

---

### Process one email at a time (sequential, not batched)

**Context:** The agent processes potentially many emails per run. A batch
approach would be faster.

**Options considered:** Process all emails concurrently; process in batches
of N; process strictly one at a time.

**Chosen:** One email at a time, fully committing each before moving to the
next.

**Tradeoffs:** Maximises crash safety — if the agent is killed mid-run, every
completed email has its Trello card created and its star removed. Unprocessed
emails remain starred and are picked up on the next run. The dedup check
handles the edge case where the card was created but the star removal failed.
Sacrifices: slower for large batches. Configurable `processing_delay_seconds`
makes rate limiting explicit.

---

### pos="top" with oldest-first processing for list ordering

**Context:** The Trello list should display the most recent email's card at
the top so the user sees the newest item first.

**Options considered:** pos="bottom" with newest-first fetch; pos="top" with
oldest-first fetch.

**Chosen:** Fetch oldest-first, create cards with `pos="top"`. After the
batch, each card pushes the previous one down, resulting in the newest email's
card sitting at the top.

**Tradeoffs:** Requires the specific combination to be correct (easy to get
backwards). Oldest-first processing also means the dedup check is most
useful for the emails most likely to have been partially processed in a prior
crashed run.
