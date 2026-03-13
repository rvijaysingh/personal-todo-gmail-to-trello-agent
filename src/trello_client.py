"""Trello REST API client: validate list, create cards."""

import logging

import requests

logger = logging.getLogger(__name__)

TRELLO_API_BASE = "https://api.trello.com/1"


class TrelloError(Exception):
    """Raised when the Trello API returns an error response."""


def validate_list(list_id: str, board_id: str, api_key: str, token: str) -> bool:
    """Confirm that a Trello list exists on the given board.

    Used as a startup check before processing any emails. If the list is
    missing (archived, deleted, or wrong ID) the agent should exit rather
    than silently dropping cards.

    Args:
        list_id: The Trello list ID to look for.
        board_id: The Trello board ID that should contain the list.
        api_key: Trello API key.
        token: Trello API token.

    Returns:
        True if the list is found on the board, False otherwise.

    Raises:
        TrelloError: If the Trello API returns a non-2xx response.
    """
    url = f"{TRELLO_API_BASE}/boards/{board_id}/lists"
    params = {"key": api_key, "token": token}

    logger.debug("Validating Trello list %s on board %s", list_id, board_id)
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        raise TrelloError(
            f"Trello API error validating list: {exc.response.status_code} "
            f"{exc.response.text}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise TrelloError(f"Trello request failed during list validation: {exc}") from exc

    lists = response.json()
    found = any(lst.get("id") == list_id for lst in lists)
    logger.debug("List %s found on board: %s", list_id, found)
    return found


def create_card(
    list_id: str,
    name: str,
    description: str,
    api_key: str,
    token: str,
) -> tuple[str, str]:
    """Create a Trello card at the top of the specified list.

    Cards are created with pos='top' so that when emails are processed
    oldest-first, each new card pushes the previous one down. After a full
    batch the most recently sent email ends up at the top of the list.

    Args:
        list_id: Trello list ID where the card will be created.
        name: Card name (the actionable task name).
        description: Card description (email metadata + body).
        api_key: Trello API key.
        token: Trello API token.

    Returns:
        Tuple of (card_id, card_url).

    Raises:
        TrelloError: If the Trello API returns a non-2xx response or the
            response body is missing expected fields.
    """
    url = f"{TRELLO_API_BASE}/cards"
    payload = {
        "idList": list_id,
        "name": name,
        "desc": description,
        "pos": "top",
        "key": api_key,
        "token": token,
    }

    logger.debug("Creating Trello card on list %s: %r", list_id, name)
    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        raise TrelloError(
            f"Trello API error creating card: {exc.response.status_code} "
            f"{exc.response.text}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise TrelloError(f"Trello request failed during card creation: {exc}") from exc

    data = response.json()

    try:
        card_id: str = data["id"]
        card_url: str = data["url"]
    except KeyError as exc:
        raise TrelloError(
            f"Trello card response missing expected field {exc}: {data}"
        ) from exc

    logger.info("Created Trello card %s: %s", card_id, card_url)
    return card_id, card_url


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        from agent_shared.infra.config_loader import load_config

        gc, ac = load_config()
    except Exception as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    board_id = gc.trello_board_id
    list_id = ac.trello_list_id
    api_key = gc.trello_api_key
    token = gc.trello_api_token

    print(f"Validating list {list_id} on board {board_id}...")
    try:
        ok = validate_list(list_id, board_id, api_key, token)
        print(f"  List found: {ok}")
    except TrelloError as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        sys.exit(1)
