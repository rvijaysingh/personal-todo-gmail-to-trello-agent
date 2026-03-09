"""Tests for src/llm_client.py."""

import json
import socket
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from src.config_loader import GlobalConfig
from src.llm_client import _clean_llm_response, generate_card_name, health_check

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SAMPLE_CONFIG = GlobalConfig(
    trello_api_key="key",
    trello_api_token="token",
    trello_board_id="board",
    ollama_host="http://localhost:11434",
    ollama_model="qwen3:8b",
    gmail_oauth_credentials_path="/creds.json",
    gmail_oauth_token_path="/token.json",
)

CARD_NAME_TEMPLATE = "Subject: {{subject}}\n\nBody:\n{{body_preview}}\n\nTask name:"


def _make_urlopen_mock(response_data: dict | str) -> MagicMock:
    """Return a mock that acts as the return value of urllib.request.urlopen.

    Supports use as a context manager (with urlopen(...) as resp).
    """
    if isinstance(response_data, dict):
        body = json.dumps(response_data).encode("utf-8")
    else:
        body = response_data.encode("utf-8") if isinstance(response_data, str) else response_data

    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


def test_health_check_success_returns_true() -> None:
    mock_resp = _make_urlopen_mock({"models": [{"name": "qwen3:8b"}]})
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = health_check(SAMPLE_CONFIG)
    assert result is True


def test_health_check_url_error_returns_false() -> None:
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        result = health_check(SAMPLE_CONFIG)
    assert result is False


def test_health_check_timeout_returns_false() -> None:
    with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        result = health_check(SAMPLE_CONFIG)
    assert result is False


def test_health_check_uses_correct_endpoint() -> None:
    mock_resp = _make_urlopen_mock({"models": []})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        health_check(SAMPLE_CONFIG)
    call_args = mock_urlopen.call_args
    url = call_args[0][0]  # first positional arg
    assert url == "http://localhost:11434/api/tags"


def test_health_check_strips_trailing_slash_from_host() -> None:
    config_with_slash = GlobalConfig(
        **{**SAMPLE_CONFIG.__dict__, "ollama_host": "http://localhost:11434/"}
    )
    mock_resp = _make_urlopen_mock({"models": []})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        health_check(config_with_slash)
    url = mock_urlopen.call_args[0][0]
    assert url == "http://localhost:11434/api/tags"


# ---------------------------------------------------------------------------
# generate_card_name — success cases
# ---------------------------------------------------------------------------


def test_generate_card_name_success_returns_name_and_source() -> None:
    mock_resp = _make_urlopen_mock(
        {"model": "qwen3:8b", "response": "Review quarterly board deck"}
    )
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = generate_card_name(
            subject="Q3 Board Deck",
            body_excerpt="Please review before Friday.",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
        )
    assert result is not None
    name, source = result
    assert name == "Review quarterly board deck"
    assert source == "llm"


def test_generate_card_name_strips_think_tags() -> None:
    raw = "<think>Let me reason about this...\nmultiline</think>\nReview Q3 board deck"
    mock_resp = _make_urlopen_mock({"response": raw})
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = generate_card_name(
            subject="Q3 Board Deck",
            body_excerpt="Review needed.",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
        )
    assert result is not None
    name, _ = result
    assert "<think>" not in name
    assert "Review Q3 board deck" in name


def test_generate_card_name_strips_code_fences() -> None:
    raw = "```\nFollow up on invoice payment\n```"
    mock_resp = _make_urlopen_mock({"response": raw})
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = generate_card_name(
            subject="Invoice",
            body_excerpt="Payment overdue.",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
        )
    assert result is not None
    name, _ = result
    assert "```" not in name
    assert "Follow up on invoice payment" in name


def test_generate_card_name_truncates_to_100_chars() -> None:
    long_response = "A" * 200
    mock_resp = _make_urlopen_mock({"response": long_response})
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = generate_card_name(
            subject="Long",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
        )
    assert result is not None
    name, _ = result
    assert len(name) <= 100


def test_generate_card_name_name_exactly_100_chars_not_truncated() -> None:
    exact_name = "B" * 100
    mock_resp = _make_urlopen_mock({"response": exact_name})
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = generate_card_name(
            subject="Exact",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
        )
    assert result is not None
    name, _ = result
    assert len(name) == 100


def test_generate_card_name_substitutes_template_placeholders() -> None:
    """Confirm {{subject}} and {{body_preview}} are substituted before sending."""
    mock_resp = _make_urlopen_mock({"response": "Review document"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        generate_card_name(
            subject="My Subject",
            body_excerpt="My body excerpt",
            prompt_template="Subject: {{subject}}\nBody: {{body_preview}}",
            config=SAMPLE_CONFIG,
        )
    # Inspect the request body sent to Ollama
    call_args = mock_urlopen.call_args
    req = call_args[0][0]  # urllib.request.Request object
    sent_payload = json.loads(req.data)
    assert "My Subject" in sent_payload["prompt"]
    assert "My body excerpt" in sent_payload["prompt"]
    assert "{{subject}}" not in sent_payload["prompt"]
    assert "{{body_preview}}" not in sent_payload["prompt"]


# ---------------------------------------------------------------------------
# generate_card_name — failure cases
# ---------------------------------------------------------------------------


def test_generate_card_name_url_error_returns_none() -> None:
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        result = generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
        )
    assert result is None


def test_generate_card_name_timeout_returns_none() -> None:
    with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        result = generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
        )
    assert result is None


def test_generate_card_name_malformed_json_returns_none() -> None:
    mock_resp = _make_urlopen_mock("{ not valid json }")
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
        )
    assert result is None


def test_generate_card_name_missing_response_key_returns_none() -> None:
    mock_resp = _make_urlopen_mock({"model": "qwen3:8b", "done": True})  # no 'response'
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
        )
    assert result is None


def test_generate_card_name_whitespace_only_response_returns_none() -> None:
    mock_resp = _make_urlopen_mock({"response": "   \n\t  "})
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
        )
    assert result is None


def test_generate_card_name_think_tags_only_response_returns_none() -> None:
    mock_resp = _make_urlopen_mock({"response": "<think>only thinking</think>"})
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
        )
    assert result is None


# ---------------------------------------------------------------------------
# _clean_llm_response unit tests
# ---------------------------------------------------------------------------


def test_clean_llm_response_strips_think_tags() -> None:
    raw = "<think>some reasoning\nmultiline</think>\nReview the document"
    assert _clean_llm_response(raw) == "Review the document"


def test_clean_llm_response_strips_multiline_think_tags() -> None:
    raw = "<think>\nLine 1\nLine 2\n</think>Follow up on invoice"
    assert _clean_llm_response(raw) == "Follow up on invoice"


def test_clean_llm_response_strips_code_fences() -> None:
    raw = "```\nReview the document\n```"
    assert _clean_llm_response(raw) == "Review the document"


def test_clean_llm_response_strips_code_fences_with_language() -> None:
    raw = "```text\nReview the document\n```"
    assert _clean_llm_response(raw) == "Review the document"


def test_clean_llm_response_strips_surrounding_whitespace() -> None:
    raw = "  \n  Review the document  \n  "
    assert _clean_llm_response(raw) == "Review the document"


def test_clean_llm_response_plain_text_unchanged() -> None:
    raw = "Follow up on invoice"
    assert _clean_llm_response(raw) == "Follow up on invoice"


def test_clean_llm_response_empty_string() -> None:
    assert _clean_llm_response("") == ""


def test_clean_llm_response_think_tags_only_becomes_empty() -> None:
    raw = "<think>just thinking</think>"
    assert _clean_llm_response(raw) == ""
