from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from app.config import ModuleConfig
from app.modules import discover_standard_mcp_module, execute_module, module_test, validation_errors_by_module


class FakeResponse:
    def __init__(self, payload: dict, *, status_code: int = 200, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = str(payload)
        self.is_success = 200 <= status_code < 300

    def raise_for_status(self) -> None:
        if not self.is_success:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeMcpClient:
    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[dict] = []

    def __enter__(self) -> FakeMcpClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, url: str, *, headers: dict | None = None, json: dict | None = None) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        method = (json or {}).get("method")
        if method == "initialize":
            return FakeResponse(
                {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "netbox-mcp"}}},
                headers={"mcp-session-id": "session-1"},
            )
        if method == "notifications/initialized":
            return FakeResponse({}, status_code=202, headers={"mcp-session-id": "session-1"})
        if method == "tools/list":
            return FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "tools": [
                            {"name": "get_objects"},
                            {"name": "get_object_by_id"},
                            {"name": "get_changelogs"},
                        ]
                    },
                },
                headers={"mcp-session-id": "session-1"},
            )
        if method == "tools/call":
            return FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {
                        "content": [
                            {"type": "text", "text": "ok"},
                        ]
                    },
                },
                headers={"mcp-session-id": "session-1"},
            )
        raise AssertionError(f"Unexpected MCP method: {method}")


class ModuleTests(unittest.TestCase):
    def test_validation_errors_by_module_detects_duplicate_ports(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            modules = [
                ModuleConfig(id="docs-a", type="docs", transport="local", path=first_dir, port=41000),
                ModuleConfig(id="docs-b", type="docs", transport="local", path=second_dir, port=41000),
            ]
            errors = validation_errors_by_module(modules)
        self.assertIn("Port-Konflikt", " ".join(errors["docs-a"]))
        self.assertIn("Port-Konflikt", " ".join(errors["docs-b"]))

    @patch("app.modules.update_module_runtime_state", lambda *args, **kwargs: {})
    @patch("app.modules.httpx.Client", FakeMcpClient)
    def test_discover_standard_mcp_module_lists_tools(self) -> None:
        module = ModuleConfig(
            id="netbox",
            type="mcp_http",
            provider="netbox-mcp-server",
            transport="remote",
            remote_protocol="mcp",
            base_url="http://127.0.0.1:8000/mcp",
        )
        payload = discover_standard_mcp_module(module)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["protocol"], "mcp")
        self.assertEqual(payload["tools"], ["get_changelogs", "get_object_by_id", "get_objects"])

    @patch("app.modules.update_module_runtime_state", lambda *args, **kwargs: {})
    @patch("app.modules.httpx.Client", FakeMcpClient)
    def test_execute_module_calls_mcp_tool(self) -> None:
        module = ModuleConfig(
            id="netbox",
            type="mcp_http",
            provider="netbox-mcp-server",
            transport="remote",
            remote_protocol="mcp",
            base_url="http://127.0.0.1:8000/mcp",
        )
        with patch("app.modules.find_module", return_value=module):
            payload = execute_module("netbox", "get_objects", {"object_type": "dcim.devices"})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tool"], "get_objects")
        self.assertEqual(payload["data"]["content"][0]["text"], "ok")

    @patch("app.modules.update_module_runtime_state", lambda *args, **kwargs: {})
    @patch("app.modules.httpx.Client", FakeMcpClient)
    def test_module_test_reports_connected_and_meaningful_for_mcp_discovery(self) -> None:
        module = ModuleConfig(
            id="netbox",
            type="mcp_http",
            provider="netbox-mcp-server",
            transport="remote",
            remote_protocol="mcp",
            base_url="http://127.0.0.1:8000/mcp",
            test_action="discover",
            test_expect_contains=["get_objects"],
        )
        with patch("app.modules.find_module", return_value=module):
            payload = module_test("netbox")
        self.assertTrue(payload["connected"])
        self.assertTrue(payload["meaningful_output"])
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
