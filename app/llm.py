from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import HarborSettings, llm_api_key

RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


def _uses_ollama(base_url: str, provider: str = "auto") -> bool:
    if provider == "ollama":
        return True
    if provider == "openai":
        return False
    parsed = urlparse(base_url.rstrip("/"))
    return parsed.path.endswith("/api") or parsed.port == 11434 or "ollama" in parsed.netloc.lower()


def extract_chat_content(response: dict[str, Any]) -> str:
    message = response.get("message")
    if isinstance(message, dict):
        content = message.get("content", "")
        if isinstance(content, str):
            return content
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            choice_message = first.get("message")
            if isinstance(choice_message, dict):
                content = choice_message.get("content", "")
                if isinstance(content, str):
                    return content
            text = first.get("text", "")
            if isinstance(text, str):
                return text
    return ""


def _timeout(settings: HarborSettings) -> httpx.Timeout:
    return httpx.Timeout(
        settings.llm.timeout_seconds,
        connect=min(settings.llm.connect_timeout_seconds, settings.llm.timeout_seconds),
    )


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in RETRYABLE_STATUS_CODES


def _attempts(settings: HarborSettings) -> int:
    return max(1, min(5, settings.llm.retry_attempts))


def _backoff(attempt: int) -> None:
    time.sleep(min(2.0, 0.25 * (2**attempt)))


def _format_llm_error(exc: Exception, timeout_seconds: float) -> str:
    """Format LLM errors with actionable detail for the user."""
    import httpx
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"Request timed out after {timeout_seconds}s. "
            f"The LLM server did not respond in time. "
            f"Increase the timeout or check server load."
        )
    if isinstance(exc, httpx.ConnectError):
        return f"Connection failed. LLM server is unreachable: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            body = exc.response.json()
            msg = body.get("error", {}).get("message", "") or body.get("error", "") or body.get("message", "")
            if msg:
                return f"HTTP {status}: {msg}"
        except Exception:
            pass
        return f"HTTP {status} {exc.response.reason_phrase}. Server returned an error."
    if isinstance(exc, ValueError):
        return f"Configuration error: {exc}"
    return f"{type(exc).__name__}: {exc}"


def llm_health(settings: HarborSettings) -> dict[str, Any]:
    if not settings.llm.base_url or not settings.llm.model:
        return {"ok": False, "status": "unconfigured", "detail": "LLM is not configured."}
    base_url = settings.llm.base_url.rstrip("/")
    headers: dict[str, str] = {}
    secret = llm_api_key(settings)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    is_ollama = _uses_ollama(base_url, settings.llm.provider)
    endpoint = "/api/tags" if is_ollama else "/models"
    started = time.monotonic()
    try:
        with httpx.Client(timeout=_timeout(settings)) as client:
            response = client.get(f"{base_url}{endpoint}", headers=headers)
            response.raise_for_status()
            payload = response.json()
        if is_ollama:
            models = [str(item.get("name", "")) for item in payload.get("models", []) if isinstance(item, dict)]
        else:
            models = [str(item.get("id", "")) for item in payload.get("data", []) if isinstance(item, dict)]
        model_available = not models or settings.llm.model in models
        return {
            "ok": model_available,
            "status": "connected" if model_available else "model_missing",
            "model": settings.llm.model,
            "models": models,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "detail": "LLM reachable." if model_available else "Configured model is not available.",
        }
    except Exception as exc:
        timeout_val = settings.llm.timeout_seconds
        detail = _format_llm_error(exc, timeout_val)
        return {
            "ok": False,
            "status": "error",
            "model": settings.llm.model,
            "models": [],
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "detail": detail,
            "error_type": type(exc).__name__,
        }


def complete_chat(settings: HarborSettings, messages: list[dict[str, str]]) -> dict[str, Any]:
    if not settings.llm.base_url or not settings.llm.model:
        raise ValueError("LLM is not configured yet.")
    base_url = settings.llm.base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    secret = llm_api_key(settings)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    is_ollama = _uses_ollama(base_url, settings.llm.provider)
    payload: dict[str, Any]
    if is_ollama:
        payload = {
            "model": settings.llm.model,
            "messages": messages,
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0.2,
                "num_predict": settings.llm.max_tokens,
            },
        }
        endpoint = "/api/chat"
    else:
        payload = {
            "model": settings.llm.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": settings.llm.max_tokens,
            "stream": False,
        }
        endpoint = "/chat/completions"
    last_error: Exception | None = None
    with httpx.Client(timeout=_timeout(settings)) as client:
        for attempt in range(_attempts(settings)):
            try:
                response = client.post(f"{base_url}{endpoint}", headers=headers, json=payload)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= _attempts(settings) or not _retryable(exc):
                    raise
                _backoff(attempt)
    raise RuntimeError(_format_llm_error(last_error, _timeout(settings).read))


def stream_chat(settings: HarborSettings, messages: list[dict[str, str]]) -> Iterator[str]:
    if not settings.llm.base_url or not settings.llm.model:
        raise ValueError("LLM is not configured yet.")
    base_url = settings.llm.base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    secret = llm_api_key(settings)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    is_ollama = _uses_ollama(base_url, settings.llm.provider)
    payload: dict[str, Any] = {
        "model": settings.llm.model,
        "messages": messages,
        "stream": True,
    }
    if is_ollama:
        payload["keep_alive"] = "30m"
        payload["options"] = {"temperature": 0.2, "num_predict": settings.llm.max_tokens}
        endpoint = "/api/chat"
    else:
        payload.update({"temperature": 0.2, "max_tokens": settings.llm.max_tokens})
        endpoint = "/chat/completions"
    with httpx.Client(timeout=_timeout(settings)) as client:
        for attempt in range(_attempts(settings)):
            yielded = False
            try:
                with client.stream("POST", f"{base_url}{endpoint}", headers=headers, json=payload) as response:
                    response.raise_for_status()
                    for raw_line in response.iter_lines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        if line == "[DONE]":
                            return
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if is_ollama:
                            content = chunk.get("message", {}).get("content", "")
                        else:
                            choices = chunk.get("choices", [])
                            content = choices[0].get("delta", {}).get("content", "") if choices else ""
                        if content:
                            yielded = True
                            yield str(content)
                return
            except Exception as exc:
                if yielded or attempt + 1 >= _attempts(settings) or not _retryable(exc):
                    raise RuntimeError(_format_llm_error(exc, _timeout(settings).read))
                _backoff(attempt)
