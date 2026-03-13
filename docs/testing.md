# Testing

## How to Run

```bash
# Fail-fast (stop on first failure) — use during development
pytest tests/ -x

# Verbose output (shows each test name)
pytest tests/ -v

# Single module
pytest tests/test_orchestrator.py -v

# Single test by name pattern
pytest tests/ -k "test_process_email_trello_error"
```

All tests run in under 3 seconds. No network calls, no disk I/O outside of
`tmp_path` fixtures. Safe to run on any machine without credentials.

---

## Test Files

| File | What it covers |
|------|---------------|
| `test_config.py` | `agent_shared.infra.config_loader.load_config`: nested key extraction, `ENV_CONFIG_PATH` env var override, missing files, missing fields, malformed JSON, type coercion for `int`/`bool` fields |
| `test_db.py` | `agent_shared.infra.db.init_db`, `insert_record`, `check_duplicate`, `get_last_run_time`: table creation, `INSERT OR REPLACE` retry semantics, dedup only counts `status='success'` rows, `None` returned on empty table |
| `test_llm_client.py` | `agent_shared.llm.client.health_check`, `generate_card_name`: successful generation, `<think>` tag stripping, markdown code-fence stripping, name truncation at 100 chars, all failure modes (URL error, bad JSON, missing `response` key, empty response), Ollama timeout |
| `test_gmail_client.py` | `gmail_client.fetch_starred_emails`, `unstar_email`, `apply_label`, `_decode_base64url`, `_extract_body`, `_strip_html`: base64url padding, recursive MIME extraction (plain > multipart > html), HTML tag stripping, pagination, oldest-first sort, error-email skipping, label-not-found logging without raising |
| `test_trello_client.py` | `agent_shared.trello.client.validate_list`, `create_card`: `pos="top"` always sent, correct list ID, auth credentials, endpoint URL, HTTP 4xx/5xx errors wrapped as `TrelloError`, connection failure, missing `id` field in response |
| `test_card_builder.py` | `card_builder._clean_subject`, `_format_date`, `generate_card_name`, `build_card_description`: all `Re:`/`Fwd:`/`FW:` prefix variants (case-insensitive, chained), Windows-safe date formatting, LLM path and fallback path, description header format, truncation at custom `max_chars`, truncation notice exact text, body prefix preserved |
| `test_orchestrator.py` | `orchestrator._setup_rotating_logger`, `_process_email`, `run`: rotating handler added to root logger, success path, Trello failure (no unstar, DB record), unstar failure (card info preserved), apply_label failure, card name source recorded, first/subsequent run detection, dedup skip, dedup disabled, invalid Trello list exits, config error exits, Ollama down continues, failed email does not stop loop |

**Total: 220 tests**

---

## Fixture Inventory

All fixtures live in `tests/fixtures/`.

| File | Description |
|------|-------------|
| `ollama_generate_response.json` | Successful `/api/generate` response from Ollama: `model`, `response` (clean task name string), `done: true` |
| `ollama_tags_response.json` | Successful `/api/tags` health-check response: list of available models on the local Ollama instance |
| `trello_card_response.json` | Successful `POST /1/cards` response: card `id`, `name`, `desc`, `idList`, `pos`, `url`, `shortUrl` |
| `trello_lists_response.json` | Successful `GET /1/boards/{id}/lists` response: array of three list objects including the target `list_backlog_001` |
| `gmail_labels.json` | Successful `GET /gmail/v1/users/me/labels` response: system labels (INBOX, STARRED, SENT) plus user labels `Agent/Added-To-Trello` and `Agent/Failed` |

Gmail message and thread responses are constructed inline in
`test_gmail_client.py` using `MagicMock` helper factories rather than
separate JSON files, because their structure (nested MIME parts, base64
bodies) is easier to express as Python dicts and varies significantly
between test cases.

---

## Testing Principles

### All external APIs are mocked
No test makes a live network call. Gmail, Trello, and Ollama are mocked
using `unittest.mock.patch` or `MagicMock`. The `requests` library is
patched at `requests.get` / `requests.post`. The Gmail API service object is
a `MagicMock` with chained return values. Ollama's `urllib.request.urlopen`
is patched to return controlled byte responses.

### Crash safety verified by _process_email failure mode tests
`test_orchestrator.py` verifies each failure scenario in the per-email
pipeline: Trello error does not remove the star; unstar failure preserves the
card ID/URL in the DB record so the dedup check works on the next run; the
processing loop continues after a failure rather than aborting the batch.

### Dedup correctness tested at the DB layer
`test_db.py` verifies that only `status='success'` rows count for dedup.
A `failed_trello_create` row for the same `gmail_message_id` does **not**
prevent a retry on the next run. `INSERT OR REPLACE` behaviour is validated:
a retry run overwrites a failed row with a success row.

### Business rule tests are deterministic
`test_card_builder.py` uses no mocked LLMs — the `llm_client` callable is
replaced with simple lambdas (`llm_ok`, `llm_down`) that return fixed values.
This makes subject-cleaning, date-formatting, and truncation tests fast and
reproducible without any timing or network dependencies.

### Fixture data matches real API response shapes
Fixture JSON files are based on actual Trello and Ollama response schemas so
that tests catch field-name mismatches (e.g., `id` vs `shortUrl` for the
card URL) rather than masking them.
