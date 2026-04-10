# Architecture

## Purpose

An automated agent that monitors Gmail for starred emails, generates an
actionable Trello card for each one using an LLM (Anthropic Haiku as primary,
local Ollama as fallback, subject-line as final fallback), then applies a
processed label to the email. Stars are preserved. Runs unattended on a
schedule via Windows Task Scheduler.

---

## Pipeline Diagram

```
  Gmail IMAP (imap.gmail.com:993)
  (starred, not yet labeled "Agent/Added-To-Trello", oldest first)
       |
       v
  gmail_client.fetch_starred_emails()
  - IMAP auth (app password, never expires)
  - Search: FLAGGED NOT X-GM-LABELS "Agent/Added-To-Trello" [SINCE date]
  - RFC822 fetch + email.message_from_bytes() parsing
  - MIME body extraction (plain text preferred; HTML fallback)
  - Sorted by email Date header ascending
       |
       v
  orchestrator: dedup check (SQLite)
  - Skip if gmail_message_id already has status='success'
  - Secondary safety net; Gmail label exclusion is the primary guard
       |
       v
  card_builder.generate_card_name()
  - Tier 1: llm_client._anthropic_generate_card_name() via Anthropic API
  - Tier 2: llm_client._ollama_generate_card_name() via Ollama /api/generate
  - Tier 3: _clean_subject() strips Re:/Fwd: prefixes
       |
       v
  card_builder.build_card_description()
  - Line 1: - See "<subject>" email from <sender> on <date>
  - Line 2: ------  (separator; future bullet points go above this)
  - Line 3: blank
  - Line 4+: full email body (truncated with notice if > 16,384 chars)
       |
       v
  trello_client.create_card()
  - POST to Trello REST API (key + token auth)
  - pos="top" so newest email ends up at list top after batch
       |
       v
  Gmail post-processing
  - gmail_client.apply_label("Agent/Added-To-Trello")
  - Star is preserved (intentional — label is the processed indicator)
       |
       v
  db.insert_record()
  - SQLite: emails_processed table
  - Captures status, card URL, card name source, timestamps
       |
       v
  alert_notifier (agent_shared.alerts.notifier) — best-effort, non-blocking
  - On any unhandled exception: send_crash_alert (traceback included)
  - On run with failures (failed > 0): send_failure_summary (counts + errors)
  - On startup failure after config load: send_crash_alert before sys.exit(1)
  - All alert calls wrapped in try/except; SMTP failures are logged only
```

---

## Module Responsibilities

**`src/orchestrator.py`** — Main entry point. Owns the startup sequence
(load config, init DB, log LLM provider availability, health-check Ollama,
validate Trello list, detect first/subsequent run) and the per-email
processing loop. Delegates every domain concern to a dedicated module.
Collects run statistics and logs a summary on completion. Startup network
checks (Trello list validation and IMAP auth) are wrapped in
`_retry_startup_check()` — up to 3 attempts with 30s backoff — so transient
DNS failures (common on Windows after boot) self-heal instead of crashing
the run. No external API calls of its own.

**`agent_shared.infra.config_loader`** — Loads and validates the two-tier
configuration: a global `.env.json` (secrets, shared across agents) and a
local `agent_config.json` (agent-specific tuning). Raises `ConfigError` on
any missing or invalid field so the agent fails fast at startup rather than
mid-run. Resolves the global config path from the `ENV_CONFIG_PATH`
environment variable with a sensible repo-local default. Exposes
`GlobalConfig` and `AgentConfig` dataclasses.

**`src/gmail_client.py`** — Gmail access via IMAP with an app password
(`imap.gmail.com:993`). `check_imap_auth` verifies credentials at startup.
`fetch_starred_emails` uses Gmail's `X-GM-LABELS` IMAP extension to exclude
already-processed messages at the server level, fetches RFC822 and parses
with Python's `email` stdlib, extracts the body (prefers `text/plain`; strips
HTML tags as fallback). `apply_label` uses `X-GM-MSGID` to find the message
in `[Gmail]/All Mail` and applies the label via `+X-GM-LABELS`. Stars are
intentionally not removed. Depends on: stdlib only (`imaplib`, `email`).

