from __future__ import annotations

from typing import Any

import httpx

from .config import HarborSettings, llm_api_key


def complete_chat(settings: HarborSettings, messages: list[dict[str, str]]) -> dict[str, Any]:
    if not settings.llm.base_url or not settings.llm.model:
        raise ValueError("LLM ist noch nicht konfiguriert.")
    base_url = settings.llm.base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    secret = llm_api_key(settings)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    payload = {
        "model": settings.llm.model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": settings.llm.max_tokens,
        "stream": False,
    }
    with httpx.Client(timeout=settings.llm.timeout_seconds) as client:
        response = client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
