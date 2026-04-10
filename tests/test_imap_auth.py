"""Integration-level tests for the startup IMAP authentication check.

Verifies that:
  - IMAP auth success → orchestrator proceeds to fetch emails
  - IMAP auth failure (bad credentials) → exit code 1, clear log message
  - IMAP server timeout → exit code 1
  - IMAP connection refused → exit code 1

These tests ensure that if IMAP auth ever breaks (app password revoked,
Gmail disables IMAP, network issue), the agent fails fast at startup with
an actionable error message rather than failing silently mid-processing.
"""

import imaplib
import socket
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.orchestrator import run
from agent_shared.infra.config_loader import AgentConfig, GlobalConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GMAIL_SENDER = "sender@gmail.com"
_GMAIL_PASSWORD = "app-password"


def make_gc() -> GlobalConfig:
    return GlobalConfig(
        trello_api_key="key",
        trello_api_token="token",
        trello_board_id="board_001",
        ollama_host="http://localhost:11434",
        ollama_model="qwen3:8b",
        gmail_sender=_GMAIL_SENDER,
        gmail_password=_GMAIL_PASSWORD,
    )


def make_ac() -> AgentConfig:
    return AgentConfig(
        trello_list_id="list_001",
        first_run_lookback_days=7,
        gmail_processed_label="Agent/Added-To-Trello",
        trello_description_max_chars=16384,
        processing_delay_seconds=0,
        dedup_enabled=True,
        log_file="/tmp/test_imap_auth.log",
        db_path="/tmp/test_imap_auth.db",
        llm_timeout_seconds=120,
        max_emails_per_run=50,
        anthropic_api_key="",
    )


@pytest.fixture
def base_patches():
    """Patch all external dependencies except IMAP auth, yielding a dict of mocks."""
    gc = make_gc()
    ac = make_ac()

    patches = {
        "load_config": patch(
            "src.orchestrator.load_config", return_value=(gc, ac)
        ),
        "setup_logger": patch("src.orchestrator._setup_rotating_logger"),
        "init_db": patch("agent_shared.infra.db.init_db"),
        "health_check": patch(
            "agent_shared.llm.client.health_check", return_value=False
        ),
        "validate_list": patch(
            "agent_shared.trello.client.validate_list", return_value=True
        ),
        "load_prompt": patch(
            "src.orchestrator._load_prompt_template", return_value="template"
        ),
        "get_last_run": patch(
            "agent_shared.infra.db.get_last_run_time",
            return_value=datetime(2026, 3, 1),
        ),
        "fetch_emails": patch(
            "src.gmail_client.fetch_starred_emails", return_value=[]
        ),
        "check_dup": patch(
            "agent_shared.infra.db.check_duplicate", return_value=False
        ),
    }

    with ExitStack() as stack:
        mocks = {k: stack.enter_context(p) for k, p in patches.items()}
        mocks["gc"] = gc
        mocks["ac"] = ac
        yield mocks


# ---------------------------------------------------------------------------
# IMAP auth check — direct function tests (gmail_client.check_imap_auth)
# ---------------------------------------------------------------------------


def test_check_imap_auth_success_returns_normally() -> None:
    """Successful IMAP login + logout should not raise."""
    from src.gmail_client import check_imap_auth

    mock_imap = MagicMock()
    mock_imap.login.return_value = ("OK", [b"user authenticated"])
    mock_imap.logout.return_value = ("BYE", [b"Logged out"])

    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        check_imap_auth(_GMAIL_SENDER, _GMAIL_PASSWORD)

    mock_imap.login.assert_called_once_with(_GMAIL_SENDER, _GMAIL_PASSWORD)
    mock_imap.logout.assert_called_once()


def test_check_imap_auth_bad_credentials_raises_imap_error() -> None:
    """IMAP4.error raised by login should propagate from check_imap_auth."""
    from src.gmail_client import check_imap_auth

    mock_imap = MagicMock()
    mock_imap.login.side_effect = imaplib.IMAP4.error("Invalid credentials")

    with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        with pytest.raises(imaplib.IMAP4.error, match="Invalid credentials"):
            check_imap_auth(_GMAIL_SENDER, "wrong-password")


def test_check_imap_auth_socket_timeout_raises() -> None:
    """socket.timeout raised by IMAP4_SSL constructor propagates."""
    from src.gmail_client import check_imap_auth

    with patch("imaplib.IMAP4_SSL", side_effect=socket.timeout("timed out")):
        with pytest.raises(socket.timeout):
            check_imap_auth(_GMAIL_SENDER, _GMAIL_PASSWORD)


def test_check_imap_auth_connection_refused_raises() -> None:
    """ConnectionRefusedError raised by IMAP4_SSL constructor propagates."""
    from src.gmail_client import check_imap_auth

    with patch(
        "imaplib.IMAP4_SSL", side_effect=ConnectionRefusedError("connection refused")
    ):
        with pytest.raises(ConnectionRefusedError):
            check_imap_auth(_GMAIL_SENDER, _GMAIL_PASSWORD)


# ---------------------------------------------------------------------------
# IMAP auth check — orchestrator integration
# ---------------------------------------------------------------------------


def test_run_imap_auth_success_proceeds_to_fetch(base_patches) -> None:
    """When IMAP auth succeeds, orchestrator proceeds past startup and calls fetch."""
    with patch("src.gmail_client.check_imap_auth", return_value=None):
        run()

    # fetch_starred_emails must have been called, confirming startup completed
    base_patches["fetch_emails"].assert_called_once()


def test_run_imap_auth_invalid_credentials_exits_code_1(base_patches) -> None:
    """imaplib.IMAP4.error during startup → orchestrator exits with code 1."""
    with patch(
        "src.gmail_client.check_imap_auth",
        side_effect=imaplib.IMAP4.error("Invalid credentials"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            run()
    assert exc_info.value.code == 1


def test_run_imap_auth_invalid_credentials_does_not_fetch(base_patches) -> None:
    """After IMAP auth failure, fetch_starred_emails must NOT be called."""
    with patch(
        "src.gmail_client.check_imap_auth",
        side_effect=imaplib.IMAP4.error("Invalid credentials"),
    ):
        with pytest.raises(SystemExit):
            run()
    base_patches["fetch_emails"].assert_not_called()


def test_run_imap_server_timeout_exits_code_1(base_patches) -> None:
    """socket.timeout during IMAP startup check → exit code 1."""
    with patch(
        "src.gmail_client.check_imap_auth",
        side_effect=socket.timeout("connection timed out"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            run()
    assert exc_info.value.code == 1


def test_run_imap_connection_refused_exits_code_1(base_patches) -> None:
    """ConnectionRefusedError during IMAP startup check → exit code 1."""
    with patch(
        "src.gmail_client.check_imap_auth",
        side_effect=ConnectionRefusedError("connection refused"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            run()
    assert exc_info.value.code == 1


def test_run_imap_os_error_exits_code_1(base_patches) -> None:
    """OSError (e.g. network unreachable) during IMAP startup check → exit code 1."""
    with patch(
        "src.gmail_client.check_imap_auth",
        side_effect=OSError("network unreachable"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            run()
    assert exc_info.value.code == 1