**`agent_shared.trello.client`** — Thin wrapper over the Trello REST API
using `requests`. Exposes `validate_list` (startup sanity check) and
`create_card` (always posts with `pos="top"` for correct ordering). Raises
`TrelloError` on any HTTP error or connection failure so the orchestrator
can record the failure and leave the email unprocessed for retry.
Depends on: `requests`.

**`src/card_builder.py`** — Pure functions for building card content from an
`EmailRecord`. `generate_card_name` calls the `LlmClientFn` callable and
falls back to `_clean_subject` on any failure. `build_card_description`
assembles the header line and full body, truncating with a notice if the
total exceeds Trello's 16,384-character limit. No external dependencies.

**`agent_shared.llm.client`** — Three-tier LLM wrapper. `health_check` pings
Ollama's `/api/tags` at startup. `generate_card_name` attempts:
(1) Anthropic API (`claude-haiku-4-5-20251001`) if `anthropic_api_key` is
configured; (2) Ollama `/api/generate` as local fallback; returning
`(name, "anthropic")`, `(name, "ollama")`, or `None` to signal fallback
to the subject line. Strips `<think>` tags from reasoning-model output.
Depends on: `anthropic` SDK (tier 1), stdlib `urllib.request` (tier 2).

**`agent_shared.infra.db`** — SQLite processing ledger using `sqlite3`
stdlib. Creates the `emails_processed` table on first run. Provides
`insert_record` (uses `INSERT OR REPLACE` so retries overwrite failed rows),
`check_duplicate` (only `status='success'` rows count), and
`get_last_run_time` (returns `None` on an empty table, signalling first run).
Depends on: stdlib only.

**`agent_shared.alerts.notifier`** — Shared alerting module used across
agents. `send_crash_alert` sends an email with the exception and traceback
when an unhandled error occurs. `send_failure_summary` sends a summary when
one or more emails fail processing. Both use Gmail SMTP (same credentials as
IMAP). Triggered by three events: (1) any unhandled exception after config is
loaded, (2) failed > 0 at end of processing loop, (3) startup failure after
config load (IMAP auth failure, Trello list not found). All calls are wrapped
in try/except — alert delivery failure is non-fatal.

**`src/models.py`** — Shared dataclasses: `EmailRecord` (raw email fields),
`CardPayload` (name + description + source), `ProcessingResult` (outcome,
card IDs, error). No logic; imported by all other modules.

---

## Data Flow

1. **Fetch** — `gmail_client.fetch_starred_emails()` returns a list of
   `EmailRecord` objects sorted oldest-first. The IMAP search is
   `FLAGGED NOT X-GM-LABELS "Agent/Added-To-Trello" [SINCE date]`, ensuring
   already-processed emails are excluded at the server level. Each record
   holds the X-GM-MSGID (as `gmail_message_id`), subject, sender, date,
   and decoded body.

2. **Name generation** — `card_builder.generate_card_name(email, llm_fn,
   template)` receives the `EmailRecord` and an `LlmClientFn` callable
   (created with `functools.partial` binding the `GlobalConfig` and
   `anthropic_api_key`). Returns
   `(name: str, source: "anthropic" | "ollama" | "fallback")`.

3. **Description** — `card_builder.build_card_description(email)` returns a
   formatted string ready to POST to Trello. Format: a bullet-point metadata
   line (`- See "..." email from ... on ...`), a `------` separator line, a
   blank line, then the email body. The gap between the bullet and separator
   is designed to accommodate future metadata bullets (e.g. duplicate-card
   links). Body is truncated with a notice if the total exceeds 16,384 chars.

