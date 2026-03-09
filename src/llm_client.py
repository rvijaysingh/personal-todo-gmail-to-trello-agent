"""LLM client: Anthropic API (primary) and Ollama (fallback) for card name generation."""

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Optional

import anthropic as anthropic_sdk

from src.config_loader import GlobalConfig

logger = logging.getLogger(__name__)

_HEALTH_TIMEOUT_SEC = 5
_DEFAULT_GENERATE_TIMEOUT_SEC = 120
_MAX_NAME_LENGTH = 100
_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

# qwen3 reasoning mode wraps its internal monologue in <think> tags
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def health_check(config: GlobalConfig) -> bool:
    """Ping Ollama's /api/tags endpoint to verify the service is reachable.

    Args:
        config: Global config containing ollama_host.

    Returns:
        True if Ollama responded with HTTP 200, False on any error.
    """
    url = f"{config.ollama_host.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT_SEC) as resp:
            resp.read()
        logger.info("Ollama health check passed at %s", config.ollama_host)
        return True
    except Exception as exc:
        logger.warning("Ollama health check failed (%s): %s", type(exc).__name__, exc)
        return False


def _clean_llm_response(raw: str) -> str:
    """Strip thinking tags, markdown code fences, and leading/trailing whitespace.

    Args:
        raw: The raw string returned by the LLM.

    Returns:
        Cleaned string suitable for use as a card name.
    """
    # Remove <think>...</think> blocks produced by qwen3 reasoning mode
    text = _THINK_TAG_RE.sub("", raw)
    # Strip markdown code fences (``` with optional language tag)
    text = re.sub(r"```[^\n]*\n?", "", text)
    return text.strip()


def _anthropic_generate_card_name(
    subject: str,
    body_excerpt: str,
    prompt_template: str,
    api_key: str,
) -> Optional[tuple[str, str]]:
    """Generate a card name using the Anthropic API (Claude Haiku 4.5).

    Args:
        subject: Email subject line.
        body_excerpt: First ~500 characters of the email body.
        prompt_template: The card_name.md template with {{subject}} and
            {{body_preview}} placeholders.
        api_key: Anthropic API key.

    Returns:
        Tuple of (card_name, "anthropic") on success, None on any failure.
    """
    prompt = prompt_template.replace("{{subject}}", subject).replace(
        "{{body_preview}}", body_excerpt
    )
    logger.debug(
        "Sending Anthropic request: model=%s subject=%r", _ANTHROPIC_MODEL, subject
    )

    try:
        client = anthropic_sdk.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=_ANTHROPIC_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
    except anthropic_sdk.AuthenticationError as exc:
        logger.warning("Anthropic authentication failed (bad API key): %s", exc)
        return None
    except anthropic_sdk.RateLimitError as exc:
        logger.warning("Anthropic rate limit hit: %s", exc)
        return None
    except anthropic_sdk.APIStatusError as exc:
        logger.warning("Anthropic API error (%d): %s", exc.status_code, exc)
        return None
    except anthropic_sdk.APIConnectionError as exc:
        logger.warning("Anthropic connection error: %s", exc)
        return None
    except Exception as exc:
        logger.warning("Anthropic request failed (%s): %s", type(exc).__name__, exc)
        return None

    logger.debug("Anthropic raw response: %s", raw)

    name = _clean_llm_response(raw)
    if not name:
        logger.warning("Anthropic returned an empty response after cleaning")
        return None

    if len(name) > _MAX_NAME_LENGTH:
        name = name[:_MAX_NAME_LENGTH].rstrip()
        logger.debug("Card name truncated to %d chars", _MAX_NAME_LENGTH)

    logger.info("Anthropic generated card name: %r", name)
    return (name, "anthropic")


def generate_card_name(
    subject: str,
    body_excerpt: str,
    prompt_template: str,
    config: GlobalConfig,
    timeout: int = _DEFAULT_GENERATE_TIMEOUT_SEC,
    anthropic_api_key: str = "",
) -> Optional[tuple[str, str]]:
    """Generate an actionable Trello card name from email content.

    Three-tier fallback:
    1. Anthropic API (Haiku 4.5) if anthropic_api_key is configured.
    2. Ollama /api/generate if Anthropic is unavailable or unconfigured.
    3. Returns None — card_builder falls back to the cleaned subject line.

    Args:
        subject: Email subject line.
        body_excerpt: First ~500 characters of the email body.
        prompt_template: The card_name.md template with {{subject}} and
            {{body_preview}} placeholders.
        config: Global config with ollama_host and ollama_model.
        timeout: Ollama request timeout in seconds.
        anthropic_api_key: Anthropic API key. If empty, Anthropic is skipped.

    Returns:
        Tuple of (card_name, source) where source is "anthropic" or "ollama",
        or None on complete failure so the caller can fall back to the subject line.
    """
    # Tier 1: Anthropic API
    if anthropic_api_key:
        result = _anthropic_generate_card_name(
            subject, body_excerpt, prompt_template, anthropic_api_key
        )
        if result is not None:
            return result
        logger.warning("Anthropic failed — falling through to Ollama")

    # Tier 2: Ollama
    prompt = prompt_template.replace("{{subject}}", subject).replace(
        "{{body_preview}}", body_excerpt
    )
    url = f"{config.ollama_host.rstrip('/')}/api/generate"
    payload = {
        "model": config.ollama_model,
        "prompt": prompt,
        "stream": False,
    }
    encoded = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=encoded, headers={"Content-Type": "application/json"}
    )

    logger.debug(
        "Sending Ollama request: model=%s subject=%r", config.ollama_model, subject
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_body = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        logger.warning("Ollama request failed (URLError): %s", exc)
        return None
    except Exception as exc:
        logger.warning("Ollama request failed (%s): %s", type(exc).__name__, exc)
        return None

    logger.debug("Ollama raw response: %s", raw_body)

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Ollama response is not valid JSON: %s | raw: %s", exc, raw_body
        )
        return None

    if "response" not in data:
        logger.warning("Ollama response missing 'response' key: %s", data)
        return None

    name = _clean_llm_response(data["response"])
    if not name:
        logger.warning("Ollama returned an empty response after cleaning")
        return None

    if len(name) > _MAX_NAME_LENGTH:
        name = name[:_MAX_NAME_LENGTH].rstrip()
        logger.debug("Card name truncated to %d chars", _MAX_NAME_LENGTH)

    logger.info("Ollama generated card name: %r", name)
    return (name, "ollama")


if __name__ == "__main__":
    import sys
    from pathlib import Path

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    try:
        from src.config_loader import load_config

        gc, ac = load_config()
    except Exception as exc:
        logger.error("Could not load config: %s", exc)
        sys.exit(1)

    print(f"Anthropic key : {'configured' if ac.anthropic_api_key else 'not configured'}")
    print(f"Ollama host   : {gc.ollama_host}")
    print(f"Ollama model  : {gc.ollama_model}")

    ok = health_check(gc)
    print(f"Ollama health : {'OK' if ok else 'FAILED'}")

    template_path = Path(__file__).parent.parent / "prompts" / "card_name.md"
    template = template_path.read_text(encoding="utf-8")
    result = generate_card_name(
        subject="Re: Q3 Board Deck - Final Review",
        body_excerpt="Please review the attached deck before Friday's board meeting.",
        prompt_template=template,
        config=gc,
        anthropic_api_key=ac.anthropic_api_key,
    )
    if result:
        name, source = result
        print(f"Generated ({source}): {name}")
    else:
        print("Card name generation failed (all LLM providers unavailable)")
