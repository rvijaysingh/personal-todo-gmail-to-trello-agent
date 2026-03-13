"""Tests for src/trello_client.py.

All Trello REST API calls are mocked — no live HTTP requests are made.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from agent_shared.trello.client import TrelloError, create_card, validate_list

FIXTURES = Path(__file__).parent / "fixtures"

API_KEY = "test-api-key"
API_TOKEN = "test-api-token"
BOARD_ID = "board_test_001"
LIST_ID = "list_backlog_001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_response(status_code: int = 200, body: str | dict = "") -> MagicMock:
    """Build a mock requests.Response."""
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = status_code
    if isinstance(body, dict):
        mock_resp.json.return_value = body
        mock_resp.text = json.dumps(body)
    else:
        mock_resp.text = body
        # Only pre-parse JSON for 2xx responses; error bodies are often plain text
        try:
            mock_resp.json.return_value = json.loads(body) if body else {}
        except json.JSONDecodeError:
            mock_resp.json.side_effect = requests.exceptions.JSONDecodeError(
                "not JSON", body, 0
            )

    if status_code >= 400:
        http_err = requests.exceptions.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_err
    else:
        mock_resp.raise_for_status.return_value = None

    return mock_resp


# ---------------------------------------------------------------------------
# validate_list
# ---------------------------------------------------------------------------


def test_validate_list_returns_true_when_list_found() -> None:
    lists_body = _load_fixture("trello_lists_response.json")
    mock_resp = make_response(200, lists_body)

    with patch("requests.get", return_value=mock_resp):
        result = validate_list(LIST_ID, BOARD_ID, API_KEY, API_TOKEN)

    assert result is True


def test_validate_list_returns_false_when_list_not_found() -> None:
    lists_body = _load_fixture("trello_lists_response.json")
    mock_resp = make_response(200, lists_body)

    with patch("requests.get", return_value=mock_resp):
        result = validate_list("list_nonexistent", BOARD_ID, API_KEY, API_TOKEN)

    assert result is False


def test_validate_list_raises_trello_error_on_401() -> None:
    mock_resp = make_response(401, "invalid key")

    with patch("requests.get", return_value=mock_resp):
        with pytest.raises(TrelloError, match="401"):
            validate_list(LIST_ID, BOARD_ID, API_KEY, API_TOKEN)


def test_validate_list_raises_trello_error_on_404() -> None:
    mock_resp = make_response(404, "board not found")

    with patch("requests.get", return_value=mock_resp):
        with pytest.raises(TrelloError):
            validate_list(LIST_ID, BOARD_ID, "bad-key", API_TOKEN)


def test_validate_list_raises_trello_error_on_connection_failure() -> None:
    with patch(
        "requests.get",
        side_effect=requests.exceptions.ConnectionError("Connection refused"),
    ):
        with pytest.raises(TrelloError, match="request failed"):
            validate_list(LIST_ID, BOARD_ID, API_KEY, API_TOKEN)


def test_validate_list_sends_to_correct_url() -> None:
    mock_resp = make_response(200, "[]")

    with patch("requests.get", return_value=mock_resp) as mock_get:
        validate_list(LIST_ID, BOARD_ID, API_KEY, API_TOKEN)

    url = mock_get.call_args[0][0]
    assert f"/boards/{BOARD_ID}/lists" in url


def test_validate_list_passes_auth_params() -> None:
    mock_resp = make_response(200, "[]")

    with patch("requests.get", return_value=mock_resp) as mock_get:
        validate_list(LIST_ID, BOARD_ID, API_KEY, API_TOKEN)

    params = mock_get.call_args[1]["params"]
    assert params["key"] == API_KEY
    assert params["token"] == API_TOKEN


# ---------------------------------------------------------------------------
# create_card
# ---------------------------------------------------------------------------


def test_create_card_success_returns_id_and_url() -> None:
    card_body = json.loads(_load_fixture("trello_card_response.json"))
    mock_resp = make_response(200, card_body)

    with patch("requests.post", return_value=mock_resp):
        card_id, card_url = create_card(
            LIST_ID, "Review Q3 board deck", "Card description", API_KEY, API_TOKEN
        )

    assert card_id == "card_abc123"
    assert card_url == "https://trello.com/c/abc123/review-q3-board-deck"


def test_create_card_sends_pos_top() -> None:
    """pos='top' is required for oldest-first ordering logic to work."""
    card_body = json.loads(_load_fixture("trello_card_response.json"))
    mock_resp = make_response(200, card_body)

    with patch("requests.post", return_value=mock_resp) as mock_post:
        create_card(LIST_ID, "Task name", "Description", API_KEY, API_TOKEN)

    sent_payload = mock_post.call_args[1]["json"]
    assert sent_payload["pos"] == "top"


def test_create_card_sends_correct_list_id() -> None:
    card_body = json.loads(_load_fixture("trello_card_response.json"))
    mock_resp = make_response(200, card_body)

    with patch("requests.post", return_value=mock_resp) as mock_post:
        create_card(LIST_ID, "Task name", "Description", API_KEY, API_TOKEN)

    sent_payload = mock_post.call_args[1]["json"]
    assert sent_payload["idList"] == LIST_ID


def test_create_card_sends_name_and_description() -> None:
    card_body = json.loads(_load_fixture("trello_card_response.json"))
    mock_resp = make_response(200, card_body)

    with patch("requests.post", return_value=mock_resp) as mock_post:
        create_card(LIST_ID, "My task name", "My description", API_KEY, API_TOKEN)

    sent_payload = mock_post.call_args[1]["json"]
    assert sent_payload["name"] == "My task name"
    assert sent_payload["desc"] == "My description"


def test_create_card_sends_auth_credentials() -> None:
    card_body = json.loads(_load_fixture("trello_card_response.json"))
    mock_resp = make_response(200, card_body)

    with patch("requests.post", return_value=mock_resp) as mock_post:
        create_card(LIST_ID, "Task", "Desc", API_KEY, API_TOKEN)

    sent_payload = mock_post.call_args[1]["json"]
    assert sent_payload["key"] == API_KEY
    assert sent_payload["token"] == API_TOKEN


def test_create_card_sends_to_correct_endpoint() -> None:
    card_body = json.loads(_load_fixture("trello_card_response.json"))
    mock_resp = make_response(200, card_body)

    with patch("requests.post", return_value=mock_resp) as mock_post:
        create_card(LIST_ID, "Task", "Desc", API_KEY, API_TOKEN)

    url = mock_post.call_args[0][0]
    assert url.endswith("/cards")
    assert "trello.com" in url


def test_create_card_raises_trello_error_on_400() -> None:
    mock_resp = make_response(400, "invalid value")

    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(TrelloError, match="400"):
            create_card(LIST_ID, "Task", "Desc", API_KEY, API_TOKEN)


def test_create_card_raises_trello_error_on_401() -> None:
    mock_resp = make_response(401, "unauthorized")

    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(TrelloError, match="401"):
            create_card(LIST_ID, "Task", "Desc", "bad-key", API_TOKEN)


def test_create_card_raises_trello_error_on_500() -> None:
    mock_resp = make_response(500, "internal server error")

    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(TrelloError, match="500"):
            create_card(LIST_ID, "Task", "Desc", API_KEY, API_TOKEN)


def test_create_card_raises_trello_error_on_connection_failure() -> None:
    with patch(
        "requests.post",
        side_effect=requests.exceptions.ConnectionError("Connection refused"),
    ):
        with pytest.raises(TrelloError, match="request failed"):
            create_card(LIST_ID, "Task", "Desc", API_KEY, API_TOKEN)


def test_create_card_raises_trello_error_when_response_missing_id() -> None:
    """If Trello omits expected fields, raise TrelloError rather than KeyError."""
    mock_resp = make_response(200, {"shortUrl": "https://trello.com/c/abc"})  # no 'id'

    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(TrelloError):
            create_card(LIST_ID, "Task", "Desc", API_KEY, API_TOKEN)


def test_create_card_pos_top_ordering_semantics() -> None:
    """Verify that creating cards with pos=top for oldest-first emails
    results in newest email at list top after a batch completes.

    This is a documentation test: it confirms the ordering contract
    described in the business rules section of CLAUDE.md.
    """
    # If we create 3 cards with pos=top in order: old, middle, new
    # Then Trello's list order (top to bottom) would be: new, middle, old
    # This matches "most recently sent email at top" requirement.
    #
    # We just verify pos='top' is always sent.
    card_body = json.loads(_load_fixture("trello_card_response.json"))
    mock_resp = make_response(200, card_body)

    call_positions = []
    with patch("requests.post", return_value=mock_resp) as mock_post:
        for name in ["Old email task", "Middle email task", "New email task"]:
            create_card(LIST_ID, name, "Desc", API_KEY, API_TOKEN)

    for call in mock_post.call_args_list:
        assert call[1]["json"]["pos"] == "top"
