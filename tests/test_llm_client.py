"""Tests for src/llm_client.py."""

import json
import socket
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import anthropic as anthropic_sdk
import pytest

from agent_shared.infra.config_loader import GlobalConfig
from agent_shared.llm.client import (
    _anthropic_generate_card_name,
    _clean_llm_response,
    generate_card_name,
    health_check,
)

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

FAKE_ANTHROPIC_KEY = "sk-ant-test-key"


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


def _make_anthropic_response(text: str) -> MagicMock:
    """Build a mock Anthropic messages.create() response."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def _make_anthropic_client_mock(response_text: str) -> MagicMock:
    """Build a mock anthropic.Anthropic client that returns the given text."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_anthropic_response(response_text)
    return mock_client


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
# _anthropic_generate_card_name — success cases
# ---------------------------------------------------------------------------


def test_anthropic_generate_card_name_success_returns_name_and_source() -> None:
    with patch("agent_shared.llm.client.anthropic_sdk.Anthropic") as mock_cls:
        mock_cls.return_value = _make_anthropic_client_mock("Review quarterly board deck")
        result = _anthropic_generate_card_name(
            subject="Q3 Board Deck",
            body_excerpt="Please review before Friday.",
            prompt_template=CARD_NAME_TEMPLATE,
            api_key=FAKE_ANTHROPIC_KEY,
        )
    assert result is not None
    name, source = result
    assert name == "Review quarterly board deck"
    assert source == "anthropic"


def test_anthropic_generate_card_name_truncates_to_100_chars() -> None:
    long_text = "A" * 200
    with patch("agent_shared.llm.client.anthropic_sdk.Anthropic") as mock_cls:
        mock_cls.return_value = _make_anthropic_client_mock(long_text)
        result = _anthropic_generate_card_name(
            subject="Long",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            api_key=FAKE_ANTHROPIC_KEY,
        )
    assert result is not None
    name, _ = result
    assert len(name) <= 100


def test_anthropic_generate_card_name_uses_correct_model() -> None:
    with patch("agent_shared.llm.client.anthropic_sdk.Anthropic") as mock_cls:
        mock_client = _make_anthropic_client_mock("Task name")
        mock_cls.return_value = mock_client
        _anthropic_generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            api_key=FAKE_ANTHROPIC_KEY,
        )
    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["model"] == "claude-haiku-4-5-20251001"


def test_anthropic_generate_card_name_uses_api_key() -> None:
    with patch("agent_shared.llm.client.anthropic_sdk.Anthropic") as mock_cls:
        mock_cls.return_value = _make_anthropic_client_mock("Task name")
        _anthropic_generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            api_key="sk-ant-specific-key",
        )
    mock_cls.assert_called_once_with(api_key="sk-ant-specific-key")


def test_anthropic_generate_card_name_substitutes_template_placeholders() -> None:
    with patch("agent_shared.llm.client.anthropic_sdk.Anthropic") as mock_cls:
        mock_client = _make_anthropic_client_mock("Review document")
        mock_cls.return_value = mock_client
        _anthropic_generate_card_name(
            subject="My Subject",
            body_excerpt="My body excerpt",
            prompt_template="Subject: {{subject}}\nBody: {{body_preview}}",
            api_key=FAKE_ANTHROPIC_KEY,
        )
    call_kwargs = mock_client.messages.create.call_args[1]
    prompt_sent = call_kwargs["messages"][0]["content"]
    assert "My Subject" in prompt_sent
    assert "My body excerpt" in prompt_sent
    assert "{{subject}}" not in prompt_sent
    assert "{{body_preview}}" not in prompt_sent


def test_anthropic_generate_card_name_strips_think_tags() -> None:
    raw = "<think>reasoning</think>\nReview the document"
    with patch("agent_shared.llm.client.anthropic_sdk.Anthropic") as mock_cls:
        mock_cls.return_value = _make_anthropic_client_mock(raw)
        result = _anthropic_generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            api_key=FAKE_ANTHROPIC_KEY,
        )
    assert result is not None
    name, _ = result
    assert "<think>" not in name
    assert "Review the document" in name


# ---------------------------------------------------------------------------
# _anthropic_generate_card_name — failure cases
# ---------------------------------------------------------------------------


def test_anthropic_generate_card_name_auth_error_returns_none() -> None:
    with patch("agent_shared.llm.client.anthropic_sdk.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.side_effect = (
            anthropic_sdk.AuthenticationError(
                message="bad key",
                response=MagicMock(status_code=401),
                body={},
            )
        )
        result = _anthropic_generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            api_key="bad-key",
        )
    assert result is None


def test_anthropic_generate_card_name_rate_limit_returns_none() -> None:
    with patch("agent_shared.llm.client.anthropic_sdk.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.side_effect = (
            anthropic_sdk.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429),
                body={},
            )
        )
        result = _anthropic_generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            api_key=FAKE_ANTHROPIC_KEY,
        )
    assert result is None


