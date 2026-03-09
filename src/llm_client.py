"""Ollama API wrapper with health check and card name generation."""

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Optional

from src.config_loader import GlobalConfig

logger = logging.getLogger(__name__)

_HEALTH_TIMEOUT_SEC = 5
_DEFAULT_GENERATE_TIMEOUT_SEC = 120
_MAX_NAME_LENGTH = 100

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


def warmup(config: GlobalConfig, timeout: int = _DEFAULT_GENERATE_TIMEOUT_SEC) -> None:
    """Send a trivial generation request to force the model to load into memory.

    Call this once after a successful health_check, before the processing loop,
    so the first real email does not pay the model-load latency cost.
    Logs the elapsed time. Never raises — a failed warmup is logged as a warning
    and processing continues (generation calls will use the fallback on timeout).

    Args:
        config: Global config with ollama_host and ollama_model.
        timeout: Request timeout in seconds (should match llm_timeout_seconds).
    """
    logger.info("Warming up Ollama model %s ...", config.ollama_model)
    start = time.monotonic()

    url = f"{config.ollama_host.rstrip('/')}/api/generate"
    payload = {"model": config.ollama_model, "prompt": "respond with OK", "stream": False}
    encoded = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=encoded, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        elapsed = time.monotonic() - start
        logger.info("Ollama warmup complete in %.1f seconds", elapsed)
    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.warning(
            "Ollama warmup failed after %.1f seconds (%s): %s",
            elapsed,
            type(exc).__name__,
            exc,
        )


def generate_card_name(
    subject: str,
    body_excerpt: str,
    prompt_template: str,
    config: GlobalConfig,
    timeout: int = _DEFAULT_GENERATE_TIMEOUT_SEC,
) -> Optional[tuple[str, str]]:
    """Generate an actionable Trello card name from email content via Ollama.

    Substitutes {{subject}} and {{body_preview}} in the prompt template, sends
    the request to Ollama's /api/generate endpoint, and returns the cleaned
    response. Falls back gracefully on any failure.

    Args:
        subject: Email subject line.
        body_excerpt: First ~500 characters of the email body.
        prompt_template: The card_name.md template with {{subject}} and
            {{body_preview}} placeholders.
        config: Global config with ollama_host and ollama_model.
        timeout: Request timeout in seconds. Defaults to
            _DEFAULT_GENERATE_TIMEOUT_SEC (120s). Override via
            AgentConfig.llm_timeout_seconds bound with functools.partial.

    Returns:
        Tuple of (card_name, "llm") on success. None on any failure so the
        caller can fall back to the cleaned subject line.
    """
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

    logger.info("LLM generated card name: %r", name)
    return (name, "llm")


if __name__ == "__main__":
    import sys
    from pathlib import Path

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    try:
        from src.config_loader import load_config

        gc, _ = load_config()
    except Exception as exc:
        logger.error("Could not load config: %s", exc)
        sys.exit(1)

    print(f"Ollama host : {gc.ollama_host}")
    print(f"Ollama model: {gc.ollama_model}")

    ok = health_check(gc)
    print(f"Health check: {'OK' if ok else 'FAILED'}")

    if ok:
        template_path = Path(__file__).parent.parent / "prompts" / "card_name.md"
        template = template_path.read_text(encoding="utf-8")
        result = generate_card_name(
            subject="Re: Q3 Board Deck - Final Review",
            body_excerpt="Please review the attached deck before Friday's board meeting.",
            prompt_template=template,
            config=gc,
        )
        if result:
            name, source = result
            print(f"Generated ({source}): {name}")
        else:
            print("Card name generation failed (LLM unavailable or errored)")
