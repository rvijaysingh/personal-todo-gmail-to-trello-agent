# Gmail-to-Trello Agent

## Required Reading
Always read LESSONS.md before making any changes to understand
known issues and working patterns.

## Purpose
An automated agent that monitors Gmail for starred emails, creates
corresponding Trello cards with an LLM-generated actionable task
name and full email details in the card description, then unstars
the email and applies a "processed" label. Runs on a schedule via
Windows Task Scheduler.

## Development Loop
- Test command: `pytest tests/ -x`
- After any code change, run the test command and fix all failures
  before considering the task complete. Do not ask the user to run
  code or paste errors.
- This agent touches two external APIs (Gmail and Trello). All tests
  must use mocked API responses, never live calls. Fixture data
  lives in tests/fixtures/.

## Architecture Constraints
- Runtime: Windows 10/11 server, Python 3.13
- LLM: Ollama running locally at http://localhost:11434
  (model name specified in global config, currently qwen3:8b).
  Used only for generating actionable card names from email content.
- Gmail: Google API Python client with OAuth2 for reading emails,
  removing stars, and applying labels. NOT IMAP.
- Trello: REST API (key + token auth) for card creation.
- No browser automation. All interactions are API-based.

## Documentation Structure

- docs/architecture.md -- System design: pipeline stages, module
  responsibilities, data flow, key design decisions. Read this
  first for system understanding.
- docs/risks.md -- Identified risks with likelihood, impact, and
  concrete mitigations. Review when adding new modules or debugging
  failures.
- docs/testing.md -- Test case table, fixture inventory, testing
  strategy. Read before writing or updating tests.
- docs/config.md -- Config file schemas, shared vs project-specific
  fields, example values. Reference for machine setup or new agents.
- LESSONS.md -- Operational findings from debugging: API quirks,
  OAuth2 token refresh behavior, Trello description limits, LLM
  prompt tuning notes.
All modules should be built defensively against the risks identified
in docs/risks.md. Run pytest before every commit. Test coverage
requirements are defined in docs/testing.md.

## Project Structure
gmail-to-trello-agent/
  CLAUDE.md
  README.md
  LESSONS.md
  .gitignore
  config/
    agent_config.json          # Agent-specific settings (gitignored, machine-local)
    agent_config.json.example  # Template with placeholder values (committed)
    .env.json.example          # Template showing required global config fields (committed)
  prompts/
    card_name.md               # LLM prompt for generating actionable task name
  src/
    __init__.py
    config_loader.py           # Loads global .env.json + agent_config.json
    gmail_client.py            # Gmail API: fetch starred, unstar, apply label
    trello_client.py           # Trello API: create card, check for duplicates
    card_builder.py            # Builds card name (LLM or fallback) and description
    llm_client.py              # Ollama API wrapper with health check
    db.py                      # SQLite: emails_processed table, dedup queries
    orchestrator.py            # Main entry point and pipeline coordinator
    models.py                  # Shared data structures (EmailRecord,
                               # CardPayload, ProcessingResult)
  tests/
    test_gmail_client.py
    test_trello_client.py
    test_card_builder.py
    test_orchestrator.py
    test_db.py
    test_config.py
    fixtures/                  # Mock API responses, sample emails
  docs/
    architecture.md
    risks.md
    testing.md
    config.md
  data/
    emails_processed.db        # SQLite processing ledger (gitignored, auto-created)
  logs/
    agent_run.log              # Rotating application log (gitignored, auto-created)

## Configuration Design
Two config sources plus LLM prompts:

