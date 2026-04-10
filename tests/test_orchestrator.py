"""Tests for src/orchestrator.py.

All external dependencies (Gmail IMAP, Trello API, Ollama, SQLite, filesystem)
are mocked — no live network calls or disk I/O are made.
"""

import imaplib
import socket
import sys
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest

from agent_shared.infra.config_loader import AgentConfig, ConfigError, GlobalConfig
from src.models import CardPayload, EmailRecord, ProcessingResult
from src.orchestrator import _days_since_last_run, _process_email, _retry_startup_check, _setup_rotating_logger, run
from agent_shared.trello.client import TrelloError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_gc() -> GlobalConfig:
    return GlobalConfig(
        trello_api_key="key",
        trello_api_token="token",
        trello_board_id="board_001",
        ollama_host="http://localhost:11434",
        ollama_model="qwen3:8b",
        gmail_sender="sender@gmail.com",
        gmail_password="app-password",
    )


def make_ac(**overrides) -> AgentConfig:
    defaults = dict(
        trello_list_id="list_001",
        first_run_lookback_days=7,
        gmail_processed_label="Agent/Added-To-Trello",
        trello_description_max_chars=16384,
        processing_delay_seconds=0,
        dedup_enabled=True,
        log_file="/tmp/test_agent.log",
        db_path="/tmp/test_emails.db",
        llm_timeout_seconds=120,
        max_emails_per_run=50,
        anthropic_api_key="test-key",
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def make_email(
    msg_id: str = "msg_001",
    subject: str = "Test Subject",
    sender: str = "Alice <alice@example.com>",
    email_date: str = "2026-03-08T10:00:00+00:00",
    body: str = "Test body.",
) -> EmailRecord:
    return EmailRecord(
        gmail_message_id=msg_id,
        subject=subject,
        sender=sender,
        email_date=email_date,
        body=body,
    )


def success_result(msg_id: str = "msg_001") -> ProcessingResult:
    return ProcessingResult(
        gmail_message_id=msg_id,
        status="success",
        trello_card_id="card_abc",
        trello_card_url="https://trello.com/c/abc",
    )


# Patch targets for _process_email dependencies
_GEN_NAME = "src.card_builder.generate_card_name"
_BUILD_DESC = "src.card_builder.build_card_description"
_CREATE_CARD = "agent_shared.trello.client.create_card"
_APPLY_LABEL = "src.gmail_client.apply_label"
_INSERT = "agent_shared.infra.db.insert_record"

_DEFAULT_CARD_NAME = ("Review the document", "anthropic")
_DEFAULT_CARD_URL = ("card_abc", "https://trello.com/c/abc")

# Alert patch targets
_SEND_CRASH_ALERT = "agent_shared.alerts.notifier.send_crash_alert"
_SEND_FAILURE_SUMMARY = "agent_shared.alerts.notifier.send_failure_summary"


def _run_process_email(
    email=None,
    gc=None,
    ac=None,
    gmail_sender: str = "sender@gmail.com",
    gmail_password: str = "app-password",
    llm_fn=None,
    prompt="template",
    *,
    gen_name_rv=_DEFAULT_CARD_NAME,
    build_desc_rv="Card description.",
    create_card_rv=_DEFAULT_CARD_URL,
    apply_label_side_effect=None,
    insert_side_effect=None,
):
    """Run _process_email with all external calls patched.

    Returns (result, mocks_dict).
    """
    if email is None:
        email = make_email()
    if gc is None:
        gc = make_gc()
    if ac is None:
        ac = make_ac()
    if llm_fn is None:
        llm_fn = lambda s, b, t: ("Fallback name", "fallback")

    with ExitStack() as stack:
        mocks = {
            "gen_name": stack.enter_context(
                patch(_GEN_NAME, return_value=gen_name_rv)
            ),
            "build_desc": stack.enter_context(
                patch(_BUILD_DESC, return_value=build_desc_rv)
            ),
            "create_card": stack.enter_context(
                patch(_CREATE_CARD, return_value=create_card_rv)
            ),
            "apply_label": stack.enter_context(
                patch(_APPLY_LABEL, side_effect=apply_label_side_effect)
            ),
            "insert": stack.enter_context(
                patch(_INSERT, side_effect=insert_side_effect)
            ),
        }
        result = _process_email(
            email, gc, ac, gmail_sender, gmail_password, llm_fn, prompt
        )

    return result, mocks


# ---------------------------------------------------------------------------
# _setup_rotating_logger
# ---------------------------------------------------------------------------


def test_setup_rotating_logger_adds_handler_to_root(tmp_path) -> None:
    import logging
    from logging.handlers import RotatingFileHandler

    log_file = str(tmp_path / "test.log")
    root = logging.getLogger()
    initial_count = len(root.handlers)

    _setup_rotating_logger(log_file)

    new_handlers = root.handlers[initial_count:]
    assert any(isinstance(h, RotatingFileHandler) for h in new_handlers)

    # Cleanup to avoid leaking handlers across tests
    for h in new_handlers:
        root.removeHandler(h)
        h.close()


def test_setup_rotating_logger_creates_parent_dirs(tmp_path) -> None:
    log_file = str(tmp_path / "subdir" / "nested" / "agent.log")
    import logging

    root = logging.getLogger()
    initial_count = len(root.handlers)

    _setup_rotating_logger(log_file)

    assert (tmp_path / "subdir" / "nested").exists()

    # Cleanup
    for h in root.handlers[initial_count:]:
        root.removeHandler(h)
        h.close()


# ---------------------------------------------------------------------------
# _process_email — success path
# ---------------------------------------------------------------------------


def test_process_email_success_returns_success_status() -> None:
    result, _ = _run_process_email()
    assert result.status == "success"


def test_process_email_success_returns_card_id() -> None:
    result, _ = _run_process_email()
    assert result.trello_card_id == "card_abc"


def test_process_email_success_returns_card_url() -> None:
    result, _ = _run_process_email()
    assert result.trello_card_url == "https://trello.com/c/abc"


def test_process_email_success_star_is_preserved() -> None:
    """unstar_email no longer exists — confirm gmail_client has no unstar attribute."""
    import src.gmail_client as gmail_mod
    assert not hasattr(gmail_mod, "unstar_email")


def test_process_email_success_calls_apply_label() -> None:
    email = make_email(msg_id="msg_xyz")
    gc = make_gc()
    ac = make_ac(gmail_processed_label="Agent/Added-To-Trello")
    _, mocks = _run_process_email(
        email=email,
        gc=gc,
        ac=ac,
        gmail_sender=gc.gmail_sender,
        gmail_password=gc.gmail_password,
    )
    mocks["apply_label"].assert_called_once_with(
        gc.gmail_sender, gc.gmail_password, "msg_xyz", "Agent/Added-To-Trello"
    )


def test_process_email_success_inserts_db_record() -> None:
    _, mocks = _run_process_email()
    mocks["insert"].assert_called_once()


def test_process_email_success_db_record_has_correct_status() -> None:
    _, mocks = _run_process_email()
    _db_path, _email, _card, result_arg = mocks["insert"].call_args[0]
    assert result_arg.status == "success"


def test_process_email_success_passes_max_chars_to_build_desc() -> None:
    ac = make_ac(trello_description_max_chars=5000)
    _, mocks = _run_process_email(ac=ac)
    _, kwargs = mocks["build_desc"].call_args
    assert kwargs.get("max_chars") == 5000 or mocks["build_desc"].call_args[0][1] == 5000


def test_process_email_success_passes_list_id_to_create_card() -> None:
    ac = make_ac(trello_list_id="my_list_007")
    _, mocks = _run_process_email(ac=ac)
    args = mocks["create_card"].call_args[0]
    assert args[0] == "my_list_007"


# ---------------------------------------------------------------------------
# _process_email — Trello failure
# ---------------------------------------------------------------------------


def test_process_email_trello_error_returns_failed_status() -> None:
    with ExitStack() as stack:
        stack.enter_context(patch(_GEN_NAME, return_value=_DEFAULT_CARD_NAME))
        stack.enter_context(patch(_BUILD_DESC, return_value="desc"))
        stack.enter_context(
            patch(_CREATE_CARD, side_effect=TrelloError("500 error"))
        )
        stack.enter_context(patch(_APPLY_LABEL))
        stack.enter_context(patch(_INSERT))
        result = _process_email(
            make_email(), make_gc(), make_ac(),
            "sender@gmail.com", "app-password",
            lambda s, b, t: None, "t"
        )

    assert result.status == "failed_trello_create"


def test_process_email_trello_error_inserts_db_record() -> None:
    with ExitStack() as stack:
        stack.enter_context(patch(_GEN_NAME, return_value=_DEFAULT_CARD_NAME))
        stack.enter_context(patch(_BUILD_DESC, return_value="desc"))
        stack.enter_context(
            patch(_CREATE_CARD, side_effect=TrelloError("API down"))
        )
        stack.enter_context(patch(_APPLY_LABEL))
        mock_insert = stack.enter_context(patch(_INSERT))
        _process_email(
            make_email(), make_gc(), make_ac(),
            "sender@gmail.com", "app-password",
            lambda s, b, t: None, "t"
        )

    mock_insert.assert_called_once()


def test_process_email_trello_error_error_message_in_result() -> None:
    with ExitStack() as stack:
        stack.enter_context(patch(_GEN_NAME, return_value=_DEFAULT_CARD_NAME))
        stack.enter_context(patch(_BUILD_DESC, return_value="desc"))
        stack.enter_context(
            patch(_CREATE_CARD, side_effect=TrelloError("Rate limited"))
        )
        stack.enter_context(patch(_APPLY_LABEL))
        stack.enter_context(patch(_INSERT))
        result = _process_email(
            make_email(), make_gc(), make_ac(),
            "sender@gmail.com", "app-password",
            lambda s, b, t: None, "t"
        )

    assert "Rate limited" in result.error_message


# ---------------------------------------------------------------------------
# _process_email — apply_label unexpected failure
# ---------------------------------------------------------------------------


def test_process_email_apply_label_failure_returns_failed_gmail_label() -> None:
    result, _ = _run_process_email(
        apply_label_side_effect=Exception("Unexpected crash")
    )
    assert result.status == "failed_gmail_label"


def test_process_email_apply_label_failure_preserves_card_url() -> None:
    result, _ = _run_process_email(
        apply_label_side_effect=Exception("Unexpected crash")
    )
    assert result.trello_card_url == "https://trello.com/c/abc"


# ---------------------------------------------------------------------------
# _process_email — card name source recorded
# ---------------------------------------------------------------------------


def test_process_email_records_anthropic_card_name_source() -> None:
    _, mocks = _run_process_email(gen_name_rv=("Task name", "anthropic"))
    _db_path, _email, card_arg, _result = mocks["insert"].call_args[0]
    assert card_arg.card_name_source == "anthropic"


def test_process_email_records_ollama_card_name_source() -> None:
    _, mocks = _run_process_email(gen_name_rv=("Task name", "ollama"))
    _db_path, _email, card_arg, _result = mocks["insert"].call_args[0]
    assert card_arg.card_name_source == "ollama"


def test_process_email_records_fallback_card_name_source() -> None:
    _, mocks = _run_process_email(gen_name_rv=("Task name", "fallback"))
    _db_path, _email, card_arg, _result = mocks["insert"].call_args[0]
    assert card_arg.card_name_source == "fallback"


# ---------------------------------------------------------------------------
# run() — shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def run_env():
    """Patch every external dependency of run() and yield a dict of mocks."""
    gc = make_gc()
    ac = make_ac()

    patches = {
        "load_config": patch(
            "src.orchestrator.load_config", return_value=(gc, ac)
        ),
        "setup_logger": patch("src.orchestrator._setup_rotating_logger"),
        "init_db": patch("agent_shared.infra.db.init_db"),
        "health_check": patch(
            "agent_shared.llm.client.health_check", return_value=True
        ),
        "validate_list": patch(
            "agent_shared.trello.client.validate_list", return_value=True
        ),
        "load_prompt": patch(
            "src.orchestrator._load_prompt_template", return_value="template"
        ),
        "check_imap_auth": patch(
            "src.gmail_client.check_imap_auth", return_value=None
        ),
        "get_last_run": patch(
            "agent_shared.infra.db.get_last_run_time", return_value=datetime(2026, 3, 1)
        ),
        "fetch_emails": patch(
            "src.gmail_client.fetch_starred_emails", return_value=[]
        ),
        "check_dup": patch("agent_shared.infra.db.check_duplicate", return_value=False),
        "process_email": patch(
            "src.orchestrator._process_email", return_value=success_result()
        ),
        "send_crash_alert": patch(_SEND_CRASH_ALERT),
        "send_failure_summary": patch(_SEND_FAILURE_SUMMARY),
        "sleep": patch("src.orchestrator.time.sleep"),
    }

    with ExitStack() as stack:
        mocks = {k: stack.enter_context(p) for k, p in patches.items()}
        mocks["gc"] = gc
        mocks["ac"] = ac
        yield mocks


# ---------------------------------------------------------------------------
# run() — startup sequence
# ---------------------------------------------------------------------------


def test_run_calls_load_config(run_env) -> None:
    run()
    run_env["load_config"].assert_called_once()


def test_run_calls_setup_logger(run_env) -> None:
    run()
    run_env["setup_logger"].assert_called_once()


def test_run_calls_init_db(run_env) -> None:
    run()
    run_env["init_db"].assert_called_once()


def test_run_calls_health_check(run_env) -> None:
    run()
    run_env["health_check"].assert_called_once()


def test_run_calls_validate_list(run_env) -> None:
    run()
    run_env["validate_list"].assert_called_once()


def test_run_calls_imap_auth_check(run_env) -> None:
    run()
    run_env["check_imap_auth"].assert_called_once_with(
        "sender@gmail.com", "app-password"
    )


def test_run_config_error_exits_with_code_1(run_env) -> None:
    run_env["load_config"].side_effect = ConfigError("Missing field")
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 1


def test_run_trello_list_not_found_exits_with_code_1(run_env) -> None:
    run_env["validate_list"].return_value = False
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 1


def test_run_trello_error_on_validate_exits_with_code_1(run_env) -> None:
    run_env["validate_list"].side_effect = TrelloError("Connection refused")
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 1


def test_run_imap_auth_failure_exits_with_code_1(run_env) -> None:
    run_env["check_imap_auth"].side_effect = imaplib.IMAP4.error("Invalid credentials")
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 1


def test_run_imap_server_unreachable_exits_with_code_1(run_env) -> None:
    run_env["check_imap_auth"].side_effect = socket.timeout("timed out")
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 1


def test_run_imap_connection_refused_exits_with_code_1(run_env) -> None:
    run_env["check_imap_auth"].side_effect = ConnectionRefusedError("refused")
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 1


def test_run_ollama_unavailable_does_not_exit(run_env) -> None:
    run_env["health_check"].return_value = False
    # Should complete without raising SystemExit
    run()


def test_run_ollama_health_check_called(run_env) -> None:
    run()
    run_env["health_check"].assert_called_once()


def test_run_anthropic_configured_ollama_healthy_does_not_exit(run_env) -> None:
    run_env["load_config"].return_value = (make_gc(), make_ac(anthropic_api_key="sk-ant-key"))
    run_env["health_check"].return_value = True
    run()  # should not raise


def test_run_no_anthropic_key_ollama_unavailable_does_not_exit(run_env) -> None:
    run_env["load_config"].return_value = (make_gc(), make_ac(anthropic_api_key=""))
    run_env["health_check"].return_value = False
    run()  # warning logged but should not raise or exit


# ---------------------------------------------------------------------------
# run() — first-run vs subsequent-run detection
# ---------------------------------------------------------------------------


def test_run_first_run_passes_lookback_days_to_fetch(run_env) -> None:
    run_env["get_last_run"].return_value = None  # first run
    run_env["ac"].first_run_lookback_days = 14
    run()
    run_env["fetch_emails"].assert_called_once()
    _, kwargs = run_env["fetch_emails"].call_args
    assert kwargs.get("max_age_days") == 14


def test_run_subsequent_run_passes_date_window_to_fetch(run_env) -> None:
    """Subsequent run should pass a positive max_age_days (days since last run + 2)."""
    run_env["get_last_run"].return_value = datetime(2026, 3, 1)  # subsequent run
    run()
    run_env["fetch_emails"].assert_called_once()
    _, kwargs = run_env["fetch_emails"].call_args
    max_age = kwargs.get("max_age_days")
    assert max_age is not None
    assert max_age >= 2  # at least the buffer


# ---------------------------------------------------------------------------
# run() — processing loop
# ---------------------------------------------------------------------------


def test_run_calls_process_email_for_each_email(run_env) -> None:
    emails = [make_email("msg_001"), make_email("msg_002"), make_email("msg_003")]
    run_env["fetch_emails"].return_value = emails
    run()
    assert run_env["process_email"].call_count == 3


def test_run_dedup_skip_when_duplicate_found(run_env) -> None:
    emails = [make_email("msg_001")]
    run_env["fetch_emails"].return_value = emails
    run_env["check_dup"].return_value = True  # already processed
    run()
    run_env["process_email"].assert_not_called()


def test_run_dedup_skip_does_not_call_process_email(run_env) -> None:
    emails = [make_email("msg_001"), make_email("msg_002")]
    run_env["fetch_emails"].return_value = emails
    # Only first email is a duplicate
    run_env["check_dup"].side_effect = lambda db_path, msg_id: msg_id == "msg_001"
    run()
    assert run_env["process_email"].call_count == 1


def test_run_dedup_disabled_skips_check(run_env) -> None:
    run_env["load_config"].return_value = (make_gc(), make_ac(dedup_enabled=False))
    emails = [make_email("msg_001")]
    run_env["fetch_emails"].return_value = emails
    run()
    run_env["check_dup"].assert_not_called()


def test_run_no_emails_does_not_call_process_email(run_env) -> None:
    run_env["fetch_emails"].return_value = []
    run()
    run_env["process_email"].assert_not_called()


def test_run_failed_email_does_not_stop_loop(run_env) -> None:
    emails = [make_email("msg_001"), make_email("msg_002")]
    run_env["fetch_emails"].return_value = emails
    # First email fails, second succeeds
    run_env["process_email"].side_effect = [
        ProcessingResult(gmail_message_id="msg_001", status="failed_trello_create"),
        success_result("msg_002"),
    ]
    run()  # should not raise
    assert run_env["process_email"].call_count == 2


def test_run_fetch_receives_processed_label(run_env) -> None:
    """The Gmail fetch should always receive the processed label for exclusion."""
    run()
    _, kwargs = run_env["fetch_emails"].call_args
    assert kwargs.get("processed_label") == "Agent/Added-To-Trello"


def test_run_fetch_receives_gmail_credentials(run_env) -> None:
    """fetch_starred_emails is called with gmail_sender and gmail_password."""
    run()
    args, kwargs = run_env["fetch_emails"].call_args
    assert args[0] == "sender@gmail.com"
    assert args[1] == "app-password"


def test_run_safety_cap_limits_emails_processed(run_env) -> None:
    """If fetch returns more than max_emails_per_run, only oldest N are processed."""
    ac = make_ac(max_emails_per_run=2)
    run_env["load_config"].return_value = (make_gc(), ac)
    emails = [make_email(f"msg_{i:03d}") for i in range(5)]
    run_env["fetch_emails"].return_value = emails
    run()
    assert run_env["process_email"].call_count == 2


def test_run_safety_cap_not_triggered_when_under_limit(run_env) -> None:
    """If fetch returns fewer than max_emails_per_run, all are processed."""
    ac = make_ac(max_emails_per_run=10)
    run_env["load_config"].return_value = (make_gc(), ac)
    emails = [make_email(f"msg_{i:03d}") for i in range(5)]
    run_env["fetch_emails"].return_value = emails
    run()
    assert run_env["process_email"].call_count == 5


# ---------------------------------------------------------------------------
# _days_since_last_run
# ---------------------------------------------------------------------------


def test_days_since_last_run_same_day_returns_zero() -> None:
    from datetime import timezone
    now = datetime.now(timezone.utc)
    result = _days_since_last_run(now)
    assert result == 0


def test_days_since_last_run_one_day_ago_returns_one() -> None:
    from datetime import timedelta, timezone
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    result = _days_since_last_run(yesterday)
    assert result == 1


def test_days_since_last_run_seven_days_returns_seven() -> None:
    from datetime import timedelta, timezone
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    result = _days_since_last_run(week_ago)
    assert result == 7


def test_days_since_last_run_naive_datetime_treated_as_utc() -> None:
    """Naive datetimes (SQLite output) should be treated as UTC without raising."""
    from datetime import timedelta
    naive_yesterday = datetime.utcnow() - timedelta(days=1)
    assert naive_yesterday.tzinfo is None
    result = _days_since_last_run(naive_yesterday)
    assert result == 1


# ---------------------------------------------------------------------------
# run() — alerting: crash on unhandled exception
# ---------------------------------------------------------------------------


def test_run_unhandled_exception_calls_send_crash_alert(run_env) -> None:
    """An unexpected exception inside run() triggers send_crash_alert."""
    run_env["init_db"].side_effect = RuntimeError("DB exploded")
    with pytest.raises(RuntimeError):
        run()
    run_env["send_crash_alert"].assert_called_once()
    call_kwargs = run_env["send_crash_alert"].call_args[1]
    assert call_kwargs["agent_name"] == "Gmail-to-Trello"
    assert isinstance(call_kwargs["error"], RuntimeError)


def test_run_unhandled_exception_reraises_after_alert(run_env) -> None:
    """After sending the crash alert, the original exception is re-raised."""
    run_env["init_db"].side_effect = RuntimeError("DB exploded")
    with pytest.raises(RuntimeError, match="DB exploded"):
        run()


def test_run_unhandled_exception_crash_alert_has_traceback(run_env) -> None:
    """Crash alert includes a non-empty traceback string."""
    run_env["init_db"].side_effect = RuntimeError("DB exploded")
    with pytest.raises(RuntimeError):
        run()
    call_kwargs = run_env["send_crash_alert"].call_args[1]
    assert "traceback_str" in call_kwargs
    assert len(call_kwargs["traceback_str"]) > 0


def test_run_unhandled_exception_crash_alert_has_credentials(run_env) -> None:
    """Crash alert receives the Gmail credentials from GlobalConfig."""
    run_env["init_db"].side_effect = RuntimeError("DB exploded")
    with pytest.raises(RuntimeError):
        run()
    call_kwargs = run_env["send_crash_alert"].call_args[1]
    assert call_kwargs["gmail_sender"] == "sender@gmail.com"
    assert call_kwargs["gmail_password"] == "app-password"


# ---------------------------------------------------------------------------
# run() — alerting: failure summary
# ---------------------------------------------------------------------------


def test_run_with_one_failure_calls_send_failure_summary(run_env) -> None:
    """When any emails fail, send_failure_summary is called after the loop."""
    emails = [make_email("msg_001"), make_email("msg_002"), make_email("msg_003")]
    run_env["fetch_emails"].return_value = emails
    run_env["process_email"].side_effect = [
        ProcessingResult(
            gmail_message_id="msg_001",
            status="failed_trello_create",
            error_message="Trello API down",
        ),
        success_result("msg_002"),
        success_result("msg_003"),
    ]
    run()
    run_env["send_failure_summary"].assert_called_once()
    call_kwargs = run_env["send_failure_summary"].call_args[1]
    assert call_kwargs["agent_name"] == "Gmail-to-Trello"
    assert call_kwargs["failed"] == 1
    assert call_kwargs["processed"] == 2


def test_run_with_zero_failures_does_not_call_send_failure_summary(run_env) -> None:
    """When all emails succeed, send_failure_summary is not called."""
    emails = [make_email("msg_001"), make_email("msg_002")]
    run_env["fetch_emails"].return_value = emails
    run_env["process_email"].return_value = success_result()
    run()
    run_env["send_failure_summary"].assert_not_called()


def test_run_failure_summary_includes_error_messages(run_env) -> None:
    """Error messages from failed results are passed to send_failure_summary."""
    emails = [make_email("msg_001")]
    run_env["fetch_emails"].return_value = emails
    run_env["process_email"].return_value = ProcessingResult(
        gmail_message_id="msg_001",
        status="failed_trello_create",
        error_message="Rate limit exceeded",
    )
    run()
    call_kwargs = run_env["send_failure_summary"].call_args[1]
    assert "Rate limit exceeded" in call_kwargs["errors"]


# ---------------------------------------------------------------------------
# run() — alerting: startup failures send crash alert before exit
# ---------------------------------------------------------------------------


def test_run_imap_auth_failure_sends_crash_alert(run_env) -> None:
    """IMAP auth failure triggers send_crash_alert before sys.exit(1)."""
    run_env["check_imap_auth"].side_effect = imaplib.IMAP4.error("Invalid credentials")
    with pytest.raises(SystemExit):
        run()
    run_env["send_crash_alert"].assert_called_once()
    call_kwargs = run_env["send_crash_alert"].call_args[1]
    assert call_kwargs["agent_name"] == "Gmail-to-Trello"


def test_run_trello_validation_failure_sends_crash_alert(run_env) -> None:
    """Trello list validation failure triggers send_crash_alert before sys.exit(1)."""
    run_env["validate_list"].side_effect = TrelloError("Connection refused")
    with pytest.raises(SystemExit):
        run()
    run_env["send_crash_alert"].assert_called_once()
    call_kwargs = run_env["send_crash_alert"].call_args[1]
    assert call_kwargs["agent_name"] == "Gmail-to-Trello"


def test_run_trello_list_not_found_sends_crash_alert(run_env) -> None:
    """Trello list not found on board triggers send_crash_alert before sys.exit(1)."""
    run_env["validate_list"].return_value = False
    with pytest.raises(SystemExit):
        run()
    run_env["send_crash_alert"].assert_called_once()
    call_kwargs = run_env["send_crash_alert"].call_args[1]
    assert call_kwargs["agent_name"] == "Gmail-to-Trello"


# ---------------------------------------------------------------------------
# run() — alerting: alert delivery failure is non-fatal
# ---------------------------------------------------------------------------


def test_run_crash_alert_failure_does_not_cause_secondary_crash(run_env) -> None:
    """If send_crash_alert itself raises, the original exception is still re-raised cleanly."""
    run_env["init_db"].side_effect = RuntimeError("DB exploded")
    run_env["send_crash_alert"].side_effect = Exception("SMTP failure")
    # Should raise the original RuntimeError, not the SMTP failure
    with pytest.raises(RuntimeError, match="DB exploded"):
        run()


def test_run_startup_crash_alert_failure_still_exits(run_env) -> None:
    """If send_crash_alert raises during a startup failure, sys.exit(1) is still called."""
    run_env["check_imap_auth"].side_effect = imaplib.IMAP4.error("Invalid credentials")
    run_env["send_crash_alert"].side_effect = Exception("SMTP failure")
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _retry_startup_check — unit tests
# ---------------------------------------------------------------------------


def test_retry_startup_check_succeeds_on_first_attempt() -> None:
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    with patch("src.orchestrator.time.sleep") as mock_sleep:
        result = _retry_startup_check(fn, max_attempts=3, backoff_seconds=5,
                                       retryable_exceptions=(OSError,), label="test")
    assert result == "ok"
    assert len(calls) == 1
    mock_sleep.assert_not_called()


def test_retry_startup_check_retries_on_retryable_exception() -> None:
    attempt = [0]

    def fn():
        attempt[0] += 1
        if attempt[0] < 3:
            raise OSError("DNS failure")
        return "ok"

    with patch("src.orchestrator.time.sleep"):
        result = _retry_startup_check(fn, max_attempts=3, backoff_seconds=5,
                                       retryable_exceptions=(OSError,), label="test")
    assert result == "ok"
    assert attempt[0] == 3


def test_retry_startup_check_sleeps_between_attempts() -> None:
    attempt = [0]

    def fn():
        attempt[0] += 1
        if attempt[0] < 3:
            raise OSError("DNS failure")
        return "ok"

    with patch("src.orchestrator.time.sleep") as mock_sleep:
        _retry_startup_check(fn, max_attempts=3, backoff_seconds=30,
                              retryable_exceptions=(OSError,), label="test")
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(30)


def test_retry_startup_check_raises_after_all_attempts_exhausted() -> None:
    def fn():
        raise OSError("DNS failure")

    with patch("src.orchestrator.time.sleep"):
        with pytest.raises(OSError, match="DNS failure"):
            _retry_startup_check(fn, max_attempts=3, backoff_seconds=5,
                                  retryable_exceptions=(OSError,), label="test")


def test_retry_startup_check_non_retryable_propagates_immediately() -> None:
    attempt = [0]

    def fn():
        attempt[0] += 1
        raise ValueError("permanent error")

    with patch("src.orchestrator.time.sleep") as mock_sleep:
        with pytest.raises(ValueError, match="permanent error"):
            _retry_startup_check(fn, max_attempts=3, backoff_seconds=5,
                                  retryable_exceptions=(OSError,), label="test")
    assert attempt[0] == 1
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# run() — retry behavior for transient startup errors
# ---------------------------------------------------------------------------


def test_run_trello_transient_error_retries_and_succeeds(run_env) -> None:
    """First validate_list call raises TrelloError, second succeeds — run proceeds normally."""
    run_env["validate_list"].side_effect = [TrelloError("DNS failure"), True]
    run()  # should not raise
    assert run_env["validate_list"].call_count == 2
    run_env["sleep"].assert_called_once()


def test_run_trello_exhausts_retries_sends_single_crash_alert_and_exits(run_env) -> None:
    """All validate_list attempts fail — exactly one crash alert sent then sys.exit(1)."""
    run_env["validate_list"].side_effect = TrelloError("DNS failure")
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 1
    assert run_env["validate_list"].call_count == 3
    run_env["send_crash_alert"].assert_called_once()


def test_run_imap_transient_network_error_retries_and_succeeds(run_env) -> None:
    """First check_imap_auth raises OSError, second succeeds — run proceeds normally."""
    run_env["check_imap_auth"].side_effect = [OSError("getaddrinfo failed"), None]
    run()  # should not raise
    assert run_env["check_imap_auth"].call_count == 2
    run_env["sleep"].assert_called_once()


def test_run_imap_bad_credentials_exits_immediately_without_retry(run_env) -> None:
    """imaplib.IMAP4.error (bad credentials) is not retried — exits on first attempt."""
    run_env["check_imap_auth"].side_effect = imaplib.IMAP4.error("Invalid credentials")
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 1
    assert run_env["check_imap_auth"].call_count == 1
    run_env["sleep"].assert_not_called()
