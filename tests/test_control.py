from __future__ import annotations

import unittest
from unittest.mock import patch

from app.config import HarborSettings, LlmSettings, ModuleConfig
from app.control import _build_messages, _context_for_chat


class ControlChatContextTests(unittest.TestCase):
    def test_context_for_chat_ignores_irrelevant_netbox_requests(self) -> None:
        module = ModuleConfig(
            id="netbox",
            type="mcp_http",
            provider="netbox-mcp-server",
            transport="remote",
            remote_protocol="mcp",
            base_url="http://127.0.0.1:8000/mcp",
        )
        with patch("app.control.load_modules", return_value=[module]), patch(
            "app.control.execute_module",
            side_effect=AssertionError("NetBox should not be queried for unrelated chat messages."),
        ):
            snippets, used_modules = _context_for_chat("Schreibe mir ein Gedicht ueber Kaffee.", None)
        self.assertEqual(snippets, [])
        self.assertEqual(used_modules, [])

    def test_context_for_chat_includes_netbox_results(self) -> None:
        module = ModuleConfig(
            id="netbox",
            type="mcp_http",
            provider="netbox-mcp-server",
            transport="remote",
            remote_protocol="mcp",
            base_url="http://127.0.0.1:8000/mcp",
        )

        def fake_execute(module_id: str, action: str, payload: dict[str, object]) -> dict[str, object]:
            self.assertEqual(module_id, "netbox")
            self.assertEqual(action, "get_objects")
            self.assertEqual(payload["object_type"], "dcim.devices")
            self.assertEqual(payload["filters"], {"limit": 5, "q": "edge-sw01"})
            return {
                "ok": True,
                "data": {
                    "structuredContent": {
                        "data": {
                            "results": [
                                {"id": 7, "name": "edge-sw01", "status": {"value": "active"}},
                            ]
                        }
                    }
                },
                "tool": "get_objects",
            }

        with patch("app.control.load_modules", return_value=[module]), patch("app.control.execute_module", side_effect=fake_execute):
            snippets, used_modules = _context_for_chat("Zeige mir den NetBox Server edge-sw01", None)

        self.assertEqual(used_modules, ["netbox"])
        self.assertEqual(snippets[0]["kind"], "netbox")
        self.assertEqual(snippets[0]["object_type"], "dcim.devices")
        self.assertEqual(snippets[0]["results"][0]["name"], "edge-sw01")

    def test_context_for_chat_includes_netbox_note_when_no_match(self) -> None:
        module = ModuleConfig(
            id="netbox",
            type="mcp_http",
            provider="netbox-mcp-server",
            transport="remote",
            remote_protocol="mcp",
            base_url="http://127.0.0.1:8000/mcp",
        )
        with patch("app.control.load_modules", return_value=[module]), patch(
            "app.control.execute_module",
            return_value={"ok": True, "data": {"structuredContent": {"data": {"results": []}}}, "tool": "get_objects"},
        ):
            snippets, used_modules = _context_for_chat("Zeige mir den NetBox Server edge-sw01", None)
        self.assertEqual(used_modules, ["netbox"])
        self.assertEqual(snippets[0]["kind"], "netbox")
        self.assertEqual(snippets[0]["results"], [])
        self.assertIn("keine passenden Objekte", snippets[0]["note"])

    def test_context_for_chat_honors_explicit_module_selection(self) -> None:
        module = ModuleConfig(
            id="netbox-prod",
            type="mcp_http",
            provider="netbox-mcp-server",
            transport="remote",
            remote_protocol="mcp",
            base_url="http://127.0.0.1:8000/mcp",
        )
        with patch("app.control.load_modules", return_value=[module]), patch(
            "app.control.execute_module",
            return_value={"ok": True, "data": {"structuredContent": {"data": {"results": []}}}, "tool": "get_objects"},
        ):
            snippets, used_modules = _context_for_chat("Schreibe mir ein Gedicht ueber Kaffee.", ["netbox-prod"])
        self.assertEqual(used_modules, ["netbox-prod"])
        self.assertEqual(snippets[0]["module"], "netbox-prod")

    def test_context_for_chat_includes_openstack_results(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            provider="openstack-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )

        def fake_execute(module_id: str, action: str, payload: dict[str, object]) -> dict[str, object]:
            self.assertEqual(module_id, "openstack")
            self.assertEqual(action, "list_servers")
            self.assertEqual(payload["name"], "prod-api-01")
            return {
                "ok": True,
                "data": {
                    "structuredContent": {
                        "data": [
                            {"ID": "vm-1", "Name": "prod-api-01", "Status": "ACTIVE"},
                        ]
                    }
                },
            }

        with patch("app.control.load_modules", return_value=[module]), patch("app.control.execute_module", side_effect=fake_execute):
            snippets, used_modules = _context_for_chat("Zeige mir in OpenStack den Server prod-api-01", None)

        self.assertEqual(used_modules, ["openstack"])
        self.assertEqual(snippets[0]["kind"], "openstack")
        self.assertEqual(snippets[0]["tool"], "list_servers")
        self.assertEqual(snippets[0]["results"][0]["Name"], "prod-api-01")

    def test_build_messages_embeds_netbox_context(self) -> None:
        settings = HarborSettings(llm=LlmSettings(base_url="http://llm", model="test-model"))
        with patch(
            "app.control._context_for_chat",
            return_value=([{"module": "netbox", "kind": "netbox", "object_type": "dcim.devices", "results": [{"name": "edge-sw01"}]}], ["netbox"]),
        ):
            messages, used_modules = _build_messages(settings, "Zeige edge-sw01", None)
        self.assertEqual(used_modules, ["netbox"])
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Kontext aus lokalen Modulen:", messages[0]["content"])
        self.assertIn("edge-sw01", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
