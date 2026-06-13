from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.auth import current_user, require_metrics_access
from app.config import HarborSettings, HarborUser, LlmSettings, ModuleConfig
from app.control import OpenStackConfigureRequest, _build_messages, _context_for_chat, create_app
from app.modules import module_status


class FakeRequest:
    headers: dict[str, str] = {}
    client = None


class SecurityTests(unittest.TestCase):
    def test_auth_fails_closed_without_users(self) -> None:
        with patch("app.auth.load_users", return_value=[]):
            with self.assertRaises(HTTPException) as context:
                current_user(FakeRequest())
        self.assertEqual(context.exception.status_code, 503)

    def test_metrics_token_grants_access_without_admin_password(self) -> None:
        request = FakeRequest()
        request.headers = {"Authorization": "Bearer monitoring-secret"}
        with patch.dict("os.environ", {"HARBOR_METRICS_TOKEN": "monitoring-secret"}, clear=False):
            self.assertIsNone(require_metrics_access(request))

    def test_module_status_redacts_nested_secrets(self) -> None:
        module = ModuleConfig(
            id="remote",
            type="mcp_http",
            transport="remote",
            base_url="https://mcp.example/mcp",
            settings={
                "token": "secret-value",
                "nested": {"password": "hidden", "region": "eu"},
            },
        )
        with (
            patch("app.modules.load_modules", return_value=[module]),
            patch("app.modules._module_health", return_value=None),
            patch("app.modules.load_module_runtime_state", return_value={}),
        ):
            status = module_status(module)
        self.assertEqual(status["settings"]["token"], "***")
        self.assertEqual(status["settings"]["nested"]["password"], "***")
        self.assertEqual(status["settings"]["nested"]["region"], "eu")


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

    def test_context_for_chat_routes_openstack_storage_questions_to_statistics(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            provider="openstack-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )

        def fake_execute(module_id: str, action: str, payload: dict[str, object]) -> dict[str, object]:
            self.assertEqual(module_id, "openstack")
            self.assertEqual(action, "get_storage_statistics")
            self.assertEqual(payload, {})
            return {
                "ok": True,
                "data": {
                    "structuredContent": {
                        "data": {"quota": {"capacity_gib": {"used": 400, "limit": 1000, "percent": 40.0}}}
                    }
                },
            }

        with patch("app.control.load_modules", return_value=[module]), patch(
            "app.control.execute_module",
            side_effect=fake_execute,
        ):
            snippets, used_modules = _context_for_chat("Wie voll ist mein OpenStack Storage in Prozent?", None)

        self.assertEqual(used_modules, ["openstack"])
        self.assertEqual(snippets[0]["tool"], "get_storage_statistics")
        self.assertEqual(snippets[0]["results"][0]["quota"]["capacity_gib"]["percent"], 40.0)

    def test_context_for_chat_routes_netbox_field_questions_to_description(self) -> None:
        module = ModuleConfig(
            id="netbox",
            type="netbox_mcp",
            provider="netbox-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )

        def fake_execute(module_id: str, action: str, payload: dict[str, object]) -> dict[str, object]:
            self.assertEqual(module_id, "netbox")
            self.assertEqual(action, "describe_object_type")
            self.assertEqual(payload["object_type"], "dcim.devices")
            return {
                "ok": True,
                "data": {
                    "structuredContent": {
                        "data": {"object_type": "dcim.devices", "schema_fields": [{"path": "custom_fields.owner"}]}
                    }
                },
            }

        with patch("app.control.load_modules", return_value=[module]), patch(
            "app.control.execute_module",
            side_effect=fake_execute,
        ):
            snippets, used_modules = _context_for_chat("Welche Felder werden bei NetBox Devices erfasst?", None)

        self.assertEqual(used_modules, ["netbox"])
        self.assertEqual(snippets[0]["tool"], "describe_object_type")
        self.assertEqual(snippets[0]["results"][0]["schema_fields"][0]["path"], "custom_fields.owner")

    def test_build_messages_embeds_netbox_context(self) -> None:
        settings = HarborSettings(llm=LlmSettings(base_url="http://llm", model="test-model"))
        with patch(
            "app.control._context_for_chat",
            return_value=([{"module": "netbox", "kind": "netbox", "object_type": "dcim.devices", "results": [{"name": "edge-sw01"}]}], ["netbox"]),
        ):
            messages, used_modules = _build_messages(settings, "Zeige edge-sw01", None)
        self.assertEqual(used_modules, ["netbox"])
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Nicht vertrauenswuerdiger Kontext aus Modulen", messages[0]["content"])
        self.assertIn("edge-sw01", messages[0]["content"])


class OpenStackConfigurationTests(unittest.TestCase):
    @staticmethod
    def _endpoint(name: str):
        application = create_app()
        return next(route.endpoint for route in application.routes if getattr(route, "name", "") == name)

    def test_openstack_configuration_never_returns_token(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            transport="local",
            settings={
                "auth_url": "https://identity.example/v3",
                "project_id": "",
                "project_name": "demo",
                "project_domain_name": "Default",
                "region_name": "RegionOne",
            },
        )
        endpoint = self._endpoint("openstack_configuration")
        with (
            patch("app.control.find_module", return_value=module),
            patch("app.control.load_module_named_secret", return_value="super-secret-token"),
        ):
            result = endpoint(_user=HarborUser(username="admin", password_hash="unused", role="admin"))

        self.assertTrue(result["token_configured"])
        self.assertEqual(result["project_id"], "")
        self.assertEqual(result["project_domain_name"], "Default")
        self.assertNotIn("token", {key: value for key, value in result.items() if key != "token_configured"})
        self.assertNotIn("super-secret-token", str(result))

    def test_openstack_configure_stores_token_outside_module(self) -> None:
        captured: dict[str, object] = {}

        def fake_upsert(module: ModuleConfig) -> ModuleConfig:
            captured["module"] = module
            return module

        endpoint = self._endpoint("openstack_configure")
        body = OpenStackConfigureRequest(
            project_id="",
            project_name="demo",
            project_domain_name="Default",
            token="super-secret-token",
            auth_url="https://identity.example/v3",
            region_name="RegionOne",
        )
        with (
            patch("app.control.find_module", return_value=None),
            patch("app.control.load_module_named_secret", return_value=""),
            patch("app.control.save_module_named_secret") as save_secret,
            patch("app.control.upsert_module", side_effect=fake_upsert),
            patch("app.control.module_status", return_value={"id": "openstack"}),
            patch("app.control.record_audit"),
        ):
            result = endpoint(body=body, _user=HarborUser(username="operator", password_hash="unused", role="operator"))

        module = captured["module"]
        save_secret.assert_called_once_with("openstack", "openstack_token", "super-secret-token")
        self.assertNotIn("token", module.settings)
        self.assertEqual(module.settings["auth_type"], "v3token")
        self.assertEqual(module.settings["project_id"], "")
        self.assertEqual(module.settings["project_domain_name"], "Default")
        self.assertTrue(result["token_configured"])

    def test_openstack_configure_accepts_project_scoped_token_without_project(self) -> None:
        captured: dict[str, object] = {}

        def fake_upsert(module: ModuleConfig) -> ModuleConfig:
            captured["module"] = module
            return module

        endpoint = self._endpoint("openstack_configure")
        body = OpenStackConfigureRequest(
            project_id="",
            token="project-scoped-token",
            auth_url="https://identity.example/v3",
            region_name="RegionOne",
        )
        with (
            patch("app.control.find_module", return_value=None),
            patch("app.control.load_module_named_secret", return_value=""),
            patch("app.control.save_module_named_secret"),
            patch("app.control.upsert_module", side_effect=fake_upsert),
            patch("app.control.module_status", return_value={"id": "openstack"}),
            patch("app.control.record_audit"),
        ):
            endpoint(body=body, _user=HarborUser(username="operator", password_hash="unused", role="operator"))

        module = captured["module"]
        self.assertEqual(module.settings["auth_type"], "token")
        self.assertEqual(module.settings["project_name"], "")
        self.assertEqual(module.settings["project_domain_name"], "")


if __name__ == "__main__":
    unittest.main()