1. Global .env.json (gitignored, machine-local): secrets and shared
   settings used across all agents.
   Path resolved from ENV_CONFIG_PATH environment variable, falling
   back to ../config/.env.json relative to repo root.
   Required fields for this agent:
   - trello_api_key
   - trello_api_token
   - trello_board_id (the Todo board: oNIV6Mcq)
   - ollama_host (default: http://localhost:11434)
   - ollama_model (default: qwen3:8b)
   - gmail_oauth_credentials_path (path to credentials.json)
   - gmail_oauth_token_path (path to token.json)

2. config/agent_config.json (gitignored, machine-local): agent-specific
   settings. config/agent_config.json.example is committed as a
   template showing the required schema with placeholder values.
   Fields:
   - trello_list_id: ID of the "Backlog to Triage (incl. Gmail)" list
   - first_run_lookback_days: default 7
   - gmail_processed_label: default "Agent/Added-To-Trello"
   - trello_description_max_chars: default 16384
   - processing_delay_seconds: delay between emails to respect rate
     limits (default: 1)
   - dedup_enabled: whether to check SQLite before creating cards
     (default: true)
   - log_file: path to agent_run.log (default: logs/agent_run.log)
   - db_path: path to SQLite DB (default: data/emails_processed.db)

3. prompts/ directory (committed): LLM prompt templates loaded at
   runtime with variable substitution.

## Business Rules

### Run Logic
1. On each run, query Gmail for all currently starred emails.
2. If this is the first run (no records in emails_processed.db),
   limit to starred emails received within the last
   first_run_lookback_days (default 7). This avoids processing
   the user's entire starred email history.
3. On subsequent runs, process all starred emails found. The star
   is the work queue -- if it is starred, it needs processing.

### Per-Email Processing Pipeline
For each starred email, in sequence (one at a time for crash safety):

Step 1 - Extract Email Data:
- Fetch full message via Gmail API.
- Extract: message ID, subject, sender (name + email), date,
  body (prefer plain text part; if unavailable, strip HTML tags
  from HTML part).
- If the email is a thread, extract only the most recent message
  body, not the full thread history.

Step 2 - Generate Card Name:
- Send subject + first 500 chars of body to Ollama/Qwen via the
  prompts/card_name.md template.
- The LLM should return a short (under 100 chars), actionable task
  name. Example: email subject "Re: Q3 Board Deck - Final Review"
  becomes "Review and approve Q3 board deck".
- If Ollama is unreachable or errors: fall back to the email subject
  line, cleaned up (strip "Re:", "Fwd:", etc., trim whitespace).
- Log whether the card name was LLM-generated or fallback.

Step 3 - Build Card Description:
- Line 1: See "[subject]" email from [sender] on [formatted date]
- Line 2: blank
- Line 3 onward: email body
- If total description exceeds trello_description_max_chars (16384):
  truncate the body and append on a new line:
  "[Email body truncated -- original exceeds Trello's 16,384
  character limit]"

Step 4 - Dedup Check (if enabled):
- Query emails_processed.db for this gmail_message_id with
  status = 'success'.
- If found, skip this email (log as "already processed, skipping").
- This is a safety net for cases where the star was not removed
  due to a partial failure on a previous run.

Step 5 - Create Trello Card:
- POST to Trello API: create card on the configured list with
  the generated name and built description.
- Capture the returned card ID and URL.

Step 6 - Post-Processing on Gmail:
- Remove the star from the email.
- Apply the "Agent/Added-To-Trello" label. If the label does not
  exist in Gmail, log an error but do not fail. The label must be
  pre-created by the user.

Step 7 - Record to Database:
- Insert a row into emails_processed with all metadata,
  card_name_source (llm or fallback), trello_card_id,
  trello_card_url, status, and timestamp.
- On any failure in Steps 5-6, record with the appropriate error
  status and error_message.

### Processing Order
- Process emails oldest first (by email date) so the Trello list
  reads chronologically top-to-bottom.

### Crash Safety
- Each email is fully processed (Trello card created, star removed,
  label applied, DB recorded) before moving to the next.
- If the agent crashes mid-batch, unprocessed emails remain starred
  and will be picked up on the next run.
- If the Trello card was created but the star removal failed, the
  dedup check on the next run prevents a duplicate card.

## Database Schema

