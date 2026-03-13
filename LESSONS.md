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
