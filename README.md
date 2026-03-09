# Gmail-to-Trello Agent

Monitors Gmail for starred emails, creates Trello cards with
LLM-generated actionable task names, then unstars the email and
applies a "processed" label.

## Setup

1. Copy `config/.env.json.example` to your global config path and
   fill in secrets.
2. Copy `config/agent_config.json.example` to
   `config/agent_config.json` and fill in your Trello list ID.
3. Run the Gmail OAuth flow: `python src/gmail_client.py --reauth`
4. Schedule via Windows Task Scheduler: `python src/orchestrator.py`

## Development

```
pytest tests/ -x
```

See `docs/` for architecture, risks, testing strategy, and config
reference. See `LESSONS.md` for operational findings.
