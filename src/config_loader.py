"""Loads global .env.json and agent_config.json, validates required fields."""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


@dataclass
class GlobalConfig:
    """Global secrets and shared settings loaded from .env.json."""

    trello_api_key: str
    trello_api_token: str
    trello_board_id: str
    ollama_host: str
    ollama_model: str
    gmail_oauth_credentials_path: str
    gmail_oauth_token_path: str


@dataclass
class AgentConfig:
    """Agent-specific settings loaded from config/agent_config.json."""

    trello_list_id: str
    first_run_lookback_days: int
    gmail_processed_label: str
    trello_description_max_chars: int
    processing_delay_seconds: int
    dedup_enabled: bool
    log_file: str
    db_path: str


def _repo_root() -> Path:
    """Return the project repo root (one level above src/)."""
    return Path(__file__).resolve().parent.parent


def _resolve_env_config_path() -> Path:
    """Resolve .env.json path via ENV_CONFIG_PATH env var or fall back to default."""
    env_var = os.environ.get("ENV_CONFIG_PATH")
    if env_var:
        return Path(env_var)
    return _repo_root() / "config" / ".env.json"


def _load_json(path: Path, label: str) -> dict:
    """Load and parse a JSON file.

    Args:
        path: Path to the JSON file.
        label: Human-readable name for error messages.

    Returns:
        Parsed JSON as a dict.

    Raises:
        ConfigError: If the file is missing or not valid JSON.
    """
    if not path.exists():
        raise ConfigError(f"{label} not found at {path}")
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{label} must be a JSON object, got {type(data).__name__}")
    return data


def _get_nested(data: dict, *keys: str, source: str) -> object:
    """Navigate a nested dict, raising ConfigError if any key is absent.

    Args:
        data: The dict to traverse.
        *keys: Sequence of keys forming the path (e.g. "trello", "api_key").
        source: Config file name for error messages.

    Returns:
        The value at the nested path.

    Raises:
        ConfigError: If any key in the path is missing.
    """
    current: object = data
    path_so_far: list[str] = []
    for key in keys:
        path_so_far.append(key)
        if not isinstance(current, dict) or key not in current:
            raise ConfigError(
                f"Missing required field '{'.'.join(path_so_far)}' in {source}"
            )
        current = current[key]  # type: ignore[index]
    return current


def _parse_global_config(data: dict, source: str) -> GlobalConfig:
    """Extract and validate GlobalConfig fields from nested .env.json data.

    Args:
        data: Parsed JSON dict from .env.json.
        source: File path string for error messages.

    Returns:
        Populated GlobalConfig dataclass.
    """

    def get(*keys: str) -> str:
        return str(_get_nested(data, *keys, source=source))

    return GlobalConfig(
        trello_api_key=get("trello", "api_key"),
        trello_api_token=get("trello", "token"),
        trello_board_id=get("trello", "personal_todo_board_id"),
        ollama_host=get("ollama_endpoint"),
        ollama_model=get("ollama_model"),
        gmail_oauth_credentials_path=get("gmail_oauth", "gmail_oauth_credentials_path"),
        gmail_oauth_token_path=get("gmail_oauth", "gmail_oauth_token_path"),
    )


def _parse_agent_config(data: dict, source: str) -> AgentConfig:
    """Extract and validate AgentConfig fields from agent_config.json data.

    Args:
        data: Parsed JSON dict from agent_config.json.
        source: File path string for error messages.

    Returns:
        Populated AgentConfig dataclass.
    """
    required = [
        "trello_list_id",
        "first_run_lookback_days",
        "gmail_processed_label",
        "trello_description_max_chars",
        "processing_delay_seconds",
        "dedup_enabled",
        "log_file",
        "db_path",
    ]
    for field in required:
        if field not in data:
            raise ConfigError(f"Missing required field '{field}' in {source}")

    return AgentConfig(
        trello_list_id=str(data["trello_list_id"]),
        first_run_lookback_days=int(data["first_run_lookback_days"]),
        gmail_processed_label=str(data["gmail_processed_label"]),
        trello_description_max_chars=int(data["trello_description_max_chars"]),
        processing_delay_seconds=int(data["processing_delay_seconds"]),
        dedup_enabled=bool(data["dedup_enabled"]),
        log_file=str(data["log_file"]),
        db_path=str(data["db_path"]),
    )


def load_config(
    env_config_path: str | None = None,
    agent_config_path: str | None = None,
) -> tuple[GlobalConfig, AgentConfig]:
    """Load and validate both configuration files.

    Args:
        env_config_path: Override path to global .env.json. If None, resolves
            via ENV_CONFIG_PATH env var or the default repo location.
        agent_config_path: Override path to agent_config.json. If None, uses
            config/agent_config.json relative to the repo root.

    Returns:
        Tuple of (GlobalConfig, AgentConfig).

    Raises:
        ConfigError: If a config file is missing, not valid JSON, or a required
            field is absent.
    """
    env_path = Path(env_config_path) if env_config_path else _resolve_env_config_path()
    agent_path = (
        Path(agent_config_path)
        if agent_config_path
        else _repo_root() / "config" / "agent_config.json"
    )

    logger.info("Loading global config from %s", env_path)
    env_data = _load_json(env_path, "Global config (.env.json)")
    global_config = _parse_global_config(env_data, str(env_path))

    logger.info("Loading agent config from %s", agent_path)
    agent_data = _load_json(agent_path, "Agent config (agent_config.json)")
    agent_config = _parse_agent_config(agent_data, str(agent_path))

    logger.info("Configuration loaded successfully")
    return global_config, agent_config


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    env_arg = sys.argv[1] if len(sys.argv) > 1 else None
    agent_arg = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        gc, ac = load_config(env_arg, agent_arg)
    except ConfigError as e:
        logger.error("Config error: %s", e)
        sys.exit(1)

    print(f"Trello board ID  : {gc.trello_board_id}")
    print(f"Ollama           : {gc.ollama_host} (model: {gc.ollama_model})")
    print(f"Gmail creds      : {gc.gmail_oauth_credentials_path}")
    print(f"Trello list ID   : {ac.trello_list_id}")
    print(f"Dedup enabled    : {ac.dedup_enabled}")
    print(f"DB path          : {ac.db_path}")
    print(f"Log file         : {ac.log_file}")
