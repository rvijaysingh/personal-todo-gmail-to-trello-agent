"""Tests for src/config_loader.py."""

import json
import os
from pathlib import Path

import pytest

from src.config_loader import (
    AgentConfig,
    ConfigError,
    GlobalConfig,
    load_config,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

VALID_ENV_CONFIG: dict = {
    "trello": {
        "api_key": "test-api-key",
        "token": "test-api-token",
        "personal_todo_board_id": "test-board-id",
    },
    "ollama_endpoint": "http://localhost:11434",
    "ollama_model": "qwen3:8b",
    "gmail_oauth": {
        "gmail_oauth_credentials_path": "/path/to/credentials.json",
        "gmail_oauth_token_path": "/path/to/token.json",
    },
}

VALID_AGENT_CONFIG: dict = {
    "trello_list_id": "test-list-id",
    "first_run_lookback_days": 7,
    "gmail_processed_label": "Agent/Added-To-Trello",
    "trello_description_max_chars": 16384,
    "processing_delay_seconds": 1,
    "dedup_enabled": True,
    "log_file": "logs/agent_run.log",
    "db_path": "data/emails_processed.db",
    "llm_timeout_seconds": 120,
    "max_emails_per_run": 50,
}


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_load_config_valid_returns_correct_types(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, VALID_ENV_CONFIG)
    write_json(agent_file, VALID_AGENT_CONFIG)

    gc, ac = load_config(str(env_file), str(agent_file))

    assert isinstance(gc, GlobalConfig)
    assert isinstance(ac, AgentConfig)


def test_load_config_valid_global_fields(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, VALID_ENV_CONFIG)
    write_json(agent_file, VALID_AGENT_CONFIG)

    gc, _ = load_config(str(env_file), str(agent_file))

    assert gc.trello_api_key == "test-api-key"
    assert gc.trello_api_token == "test-api-token"
    assert gc.trello_board_id == "test-board-id"
    assert gc.ollama_host == "http://localhost:11434"
    assert gc.ollama_model == "qwen3:8b"
    assert gc.gmail_oauth_credentials_path == "/path/to/credentials.json"
    assert gc.gmail_oauth_token_path == "/path/to/token.json"


def test_load_config_valid_agent_fields(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, VALID_ENV_CONFIG)
    write_json(agent_file, VALID_AGENT_CONFIG)

    _, ac = load_config(str(env_file), str(agent_file))

    assert ac.trello_list_id == "test-list-id"
    assert ac.first_run_lookback_days == 7
    assert ac.gmail_processed_label == "Agent/Added-To-Trello"
    assert ac.trello_description_max_chars == 16384
    assert ac.processing_delay_seconds == 1
    assert ac.dedup_enabled is True
    assert ac.log_file == "logs/agent_run.log"
    assert ac.db_path == "data/emails_processed.db"
    assert ac.llm_timeout_seconds == 120
    assert ac.max_emails_per_run == 50


# ---------------------------------------------------------------------------
# Missing files
# ---------------------------------------------------------------------------


def test_load_config_env_file_missing_raises(tmp_path: Path) -> None:
    agent_file = tmp_path / "agent_config.json"
    write_json(agent_file, VALID_AGENT_CONFIG)

    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nonexistent.json"), str(agent_file))


def test_load_config_agent_file_missing_raises(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.json"
    write_json(env_file, VALID_ENV_CONFIG)

    with pytest.raises(ConfigError, match="not found"):
        load_config(str(env_file), str(tmp_path / "nonexistent.json"))


# ---------------------------------------------------------------------------
# Invalid JSON
# ---------------------------------------------------------------------------


def test_load_config_env_invalid_json_raises(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.json"
    env_file.write_text("{ this is not json }", encoding="utf-8")
    agent_file = tmp_path / "agent_config.json"
    write_json(agent_file, VALID_AGENT_CONFIG)

    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(str(env_file), str(agent_file))


def test_load_config_agent_invalid_json_raises(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.json"
    write_json(env_file, VALID_ENV_CONFIG)
    agent_file = tmp_path / "agent_config.json"
    agent_file.write_text("not json at all", encoding="utf-8")

    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(str(env_file), str(agent_file))


# ---------------------------------------------------------------------------
# Missing top-level section in .env.json
# ---------------------------------------------------------------------------


def test_load_config_missing_trello_section_raises(tmp_path: Path) -> None:
    bad_env = {k: v for k, v in VALID_ENV_CONFIG.items() if k != "trello"}
    env_file = tmp_path / ".env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, bad_env)
    write_json(agent_file, VALID_AGENT_CONFIG)

    with pytest.raises(ConfigError, match="trello"):
        load_config(str(env_file), str(agent_file))


def test_load_config_missing_ollama_endpoint_raises(tmp_path: Path) -> None:
    bad_env = {k: v for k, v in VALID_ENV_CONFIG.items() if k != "ollama_endpoint"}
    env_file = tmp_path / ".env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, bad_env)
    write_json(agent_file, VALID_AGENT_CONFIG)

    with pytest.raises(ConfigError, match="ollama_endpoint"):
        load_config(str(env_file), str(agent_file))


def test_load_config_missing_gmail_oauth_section_raises(tmp_path: Path) -> None:
    bad_env = {k: v for k, v in VALID_ENV_CONFIG.items() if k != "gmail_oauth"}
    env_file = tmp_path / ".env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, bad_env)
    write_json(agent_file, VALID_AGENT_CONFIG)

    with pytest.raises(ConfigError, match="gmail_oauth"):
        load_config(str(env_file), str(agent_file))


# ---------------------------------------------------------------------------
# Missing nested keys within a section
# ---------------------------------------------------------------------------


def test_load_config_missing_trello_board_id_raises(tmp_path: Path) -> None:
    bad_trello = {"api_key": "k", "token": "t"}  # missing personal_todo_board_id
    bad_env = {**VALID_ENV_CONFIG, "trello": bad_trello}
    env_file = tmp_path / ".env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, bad_env)
    write_json(agent_file, VALID_AGENT_CONFIG)

    with pytest.raises(ConfigError, match="board_id"):
        load_config(str(env_file), str(agent_file))


def test_load_config_missing_ollama_model_raises(tmp_path: Path) -> None:
    bad_env = {k: v for k, v in VALID_ENV_CONFIG.items() if k != "ollama_model"}
    env_file = tmp_path / ".env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, bad_env)
    write_json(agent_file, VALID_AGENT_CONFIG)

    with pytest.raises(ConfigError, match="ollama_model"):
        load_config(str(env_file), str(agent_file))


def test_load_config_missing_credentials_path_raises(tmp_path: Path) -> None:
    bad_oauth = {"gmail_oauth_token_path": "/t.json"}  # missing gmail_oauth_credentials_path
    bad_env = {**VALID_ENV_CONFIG, "gmail_oauth": bad_oauth}
    env_file = tmp_path / ".env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, bad_env)
    write_json(agent_file, VALID_AGENT_CONFIG)

    with pytest.raises(ConfigError, match="credentials_path"):
        load_config(str(env_file), str(agent_file))


# ---------------------------------------------------------------------------
# Missing agent config fields
# ---------------------------------------------------------------------------


def test_load_config_missing_agent_trello_list_id_raises(tmp_path: Path) -> None:
    bad_agent = {k: v for k, v in VALID_AGENT_CONFIG.items() if k != "trello_list_id"}
    env_file = tmp_path / ".env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, VALID_ENV_CONFIG)
    write_json(agent_file, bad_agent)

    with pytest.raises(ConfigError, match="trello_list_id"):
        load_config(str(env_file), str(agent_file))


def test_load_config_missing_agent_db_path_raises(tmp_path: Path) -> None:
    bad_agent = {k: v for k, v in VALID_AGENT_CONFIG.items() if k != "db_path"}
    env_file = tmp_path / ".env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, VALID_ENV_CONFIG)
    write_json(agent_file, bad_agent)

    with pytest.raises(ConfigError, match="db_path"):
        load_config(str(env_file), str(agent_file))


# ---------------------------------------------------------------------------
# ENV_CONFIG_PATH env var
# ---------------------------------------------------------------------------


def test_load_config_env_var_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "custom_env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, VALID_ENV_CONFIG)
    write_json(agent_file, VALID_AGENT_CONFIG)

    monkeypatch.setenv("ENV_CONFIG_PATH", str(env_file))
    # Do not pass env_config_path — must pick it up from env var
    gc, _ = load_config(agent_config_path=str(agent_file))

    assert gc.trello_api_key == "test-api-key"


def test_load_config_explicit_param_overrides_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit env_config_path param takes precedence over ENV_CONFIG_PATH."""
    env_file = tmp_path / ".env.json"
    agent_file = tmp_path / "agent_config.json"
    write_json(env_file, VALID_ENV_CONFIG)
    write_json(agent_file, VALID_AGENT_CONFIG)

    # Point env var at a nonexistent file — the explicit param must win
    monkeypatch.setenv("ENV_CONFIG_PATH", str(tmp_path / "does_not_exist.json"))

    gc, _ = load_config(
        env_config_path=str(env_file), agent_config_path=str(agent_file)
    )
    assert gc.trello_board_id == "test-board-id"
