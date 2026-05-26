from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from .config import HarborSettings, llm_api_key


def _uses_ollama(base_url: str) -> bool:
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


def complete_chat(settings: HarborSettings, messages: list[dict[str, str]]) -> dict[str, Any]:
    if not settings.llm.base_url or not settings.llm.model:
        raise ValueError("LLM ist noch nicht konfiguriert.")
    base_url = settings.llm.base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    secret = llm_api_key(settings)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    is_ollama = _uses_ollama(base_url)
    payload: dict[str, Any]
    if is_ollama:
        payload = {
            "model": settings.llm.model,
            "messages": messages,
            "stream": False,
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
    with httpx.Client(timeout=settings.llm.timeout_seconds) as client:
        response = client.post(f"{base_url}{endpoint}", headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