4. **Card creation** — `trello_client.create_card(...)` returns
   `(card_id, card_url)` which flow into the `ProcessingResult`.

5. **Post-processing** — `gmail_client.apply_label` marks the email as
   processed in Gmail. The star is preserved so the user retains visibility
   of which emails triggered card creation. If label application fails,
   the DB records `status='failed_gmail_label'` and the Gmail label
   exclusion in the next run's query prevents duplicate processing because
   the label was not applied (the email will be re-queried and re-processed).

6. **Record** — `db.insert_record(db_path, email, card, result)` persists the
   full outcome. The `ProcessingResult.status` is one of:
   `success`, `failed_trello_create`, `failed_gmail_label`, `skipped_dedup`.

---

## Key Design Decisions

### IMAP with app password for Gmail access

**Context:** The agent needs to read emails and apply Gmail labels.
OAuth2 was originally used but Google's token refresh policy requires manual
browser re-consent every 7 days for unverified apps with restricted scopes —
making headless scheduling impractical.

**Options considered:** Gmail REST API with OAuth2; IMAP with app password.

**Chosen:** IMAP with a Gmail app password (`imap.gmail.com:993`). Gmail's
X-GM-LABELS and X-GM-MSGID IMAP extensions handle label operations.

**Tradeoffs:** App passwords never expire (until manually revoked or 2FA
disabled), enabling fully headless operation. No Google Cloud project or
OAuth consent screen required — just a one-time app password setup. Sacrifices:
app passwords require 2FA to be enabled on the Google account. The IMAP
X-GM-LABELS extension is Gmail-specific (not standard IMAP). Revisit if Google
deprecates IMAP or the X-GM extensions.

---

### Gmail label as the primary processed indicator (stars preserved)

**Context:** The original design removed the Gmail star after creating the
Trello card. This made the star the work queue, but created a partial-failure
risk: if the star removal failed after card creation, the next run would
create a duplicate card (caught only by the SQLite dedup check).

**Options considered:** Remove star after processing (original); keep star,
use Gmail label as processed indicator.

**Chosen:** Preserve the star; apply the `Agent/Added-To-Trello` label as the
sole processed indicator.

**Tradeoffs:** The Gmail query (`-label:Agent/Added-To-Trello`) excludes
already-processed emails at the API level, eliminating the duplicate-card
risk without relying on the SQLite dedup check as the first line of defense.
Users retain visibility of which emails triggered card creation (the star
remains). Sacrifices: the inbox star count grows over time rather than being
cleared by the agent. The SQLite dedup check is now a secondary safety net
rather than the primary guard. Revisit if users find the retained stars
confusing.

---

### Anthropic API as primary LLM, Ollama as local fallback

**Context:** Card name quality is important — a vague subject line like
"Re: Re: Follow up" produces a poor Trello card name. A local Ollama
instance is convenient but slow to load and occasionally unavailable.

**Options considered:** Ollama only; Anthropic API only; three-tier fallback
(Anthropic → Ollama → subject line).

**Chosen:** Three-tier fallback: Anthropic Haiku 4.5 as primary (when API key
configured), Ollama as local backup, subject-line cleanup as final fallback.

**Tradeoffs:** Anthropic API produces higher-quality card names and responds
faster than a locally-loaded model. The `anthropic_api_key` field in
`agent_config.json` is optional — omitting it silently degrades to
Ollama-only. Sacrifices: card name generation incurs API cost when Anthropic
is used. The Ollama fallback provides offline resilience. The subject-line
fallback ensures card creation never blocks on LLM availability. Revisit the
model choice (`claude-haiku-4-5-20251001`) when newer Haiku versions are
released.

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
completed email has its Trello card created and its label applied. Unprocessed
emails remain without the label and are picked up on the next run. The SQLite
dedup check provides a secondary safety net. Sacrifices: slower for large
batches. Configurable `processing_delay_seconds` makes rate limiting explicit.

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