### emails_processed table
```sql
CREATE TABLE IF NOT EXISTS emails_processed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT UNIQUE NOT NULL,
    subject TEXT,
    sender TEXT,
    email_date TEXT,
    generated_card_name TEXT,
    card_name_source TEXT CHECK(card_name_source IN ('llm', 'fallback')),
    trello_card_id TEXT,
    trello_card_url TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'success',
        'failed_trello_create',
        'failed_gmail_unstar',
        'failed_gmail_label',
        'skipped_dedup'
    )),
    error_message TEXT,
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

## Logging
- Application log: logs/agent_run.log
  - Python logging module, rotating file handler (5MB, keep 3)
  - Every run logs: startup, config loaded, Gmail query count,
    per-email processing steps, Trello card URLs created, errors,
    run summary (X processed, Y failed, Z skipped)
  - Log levels: INFO for normal flow, WARNING for fallbacks (LLM
    down, label missing), ERROR for failures (API errors, crashes)
- Database: data/emails_processed.db
  - Structured audit trail of every email processed
  - Queryable for reporting, dedup, and last-run detection

## Risks and Mitigations
These are cross-cutting risks that should influence how every module
is built. Claude Code should build defensively against these.

### Gmail OAuth Token Expiry (Likelihood: Medium)
The OAuth2 refresh token may expire or be revoked (e.g., password
change, security event, 6-month inactivity on the token).
- Mitigation: The gmail_client must handle token refresh
  automatically via the google-auth library's built-in flow.
- Mitigation: If refresh fails, log the specific error and exit
  with a clear message: "Gmail OAuth token expired, manual
  re-authentication required. Run: python src/gmail_client.py
  --reauth"
- Mitigation: Provide a --reauth CLI flag on gmail_client.py
  that triggers the full OAuth consent flow.

### Gmail API Rate Limits (Likelihood: Low)
Gmail API quota is generous (250 units/second for read operations)
but batch processing many starred emails could hit limits.
- Mitigation: Configurable processing_delay_seconds between emails.
- Mitigation: On 429 responses, implement exponential backoff with
  a maximum of 3 retries before recording the email as failed.

### Ollama Unavailable (Likelihood: Medium)
The Ollama service may not be running, may have crashed, or the
model may not be loaded.
- Mitigation: Card creation does NOT depend on Ollama. Only the
  card name generation uses it.
- Mitigation: If Ollama is unreachable, fall back to cleaned
  subject line. Log a warning but continue processing.
- Mitigation: Check Ollama connectivity once at startup before
  entering the processing loop. Log a warning if unavailable so
  the entire run's fallback status is visible at the top of the log.

### Trello API Failure (Likelihood: Low)
Trello API could be down, return errors, or reject the card
(e.g., description too long despite truncation, list archived).
- Mitigation: On Trello API failure, record the email in the DB
  with status 'failed_trello_create' and the error. Do NOT remove
  the star so the email is retried on the next run.
- Mitigation: Validate that the target list ID exists on startup.
  If the list is not found, exit immediately with a clear error.

### Duplicate Cards (Likelihood: Medium)
If the star removal fails after card creation, the next run would
re-process the same email.
- Mitigation: The dedup check queries emails_processed.db by
  gmail_message_id before creating a card.
- Mitigation: As a secondary check, the gmail_message_id is stored
  in the card description metadata line, allowing manual
  identification if needed.

### Email Body Encoding Issues (Likelihood: Medium)
Emails may contain unusual encodings, inline images, or heavily
nested HTML that fails to parse cleanly.
- Mitigation: Prefer the plain text MIME part when available.
- Mitigation: For HTML-only emails, use a simple tag stripper
  (not a full rendering engine). Accept that formatting will be
  imperfect in the Trello card.
- Mitigation: If body extraction fails entirely, create the card
  with only the metadata line and note "[Email body could not be
  extracted]" in the description.

### Large Batch on First Run (Likelihood: Low)
If the user has hundreds of starred emails, the first run could
take a long time and hit API limits.
- Mitigation: first_run_lookback_days limits the scope.
- Mitigation: Log progress every 10 emails ("Processed 10/85...")
  so the user can monitor.

## Future Scope (Do NOT Build Now)
The following are out of scope for the current build. Do not build
abstractions or frameworks for these. Just avoid hardcoding decisions
that would make them difficult later.

- Priority labeling: Add a Trello label to the card based on email
  content analysis (e.g., "urgent", "finance", "personal"). The
  card_builder module should have a clean interface that a future
  label_assigner module can extend.
- Smart list placement: Move the card to the appropriate list
  (Today, Tomorrow, This Week) based on urgency detected in the
  email. The orchestrator should not hardcode the target list --
  it already reads it from config, so a future version can
  determine it dynamically.
- Action plan generation: Use the LLM to generate a checklist or
  action summary in the card description. The card_builder already
  has the email content; a future version can add a second LLM call
  for this.
- Email thread processing: Currently extracts only the most recent
  message. Future versions could include thread context for better
  LLM summarization.
- Notification on completion: Send a summary email or Slack message
  after each run (X emails processed, Y cards created, Z failures).
  The orchestrator already collects run statistics; a notifier
  module can be added later.
- Alternative LLM providers: Model name and endpoint are in config.
  No additional abstraction needed.
