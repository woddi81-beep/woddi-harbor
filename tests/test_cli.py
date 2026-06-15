from __future__ import annotations

import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from app.cli import app, module_add_netbox_mcp, module_add_openstack_local_mcp, module_add_openstack_mcp


class CliModuleTests(unittest.TestCase):
    def test_module_call_accepts_positional_payload(self) -> None:
        with patch("app.cli.execute_module", return_value={"ok": True}) as execute:
            result = CliRunner().invoke(app, ["module", "call", "openstack", "list_servers", "{}"])

        self.assertEqual(result.exit_code, 0, result.output)
        execute.assert_called_once_with("openstack", "list_servers", {})

    def test_module_call_accepts_payload_option(self) -> None:
        with patch("app.cli.execute_module", return_value={"ok": True}) as execute:
            result = CliRunner().invoke(app, ["module", "call", "openstack", "list_servers", "--payload", "{}"])

        self.assertEqual(result.exit_code, 0, result.output)
        execute.assert_called_once_with("openstack", "list_servers", {})

    def test_add_openstack_mcp_registers_remote_mcp_module(self) -> None:
        captured: dict[str, object] = {}

        def fake_upsert(module) -> object:
            captured["module"] = module
            return module

        with (
            patch("app.cli.validate_module_config", return_value=[]),
            patch("app.cli.upsert_module", side_effect=fake_upsert),
            patch("app.cli.load_module_named_secret", return_value=""),
            patch("app.cli.console.print"),
        ):
            module_add_openstack_mcp(module_id="openstack", base_url="http://127.0.0.1:8080/mcp")

        module = captured["module"]
        self.assertEqual(module.id, "openstack")
        self.assertEqual(module.type, "mcp_http")
        self.assertEqual(module.provider, "openstack-mcp-server")
        self.assertEqual(module.remote_protocol, "mcp")
        self.assertEqual(module.base_url, "http://127.0.0.1:8080/mcp")
        self.assertEqual(
            module.tool_names,
            [
                "discover_resources",
                "get_storage_statistics",
                "get_project_statistics",
                "list_servers",
                "list_projects",
                "list_images",
            ],
        )

    def test_add_openstack_local_mcp_registers_local_mcp_module(self) -> None:
        captured: dict[str, object] = {}

        def fake_upsert(module) -> object:
            captured["module"] = module
            return module

        with (
            patch("app.cli.validate_module_config", return_value=[]),
            patch("app.cli.upsert_module", side_effect=fake_upsert),
            patch("app.cli.console.print"),
        ):
            module_add_openstack_local_mcp(module_id="openstack")

        module = captured["module"]
        self.assertEqual(module.id, "openstack")
        self.assertEqual(module.type, "openstack_mcp")
        self.assertEqual(module.transport, "local")
        self.assertEqual(module.remote_protocol, "mcp")

    def test_add_netbox_mcp_allows_open_api_without_token(self) -> None:
        captured: dict[str, object] = {}

        def fake_upsert(module) -> object:
            captured["module"] = module
            return module

        with (
            patch("app.cli.validate_module_config", return_value=[]),
            patch("app.cli.upsert_module", side_effect=fake_upsert),
            patch("app.cli.delete_module_named_secret"),
            patch("app.cli.console.print"),
        ):
            module_add_netbox_mcp(module_id="netbox", netbox_url="https://netbox.example")

        module = captured["module"]
        self.assertEqual(module.type, "netbox_mcp")
        self.assertEqual(module.transport, "local")
        self.assertNotIn("netbox_token", module.settings)
        self.assertNotIn("netbox_token_env", module.settings)


if __name__ == "__main__":
    unittest.main()
