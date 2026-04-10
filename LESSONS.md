# Lessons Learned

Operational findings from real-world usage of this agent. Update this file
whenever you discover an API quirk, fix a subtle bug, or tune a parameter
that noticeably changed behaviour. Read this before making changes.

---

## Gmail API Quirks

*(Empty — add findings here as they are discovered in production.)*

---

## Trello API Quirks

*(Empty — add findings here as they are discovered in production.)*

---

## Ollama / LLM Notes

*(Empty — add prompt tuning notes and model behaviour observations here.)*

---

## OAuth2 Token Behaviour

*(Empty — add observations about token refresh, expiry patterns, and
re-authentication triggers here.)*

---

## Known Issues

*(Empty — add reproducible bugs or edge cases being tracked here.)*

---

## Transient Network Failures at Startup (2026-03)

### What happened
Three scheduled runs failed with DNS resolution errors shortly after the machine started:
- `socket.gaierror: [Errno 11004] getaddrinfo failed` — IMAP `imap.gmail.com:993`
- `NameResolutionError: Failed to resolve 'api.trello.com'` — Trello list validation

The agent was exiting immediately on the first failure and sending a crash alert email.
The network became available seconds later, but the run was already dead.

### Root cause
Windows Task Scheduler fires the agent before the network stack is fully ready
(common after boot or wake-from-sleep on Windows 10/11).

### Fix applied
`orchestrator._retry_startup_check()` retries both startup network checks up to
3 times with a 30-second backoff before giving up:
- Trello list validation: retries any `TrelloError` (connection errors)
- IMAP auth check: retries `OSError`, `socket.timeout`, `ConnectionRefusedError`
- `imaplib.IMAP4.error` (bad credentials) is **not** retried — exits immediately

### Constants
`_STARTUP_MAX_ATTEMPTS = 3`, `_STARTUP_BACKOFF_SECONDS = 30` in `orchestrator.py`.
Total worst-case wait before giving up: ~60 seconds (2 sleeps of 30s).

### Distinguishing transient vs. permanent IMAP errors
- `imaplib.IMAP4.error` = bad credentials = permanent → no retry
- `OSError` (superclass of `socket.gaierror`) = DNS/network failure = transient → retry
- `socket.timeout` = server unreachable/slow = transient → retry
- `ConnectionRefusedError` = port closed = transient → retry

---

## Gmail IMAP transient "Lookup failed" error

Gmail's IMAP server occasionally returns `imaplib.IMAP4.error` with the
message "Lookup failed <token>". This is a transient server-side error,
not a credential failure, and resolves on its own within minutes. It must
be retried rather than treated as a hard auth failure.

Fix: `_retry_startup_check` now accepts an optional `retryable_predicate`.
The IMAP auth check passes a predicate that returns `True` for non-IMAP4
exceptions (OSError, timeout, etc.) and for `imaplib.IMAP4.error` only when
"Lookup failed" appears in the message. All other `imaplib.IMAP4.error`
values (bad password, IMAP disabled) are re-raised immediately.

---

## Migration to agent_shared (2026-03)

### What moved to agent_shared
Four local modules were replaced by the `agent_shared` package:
- `src/config_loader.py` → `agent_shared.infra.config_loader`
- `src/db.py` → `agent_shared.infra.db`
- `src/trello_client.py` → `agent_shared.trello.client`
- `src/llm_client.py` → `agent_shared.llm.client`

### Import paths after migration
```python
from agent_shared.infra.config_loader import AgentConfig, ConfigError, GlobalConfig, load_config
from agent_shared.infra import db
from agent_shared.trello import client as trello_client
from agent_shared.trello.client import TrelloError
from agent_shared.llm import client as llm_client
```

### Mock targets in tests (critical)
`patch()` resolves names where they are looked up, not where they are defined.
After migration, mock targets are the agent_shared module paths:
- `"agent_shared.infra.db.init_db"` (not `"src.orchestrator.db.init_db"`)
- `"agent_shared.infra.db.insert_record"`
- `"agent_shared.trello.client.create_card"`
- `"agent_shared.trello.client.validate_list"`
- `"agent_shared.llm.client.health_check"`
- `"agent_shared.llm.client.anthropic_sdk.Anthropic"` — works because
  `agent_shared/llm/client.py` declares `anthropic_sdk = anthropic` at module
  level, making the alias patchable by name.

### Anthropic API key location change
The Anthropic API key moved from `config/agent_config.json` (`anthropic_api_key`)
to the global `.env.json` under `anthropic_api_keys["gmail-to-trello"]`.
The orchestrator reads it via `gc.anthropic_api_keys.get("gmail-to-trello", "")`.
The `anthropic_api_key` field was removed from `agent_config.json.example`.

### agent_shared interface notes
- `load_env_config` is the generic dict-returning loader in agent_shared.
  `load_config` is the agent-specific typed loader returning `(GlobalConfig, AgentConfig)`.
- `insert_record` in agent_shared.infra.db uses duck typing on email/card/result
  params — no dependency on `src.models` from the shared library side.
- The `TrelloError` class lives in `agent_shared.trello.client`, alongside
  the function-based `create_card` and `validate_list` wrappers.