def test_anthropic_generate_card_name_server_error_returns_none() -> None:
    with patch("agent_shared.llm.client.anthropic_sdk.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.side_effect = (
            anthropic_sdk.InternalServerError(
                message="server error",
                response=MagicMock(status_code=500),
                body={},
            )
        )
        result = _anthropic_generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            api_key=FAKE_ANTHROPIC_KEY,
        )
    assert result is None


def test_anthropic_generate_card_name_connection_error_returns_none() -> None:
    with patch("agent_shared.llm.client.anthropic_sdk.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.side_effect = (
            anthropic_sdk.APIConnectionError(request=MagicMock())
        )
        result = _anthropic_generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            api_key=FAKE_ANTHROPIC_KEY,
        )
    assert result is None


def test_anthropic_generate_card_name_empty_response_returns_none() -> None:
    with patch("agent_shared.llm.client.anthropic_sdk.Anthropic") as mock_cls:
        mock_cls.return_value = _make_anthropic_client_mock("   ")
        result = _anthropic_generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            api_key=FAKE_ANTHROPIC_KEY,
        )
    assert result is None


# ---------------------------------------------------------------------------
# generate_card_name — Ollama path (no Anthropic key)
# ---------------------------------------------------------------------------


def test_generate_card_name_ollama_success_returns_ollama_source() -> None:
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
    assert source == "ollama"


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
# generate_card_name — Ollama failure cases
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
# generate_card_name — three-tier fallback chain
# ---------------------------------------------------------------------------


def test_generate_card_name_anthropic_key_uses_anthropic_first() -> None:
    """With an API key configured, Anthropic should be tried first."""
    with patch("agent_shared.llm.client._anthropic_generate_card_name") as mock_anthropic:
        mock_anthropic.return_value = ("Anthropic name", "anthropic")
        result = generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
            anthropic_api_key=FAKE_ANTHROPIC_KEY,
        )
    mock_anthropic.assert_called_once()
    assert result == ("Anthropic name", "anthropic")


def test_generate_card_name_no_anthropic_key_skips_anthropic() -> None:
    """Without an API key, Anthropic should not be called."""
    with patch("agent_shared.llm.client._anthropic_generate_card_name") as mock_anthropic:
        mock_resp = _make_urlopen_mock({"response": "Ollama name"})
        with patch("urllib.request.urlopen", return_value=mock_resp):
            generate_card_name(
                subject="Test",
                body_excerpt="body",
                prompt_template=CARD_NAME_TEMPLATE,
                config=SAMPLE_CONFIG,
                anthropic_api_key="",
            )
    mock_anthropic.assert_not_called()


def test_generate_card_name_anthropic_fails_falls_back_to_ollama() -> None:
    """If Anthropic returns None, Ollama is tried next."""
    mock_resp = _make_urlopen_mock({"response": "Ollama name"})
    with patch("agent_shared.llm.client._anthropic_generate_card_name", return_value=None):
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = generate_card_name(
                subject="Test",
                body_excerpt="body",
                prompt_template=CARD_NAME_TEMPLATE,
                config=SAMPLE_CONFIG,
                anthropic_api_key=FAKE_ANTHROPIC_KEY,
            )
    assert result is not None
    name, source = result
    assert name == "Ollama name"
    assert source == "ollama"


def test_generate_card_name_anthropic_rate_limited_falls_back_to_ollama() -> None:
    """A 429 from Anthropic should fall through to Ollama."""
    with patch("agent_shared.llm.client.anthropic_sdk.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.side_effect = (
            anthropic_sdk.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429),
                body={},
            )
        )
        mock_resp = _make_urlopen_mock({"response": "Ollama fallback"})
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = generate_card_name(
                subject="Test",
                body_excerpt="body",
                prompt_template=CARD_NAME_TEMPLATE,
                config=SAMPLE_CONFIG,
                anthropic_api_key=FAKE_ANTHROPIC_KEY,
            )
    assert result is not None
    name, source = result
    assert name == "Ollama fallback"
    assert source == "ollama"


def test_generate_card_name_both_fail_returns_none() -> None:
    """If Anthropic and Ollama both fail, return None."""
    with patch("agent_shared.llm.client._anthropic_generate_card_name", return_value=None):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = generate_card_name(
                subject="Test",
                body_excerpt="body",
                prompt_template=CARD_NAME_TEMPLATE,
                config=SAMPLE_CONFIG,
                anthropic_api_key=FAKE_ANTHROPIC_KEY,
            )
    assert result is None


def test_generate_card_name_no_key_ollama_fails_returns_none() -> None:
    """Without a key and Ollama down, return None."""
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        result = generate_card_name(
            subject="Test",
            body_excerpt="body",
            prompt_template=CARD_NAME_TEMPLATE,
            config=SAMPLE_CONFIG,
            anthropic_api_key="",
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
