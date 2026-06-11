from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from app.config import HarborSettings, LlmSettings
from app.llm import complete_chat, llm_health


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://llm")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("failed", request=request, response=response)

    def json(self) -> dict:
        return self.payload


class LlmTests(unittest.TestCase):
    def test_health_rejects_missing_model(self) -> None:
        settings = HarborSettings(llm=LlmSettings(provider="ollama", base_url="http://llm", model="missing"))

        class Client:
            def __init__(self, **_kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def get(self, *_args, **_kwargs):
                return FakeResponse({"models": [{"name": "available"}]})

        with patch("app.llm.httpx.Client", Client):
            result = llm_health(settings)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "model_missing")

    def test_complete_chat_retries_transient_connection_error(self) -> None:
        settings = HarborSettings(
            llm=LlmSettings(provider="ollama", base_url="http://llm", model="model", retry_attempts=2)
        )
        calls = 0

        class Client:
            def __init__(self, **_kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def post(self, *_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise httpx.ConnectError("temporary")
                return FakeResponse({"message": {"content": "ok"}})

        with patch("app.llm.httpx.Client", Client), patch("app.llm.time.sleep"):
            response = complete_chat(settings, [{"role": "user", "content": "test"}])
        self.assertEqual(response["message"]["content"], "ok")
        self.assertEqual(calls, 2)
