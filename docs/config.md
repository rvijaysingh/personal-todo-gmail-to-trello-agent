# Configuration Reference

## Two-Tier Design

Configuration is split into two files so secrets are never committed and
agent-specific tuning can be changed without touching the credentials file.

```
Global .env.json                    Agent config
(gitignored, machine-local)         (config/agent_config.json, gitignored)
         |                                      |
         |-- trello API key/token               |-- which Trello list to use
         |-- Gmail IMAP credentials             |-- first-run lookback window
         |-- Ollama host/model                  |-- rate-limit delay
                                                |-- log and DB paths
```

**Path resolution for `.env.json`:**
1. If the `ENV_CONFIG_PATH` environment variable is set, use that path.
2. Otherwise, look for `config/.env.json` relative to the repo root.

This allows a single `.env.json` to be shared across multiple agent repos on
the same machine by setting `ENV_CONFIG_PATH` to an absolute path outside any
repo.

---

## Global `.env.json`

Gitignored; never committed. See `config/.env.json.example` for the
full template with placeholder values.

The file uses a mixed structure — `trello` is a nested object, while Gmail
credentials and Ollama settings are top-level keys:

```json
{
  "trello": {
    "api_key": "<your-trello-api-key>",
    "token":   "<your-trello-api-token>",
    "personal_todo_board_id": "<your-trello-board-id>"
  },
  "ollama_endpoint": "http://localhost:11434",
  "ollama_model": "qwen3:8b",
  "gmail_sender": "<your-gmail-address@gmail.com>",
  "gmail_password": "<your-gmail-app-password>",
  "anthropic_api_keys": {
    "gmail-to-trello": "<your-anthropic-api-key-or-omit-for-ollama-only>"
  }
}
```

| Field path | Type | Description | Example |
|-----------|------|-------------|---------|
| `trello.api_key` | string | Trello Power-Up API key from https://trello.com/power-ups/admin | `"a1b2c3d4..."` |
| `trello.token` | string | Trello user token generated from the API key page | `"xxxxxxxxxxxx..."` |
| `trello.personal_todo_board_id` | string | ID of the Trello board containing the target list (visible in board URL) | `"oNIV6Mcq"` |
| `ollama_endpoint` | string | Base URL of the local Ollama server | `"http://localhost:11434"` |
| `ollama_model` | string | Model name to use for card name generation | `"qwen3:8b"` |
| `gmail_sender` | string | Gmail address used to authenticate IMAP | `"you@gmail.com"` |
| `gmail_password` | string | Gmail app password (Google Account → Security → App passwords). NOT your regular Gmail password. | `"abcd efgh ijkl mnop"` |
| `anthropic_api_keys.gmail-to-trello` | string | *(optional)* Anthropic API key used by this agent for card name generation. When present, Anthropic Haiku 4.5 is used as the primary LLM; Ollama is the fallback. Omit the key entirely to use Ollama-only mode. | `"sk-ant-..."` |

All required fields are validated at startup. The agent exits with a message
naming the exact missing field path (e.g., `"Missing required field 'trello.api_key'"`).

---

## Agent Config (`config/agent_config.json`)

Machine-local, gitignored. See `config/agent_config.json.example` for the
full template. All fields are at the top level (flat JSON object).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `trello_list_id` | string | *(required)* | ID of the Trello list to create cards in. Find it via the Trello API (`GET /1/boards/{id}/lists`) or by exporting the board as JSON from the Trello menu. |
| `first_run_lookback_days` | integer | `7` | On the first run (empty DB), only process starred emails received within this many days. Prevents importing a user's entire starred email history. |
| `gmail_processed_label` | string | `"Agent/Added-To-Trello"` | Gmail label applied to emails after processing. Must be pre-created in Gmail Settings → Labels before the agent runs. |
| `trello_description_max_chars` | integer | `16384` | Maximum card description length. Bodies are truncated at this limit with a notice appended. Matches Trello's documented character limit. |
| `processing_delay_seconds` | integer | `1` | Seconds to sleep between emails. Respects Gmail API rate limits. Set to `0` only for low-volume scenarios or testing. |
| `dedup_enabled` | boolean | `true` | When `true`, check `emails_processed.db` by `gmail_message_id` before creating a card. Prevents duplicate cards when a previous run crashed after card creation but before star removal. |
| `log_file` | string | `"logs/agent_run.log"` | Path to the rotating application log file. Parent directories are created automatically. Rotating at 5 MB, keeping 3 backups. |
| `db_path` | string | `"data/emails_processed.db"` | Path to the SQLite processing ledger. Parent directories are created automatically. |

---

## Prompts Directory

`prompts/card_name.md` contains the LLM prompt template. It is committed
to source control because prompt wording directly affects card name quality
and should be reviewed and versioned alongside the code.

The template uses two placeholders substituted at runtime:
- `{{subject}}` — the email subject line
- `{{body_preview}}` — the first 500 characters of the email body

To tune card name quality, edit `prompts/card_name.md` and re-run the agent.
No code changes are required.

---

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `ENV_CONFIG_PATH` | Absolute path to the global `.env.json` file. Set this in Windows Task Scheduler's task environment or in a `.bat` launcher script to share one credentials file across multiple agent repos on the same machine. |
