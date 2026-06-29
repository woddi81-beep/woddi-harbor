from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.auth import current_user, require_metrics_access
from app.config import HarborSettings, HarborUser, LlmSettings, ModuleConfig
from app.control import (
    ChatRequest,
    NetBoxConfigureRequest,
    OpenStackConfigureRequest,
    OpenStackTokenRequest,
    _build_messages,
    _context_for_chat,
    _direct_context_answer,
    create_app,
)
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

    def test_context_for_chat_reports_selected_module_context_failures(self) -> None:
        module = ModuleConfig(
            id="netbox",
            type="netbox_mcp",
            provider="netbox-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )
        with patch("app.control.load_modules", return_value=[module]), patch(
            "app.control._context_for_module",
            side_effect=RuntimeError("upstream failed"),
        ):
            snippets, used_modules = _context_for_chat("Zeige NetBox Daten", ["netbox"])

        self.assertEqual(used_modules, ["netbox"])
        self.assertEqual(snippets[0]["kind"], "netbox")
        self.assertIn("upstream failed", snippets[0]["note"])

    def test_context_for_chat_includes_openstack_results(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            provider="openstack-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )

        def fake_execute(
            module_id: str,
            action: str,
            payload: dict[str, object],
            **credentials: str,
        ) -> dict[str, object]:
            self.assertEqual(module_id, "openstack")
            self.assertEqual(action, "list_servers")
            self.assertEqual(payload["name"], "prod-api-01")
            self.assertEqual(credentials["openstack_token"], "alice-token")
            self.assertEqual(credentials["openstack_user"], "alice")
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
            snippets, used_modules = _context_for_chat(
                "Zeige mir in OpenStack den Server prod-api-01",
                None,
                openstack_token="alice-token",
                openstack_user="alice",
            )

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

        def fake_execute(
            module_id: str,
            action: str,
            payload: dict[str, object],
            **credentials: str,
        ) -> dict[str, object]:
            self.assertEqual(module_id, "openstack")
            self.assertEqual(action, "get_storage_statistics")
            self.assertEqual(payload, {})
            self.assertEqual(credentials["openstack_token"], "alice-token")
            self.assertEqual(credentials["openstack_user"], "alice")
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
            snippets, used_modules = _context_for_chat(
                "Wie voll ist mein OpenStack Storage in Prozent?",
                None,
                openstack_token="alice-token",
                openstack_user="alice",
            )

        self.assertEqual(used_modules, ["openstack"])
        self.assertEqual(snippets[0]["tool"], "get_storage_statistics")
        self.assertEqual(snippets[0]["results"][0]["quota"]["capacity_gib"]["percent"], 40.0)

    def test_context_for_chat_lists_openstack_servers_without_generic_name_filter(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            provider="openstack-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )

        def fake_execute(
            module_id: str,
            action: str,
            payload: dict[str, object],
            **credentials: str,
        ) -> dict[str, object]:
            self.assertEqual(module_id, "openstack")
            self.assertEqual(action, "list_servers")
            self.assertEqual(payload, {"limit": 5})
            return {
                "ok": True,
                "data": {"structuredContent": {"data": [{"name": "prod-api-01"}]}},
            }

        with patch("app.control.load_modules", return_value=[module]), patch(
            "app.control.execute_module",
            side_effect=fake_execute,
        ):
            snippets, used_modules = _context_for_chat("Welche OpenStack Server gibt es?", None)

        self.assertEqual(used_modules, ["openstack"])
        self.assertEqual(snippets[0]["tool"], "list_servers")
        self.assertEqual(snippets[0]["results"][0]["name"], "prod-api-01")

    def test_context_for_chat_routes_openstack_server_count_to_compute_limits(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            provider="openstack-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )

        def fake_execute(
            module_id: str,
            action: str,
            payload: dict[str, object],
            **credentials: str,
        ) -> dict[str, object]:
            self.assertEqual(module_id, "openstack")
            self.assertEqual(action, "get_compute_limits")
            self.assertEqual(payload, {})
            return {
                "ok": True,
                "data": {"structuredContent": {"data": {"absolute": {"totalInstancesUsed": 7, "maxTotalInstances": 20}}}},
            }

        with (
            patch("app.control.load_modules", return_value=[module]),
            patch("app.control.load_field_catalog", return_value={"resources": {}}),
            patch("app.control.execute_module", side_effect=fake_execute),
        ):
            snippets, used_modules = _context_for_chat("Wieviele Server siehst du?", None)

        self.assertEqual(used_modules, ["openstack"])
        self.assertEqual(snippets[0]["tool"], "get_compute_limits")
        self.assertEqual(snippets[0]["results"][0]["absolute"]["totalInstancesUsed"], 7)

    def test_context_for_chat_answers_openstack_resource_overview_from_field_catalog(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            provider="openstack-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )
        catalog = {
            "updated_at": "2026-06-29T09:12:04",
            "resource_count": 2,
            "resources": {
                "server": {
                    "tool": "list_servers",
                    "available": False,
                    "has_objects": False,
                    "fields": [],
                    "field_count": 0,
                    "error": "OpenStack server.list failed: owner_seen",
                },
                "volume": {
                    "tool": "list_volumes",
                    "available": True,
                    "has_objects": True,
                    "fields": [{"path": "id"}, {"path": "name"}, {"path": "status"}, {"path": "size"}],
                    "field_count": 4,
                },
            },
        }

        with (
            patch("app.control.load_modules", return_value=[module]),
            patch("app.control.load_field_catalog", return_value=catalog),
            patch("app.control.execute_module", side_effect=AssertionError("OpenStack should not be queried for catalog overview.")),
        ):
            snippets, used_modules = _context_for_chat("Welche Ressourcen siehst du?", None)

        self.assertEqual(used_modules, ["openstack"])
        self.assertEqual(snippets[0]["tool"], "field_catalog")
        payload = snippets[0]["results"][0]
        self.assertEqual(payload["resource_count"], 2)
        self.assertEqual(payload["available_resource_count"], 1)
        self.assertEqual(payload["unavailable_resources"][0]["name"], "server")
        self.assertEqual(payload["resources"][0]["name"], "volume")
        self.assertEqual(payload["resources"][0]["fields"], ["id", "name", "status", "size"])

    def test_context_for_chat_focuses_openstack_resource_overview_when_other_modules_exist(self) -> None:
        docs = ModuleConfig(id="10", type="docs", transport="local", path="/tmp/docs")
        netbox = ModuleConfig(
            id="netbox",
            type="netbox_mcp",
            provider="netbox-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )
        openstack = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            provider="openstack-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )
        catalog = {
            "resource_count": 1,
            "resources": {
                "volume": {
                    "tool": "list_volumes",
                    "available": True,
                    "has_objects": True,
                    "fields": [{"path": "id"}, {"path": "name"}],
                    "field_count": 2,
                },
            },
        }

        with (
            patch("app.control.load_modules", return_value=[docs, netbox, openstack]),
            patch("app.control.load_field_catalog", return_value=catalog),
            patch("app.control.execute_module", side_effect=AssertionError("Only field catalog context should be used.")),
        ):
            snippets, used_modules = _context_for_chat("Welche Ressourcen siehst du?", None)

        self.assertEqual(used_modules, ["openstack"])
        self.assertEqual(snippets[0]["tool"], "field_catalog")

    def test_context_for_chat_focuses_openstack_inventory_when_other_modules_exist(self) -> None:
        docs = ModuleConfig(id="10", type="docs", transport="local", path="/tmp/docs")
        netbox = ModuleConfig(
            id="netbox",
            type="netbox_mcp",
            provider="netbox-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )
        openstack = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            provider="openstack-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )

        def fake_execute(
            module_id: str,
            action: str,
            payload: dict[str, object],
            **credentials: str,
        ) -> dict[str, object]:
            self.assertEqual(module_id, "openstack")
            self.assertEqual(action, "get_project_statistics")
            self.assertEqual(payload, {})
            return {
                "ok": True,
                "data": {"structuredContent": {"data": {"inventory": {"server": {"count": 7}}}}},
            }

        with (
            patch("app.control.load_modules", return_value=[docs, netbox, openstack]),
            patch("app.control.load_field_catalog", return_value={"resources": {}}),
            patch("app.control.execute_module", side_effect=fake_execute),
        ):
            snippets, used_modules = _context_for_chat("Wieviele Server siehst du?", None)

        self.assertEqual(used_modules, ["openstack"])
        self.assertEqual(snippets[0]["tool"], "get_compute_limits")

    def test_context_for_chat_uses_compute_limits_for_server_count_with_unavailable_catalog_server(self) -> None:
        openstack = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            provider="openstack-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )
        catalog = {
            "resource_count": 1,
            "resources": {
                "server": {
                    "tool": "list_servers",
                    "available": False,
                    "has_objects": False,
                    "fields": [],
                    "field_count": 0,
                    "error": "OpenStack server.list failed: owner_seen",
                },
            },
        }

        def fake_execute(
            module_id: str,
            action: str,
            payload: dict[str, object],
            **credentials: str,
        ) -> dict[str, object]:
            self.assertEqual(module_id, "openstack")
            self.assertEqual(action, "get_compute_limits")
            self.assertEqual(payload, {})
            return {
                "ok": True,
                "data": {"structuredContent": {"data": {"absolute": {"totalInstancesUsed": 7}}}},
            }

        with (
            patch("app.control.load_modules", return_value=[openstack]),
            patch("app.control.load_field_catalog", return_value=catalog),
            patch("app.control.execute_module", side_effect=fake_execute),
        ):
            snippets, used_modules = _context_for_chat("Wieviele Server siehst du?", None)

        self.assertEqual(used_modules, ["openstack"])
        self.assertEqual(snippets[0]["tool"], "get_compute_limits")
        self.assertEqual(snippets[0]["results"][0]["absolute"]["totalInstancesUsed"], 7)

    def test_direct_context_answer_formats_openstack_field_catalog(self) -> None:
        answer = _direct_context_answer(
            "Welche Ressourcen siehst du?",
            [
                {
                    "module": "openstack",
                    "kind": "openstack",
                    "tool": "field_catalog",
                    "results": [
                        {
                            "resource_count": 2,
                            "available_resource_count": 1,
                            "resources": [
                                {
                                    "name": "volume",
                                    "tool": "list_volumes",
                                    "available": True,
                                    "field_count": 3,
                                    "fields": ["id", "name", "status"],
                                }
                            ],
                            "unavailable_resources": [{"name": "server", "error": "owner_seen"}],
                        }
                    ],
                }
            ],
        )

        self.assertIn("1 von 2 OpenStack-Ressourcen", answer)
        self.assertIn("volume", answer)
        self.assertIn("server", answer)

    def test_direct_context_answer_formats_openstack_server_count(self) -> None:
        answer = _direct_context_answer(
            "Wieviele Server siehst du?",
            [
                {
                    "module": "openstack",
                    "kind": "openstack",
                    "tool": "get_project_statistics",
                    "results": [
                        {
                            "inventory": {
                                "server": {
                                    "count": 7,
                                    "statuses": {"active": 5, "shutoff": 2},
                                }
                            },
                            "errors": {},
                        }
                    ],
                }
            ],
        )

        self.assertIn("7 OpenStack-Server", answer)
        self.assertIn("5 active", answer)

    def test_direct_context_answer_formats_openstack_server_count_from_compute_limits(self) -> None:
        answer = _direct_context_answer(
            "Wieviele Server siehst du?",
            [
                {
                    "module": "openstack",
                    "kind": "openstack",
                    "tool": "get_compute_limits",
                    "results": [
                        {
                            "absolute": {
                                "totalInstancesUsed": 7,
                                "maxTotalInstances": 20,
                            }
                        }
                    ],
                }
            ],
        )

        self.assertIn("7 OpenStack-Server", answer)
        self.assertIn("7 von 20", answer)

    def test_direct_context_answer_formats_empty_openstack_compute_limits(self) -> None:
        answer = _direct_context_answer(
            "Wieviele Server siehst du?",
            [
                {
                    "module": "openstack",
                    "kind": "openstack",
                    "tool": "get_compute_limits",
                    "results": [{}],
                    "note": "",
                }
            ],
        )

        self.assertIn("nicht aus den Compute-Limits ermitteln", answer)
        self.assertIn("totalInstancesUsed", answer)

    def test_direct_context_answer_uses_first_answerable_openstack_context(self) -> None:
        answer = _direct_context_answer(
            "Wieviele Server siehst du?",
            [
                {
                    "module": "old-openstack",
                    "kind": "openstack",
                    "tool": "list_servers",
                    "results": [],
                    "note": "",
                },
                {
                    "module": "openstack",
                    "kind": "openstack",
                    "tool": "get_compute_limits",
                    "results": [{"absolute": {"totalInstancesUsed": 7}}],
                    "note": "",
                },
            ],
        )

        self.assertIn("7 OpenStack-Server", answer)

    def test_direct_context_answer_formats_openstack_server_count_error(self) -> None:
        answer = _direct_context_answer(
            "Wieviele Server siehst du?",
            [
                {
                    "module": "openstack",
                    "kind": "openstack",
                    "tool": "get_project_statistics",
                    "results": [
                        {
                            "inventory": {
                                "server": {
                                    "count": None,
                                    "statuses": {},
                                    "available": False,
                                }
                            },
                            "errors": {"server": "'Image' object has no attribute 'owner_seen'"},
                        }
                    ],
                }
            ],
        )

        self.assertIn("nicht ermitteln", answer)
        self.assertIn("server.list meldet", answer)
        self.assertIn("owner_seen", answer)

    def test_direct_context_answer_formats_openstack_server_count_note(self) -> None:
        answer = _direct_context_answer(
            "Wieviele Server siehst du?",
            [
                {
                    "module": "openstack",
                    "kind": "openstack",
                    "tool": "get_project_statistics",
                    "results": [],
                    "note": "OpenStack-Abfrage fehlgeschlagen: timeout",
                }
            ],
        )

        self.assertIn("nicht ermitteln", answer)
        self.assertIn("timeout", answer)

    def test_context_for_chat_maps_openstack_field_questions_from_field_catalog(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            provider="openstack-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )
        catalog = {
            "resource_count": 1,
            "resources": {
                "floating_ip": {
                    "tool": "list_floating_ips",
                    "available": True,
                    "has_objects": True,
                    "fields": [{"path": "id"}, {"path": "fixed_ip_address"}, {"path": "floating_ip_address"}],
                    "field_count": 3,
                },
            },
        }

        with (
            patch("app.control.load_modules", return_value=[module]),
            patch("app.control.load_field_catalog", return_value=catalog),
            patch("app.control.execute_module", side_effect=AssertionError("OpenStack should not be queried for field catalog questions.")),
        ):
            snippets, used_modules = _context_for_chat("Wo gibt es das Feld fixed_ip_address?", None)

        self.assertEqual(used_modules, ["openstack"])
        self.assertEqual(snippets[0]["tool"], "field_catalog")
        self.assertEqual(snippets[0]["results"][0]["resources"][0]["name"], "floating_ip")
        self.assertIn("fixed_ip_address", snippets[0]["results"][0]["resources"][0]["fields"])

    def test_context_for_chat_prioritizes_servers_over_project_word(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            provider="openstack-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )

        def fake_execute(
            module_id: str,
            action: str,
            payload: dict[str, object],
            **credentials: str,
        ) -> dict[str, object]:
            self.assertEqual(module_id, "openstack")
            self.assertEqual(action, "list_servers")
            return {
                "ok": True,
                "data": {"structuredContent": {"data": [{"name": "prod-api-01"}]}},
            }

        with patch("app.control.load_modules", return_value=[module]), patch(
            "app.control.execute_module",
            side_effect=fake_execute,
        ):
            snippets, used_modules = _context_for_chat("Welche Server gibt es im Projekt PlusOne SE?", None)

        self.assertEqual(used_modules, ["openstack"])
        self.assertEqual(snippets[0]["tool"], "list_servers")

    def test_context_for_chat_routes_german_openstack_network_questions(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            provider="openstack-mcp-server",
            transport="local",
            remote_protocol="mcp",
        )

        def fake_execute(
            module_id: str,
            action: str,
            payload: dict[str, object],
            **credentials: str,
        ) -> dict[str, object]:
            self.assertEqual(module_id, "openstack")
            self.assertEqual(action, "list_networks")
            self.assertEqual(payload, {"limit": 5})
            return {
                "ok": True,
                "data": {"structuredContent": {"data": [{"name": "private-net"}]}},
            }

        with patch("app.control.load_modules", return_value=[module]), patch(
            "app.control.execute_module",
            side_effect=fake_execute,
        ):
            snippets, used_modules = _context_for_chat("Welche Netze gibt es in OpenStack?", None)

        self.assertEqual(used_modules, ["openstack"])
        self.assertEqual(snippets[0]["tool"], "list_networks")
        self.assertEqual(snippets[0]["results"][0]["name"], "private-net")

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

    def test_chat_endpoint_uses_direct_openstack_answer_without_llm(self) -> None:
        endpoint = next(route.endpoint for route in create_app().routes if getattr(route, "name", "") == "chat")
        context = [
            {
                "module": "openstack",
                "kind": "openstack",
                "tool": "get_compute_limits",
                "results": [{"absolute": {"totalInstancesUsed": 7}}],
                "note": "",
            }
        ]

        with (
            patch("app.control.load_settings", return_value=HarborSettings(llm=LlmSettings(base_url="http://llm", model="test-model"))),
            patch("app.control.create_chat_session", return_value="session-1"),
            patch("app.control.load_chat_messages", return_value=[]),
            patch("app.control.load_user_named_secret", return_value="token"),
            patch("app.control._context_for_chat", return_value=(context, ["openstack"])),
            patch("app.control.append_chat_message") as append_message,
            patch("app.control.complete_chat", side_effect=AssertionError("Direct OpenStack answers must not call the LLM.")),
        ):
            result = endpoint(
                ChatRequest(message="Wieviele Server siehst du?", modules=[], session_id=""),
                _user=HarborUser(username="admin", password_hash="unused", role="admin"),
            )

        self.assertEqual(result["used_modules"], ["openstack"])
        self.assertIn("7 OpenStack-Server", result["reply"])
        self.assertEqual(append_message.call_count, 2)


class OperationsApiTests(unittest.TestCase):
    @staticmethod
    def _endpoint(name: str):
        application = create_app()
        return next(route.endpoint for route in application.routes if getattr(route, "name", "") == name)

    def test_services_endpoint_returns_operational_overview(self) -> None:
        endpoint = self._endpoint("services")
        overview = {
            "version": {"version": "1.2.3", "git_rev": "abc123"},
            "services": [{"id": "harbor", "running": True}],
        }
        with patch("app.control.service_overview", return_value=overview):
            result = endpoint(_user=HarborUser(username="admin", password_hash="unused", role="admin"))

        self.assertEqual(result, overview)

    def test_service_restart_harbor_is_scheduled(self) -> None:
        endpoint = self._endpoint("service_run")
        with (
            patch("app.control.schedule_runtime_restart", return_value={"ok": True, "scheduled": True}) as schedule,
            patch("app.control.run_service_profile_action") as run_action,
            patch("app.control.record_audit"),
        ):
            result = endpoint(
                profile_id="harbor",
                action="restart",
                _user=HarborUser(username="admin", password_hash="unused", role="admin"),
            )

        self.assertTrue(result["scheduled"])
        schedule.assert_called_once()
        run_action.assert_not_called()

    def test_service_restart_module_uses_profile_action(self) -> None:
        endpoint = self._endpoint("service_run")
        with (
            patch("app.control.run_service_profile_action", return_value={"ok": True, "message": "restarted"}) as run_action,
            patch("app.control.record_audit"),
        ):
            result = endpoint(
                profile_id="module:openstack",
                action="restart",
                _user=HarborUser(username="admin", password_hash="unused", role="admin"),
            )

        self.assertTrue(result["ok"])
        run_action.assert_called_once_with("module:openstack", "restart")

    def test_system_update_schedules_restart_when_required(self) -> None:
        endpoint = self._endpoint("system_update")
        with (
            patch("app.control.update_checkout", return_value={"ok": True, "changed": True, "restart_required": True}),
            patch("app.control.schedule_runtime_restart", return_value={"ok": True, "scheduled": True}) as schedule,
            patch("app.control.record_audit"),
        ):
            result = endpoint(_user=HarborUser(username="admin", password_hash="unused", role="admin"))

        self.assertTrue(result["restart"]["scheduled"])
        schedule.assert_called_once()


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
                "region_name": "RegionOne",
            },
        )
        endpoint = self._endpoint("openstack_configuration")
        secrets = {
            "openstack_token": "super-secret-token",
            "openstack_token_project_id": "project-1",
            "openstack_token_project_name": "production",
            "openstack_token_project_domain_name": "Default",
            "openstack_token_user_name": "alice",
            "openstack_token_expires": "2026-06-24T17:17:08+0000",
        }
        with (
            patch("app.control.find_module", return_value=module),
            patch(
                "app.control.load_user_named_secret",
                side_effect=lambda _username, name: secrets.get(name, ""),
            ) as load_secret,
        ):
            result = endpoint(_user=HarborUser(username="admin", password_hash="unused", role="admin"))

        self.assertEqual(load_secret.call_args_list[0].args, ("admin", "openstack_token"))
        self.assertTrue(result["token_configured"])
        self.assertEqual(result["token_owner"], "admin")
        self.assertEqual(result["token_scope"]["project_id"], "project-1")
        self.assertEqual(result["token_scope"]["project_name"], "production")
        self.assertEqual(result["token_scope"]["project_domain_name"], "Default")
        self.assertEqual(result["token_scope"]["user_name"], "alice")
        self.assertEqual(result["token_scope"]["expires_at"], "2026-06-24T17:17:08+0000")
        self.assertTrue(result["can_configure"])
        self.assertEqual(result["credential_mode"], "per_user")
        self.assertEqual(result["scope_mode"], "project_from_token")
        self.assertNotIn("project_id", {key: value for key, value in result.items() if key != "token_scope"})
        self.assertNotIn("project_name", {key: value for key, value in result.items() if key != "token_scope"})
        self.assertNotIn("token", {key: value for key, value in result.items() if key != "token_configured"})
        self.assertNotIn("super-secret-token", str(result))

    def test_openstack_configuration_validates_raw_token_for_scope_display(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            transport="local",
            timeout_seconds=12,
            settings={
                "auth_url": "https://identity.example/v3",
                "region_name": "RegionOne",
            },
        )
        endpoint = self._endpoint("openstack_configuration")
        with (
            patch("app.control.find_module", return_value=module),
            patch(
                "app.control.load_user_named_secret",
                side_effect=lambda _username, name: "raw-token" if name == "openstack_token" else "",
            ),
            patch(
                "app.control.validate_openstack_token_scope",
                return_value={
                    "project_scoped": True,
                    "project_id": "project-1",
                    "project_name": "production",
                    "project_domain_name": "Default",
                    "user_name": "alice",
                    "has_service_catalog": True,
                },
            ) as validate_scope,
        ):
            result = endpoint(_user=HarborUser(username="admin", password_hash="unused", role="admin"))

        validate_scope.assert_called_once()
        self.assertEqual(result["token_scope"]["source"], "keystone_validation")
        self.assertEqual(result["token_scope"]["project_name"], "production")
        self.assertEqual(result["token_scope"]["project_domain_name"], "Default")
        self.assertNotIn("raw-token", str(result))

    def test_openstack_configure_stores_token_outside_module(self) -> None:
        captured: dict[str, object] = {}

        def fake_upsert(module: ModuleConfig) -> ModuleConfig:
            captured["module"] = module
            return module

        endpoint = self._endpoint("openstack_configure")
        body = OpenStackConfigureRequest(
            token="super-secret-token",
            auth_url="https://identity.example/v3",
            region_name="RegionOne",
        )
        with (
            patch("app.control.find_module", return_value=None),
            patch("app.control.load_user_named_secret", return_value=""),
            patch("app.control.save_user_named_secret") as save_secret,
            patch("app.control.upsert_module", side_effect=fake_upsert),
            patch("app.control.delete_module_named_secret"),
            patch("app.control.module_status", return_value={"id": "openstack"}),
            patch("app.control.record_audit"),
        ):
            result = endpoint(body=body, _user=HarborUser(username="operator", password_hash="unused", role="operator"))

        module = captured["module"]
        save_secret.assert_called_once_with("operator", "openstack_token", "super-secret-token")
        self.assertNotIn("token", module.settings)
        self.assertEqual(module.settings["auth_type"], "token")
        self.assertNotIn("project_id", module.settings)
        self.assertNotIn("project_name", module.settings)
        self.assertTrue(result["token_configured"])

    def test_openstack_configure_extracts_id_from_token_json(self) -> None:
        captured_secrets: dict[str, str] = {}

        def fake_save(_username: str, name: str, value: str) -> None:
            captured_secrets[name] = value

        endpoint = self._endpoint("openstack_configure")
        body = OpenStackConfigureRequest(
            token=(
                '{"id":"token-id","project_id":"project-1","project_domain_name":"Default",'
                '"user_id":"user-1","user_domain_id":"domain-1","expires":"2026-06-24T17:17:08+0000"}'
            ),
            auth_url="https://identity.example/v3",
            region_name="RegionOne",
        )
        with (
            patch("app.control.find_module", return_value=None),
            patch("app.control.load_user_named_secret", return_value=""),
            patch("app.control.save_user_named_secret", side_effect=fake_save),
            patch("app.control.delete_user_named_secret"),
            patch("app.control.upsert_module", side_effect=lambda module: module),
            patch("app.control.delete_module_named_secret"),
            patch("app.control.module_status", return_value={"id": "openstack"}),
            patch("app.control.record_audit"),
        ):
            endpoint(body=body, _user=HarborUser(username="operator", password_hash="unused", role="operator"))

        self.assertEqual(captured_secrets["openstack_token"], "token-id")
        self.assertEqual(captured_secrets["openstack_token_project_id"], "project-1")
        self.assertEqual(captured_secrets["openstack_token_project_domain_name"], "Default")
        self.assertEqual(captured_secrets["openstack_token_user_id"], "user-1")
        self.assertEqual(captured_secrets["openstack_token_user_domain_id"], "domain-1")
        self.assertEqual(captured_secrets["openstack_token_expires"], "2026-06-24T17:17:08+0000")
        self.assertNotIn("id", {key: value for key, value in captured_secrets.items() if key != "openstack_token"})

    def test_openstack_configure_accepts_project_scoped_token_without_project(self) -> None:
        captured: dict[str, object] = {}

        def fake_upsert(module: ModuleConfig) -> ModuleConfig:
            captured["module"] = module
            return module

        endpoint = self._endpoint("openstack_configure")
        body = OpenStackConfigureRequest(
            token="project-scoped-token",
            auth_url="https://identity.example/v3",
            region_name="RegionOne",
        )
        with (
            patch("app.control.find_module", return_value=None),
            patch("app.control.load_user_named_secret", return_value=""),
            patch("app.control.save_user_named_secret"),
            patch("app.control.upsert_module", side_effect=fake_upsert),
            patch("app.control.delete_module_named_secret"),
            patch("app.control.module_status", return_value={"id": "openstack"}),
            patch("app.control.record_audit"),
        ):
            endpoint(body=body, _user=HarborUser(username="operator", password_hash="unused", role="operator"))

        module = captured["module"]
        self.assertEqual(module.settings["auth_type"], "token")
        self.assertNotIn("project_name", module.settings)
        self.assertNotIn("project_domain_name", module.settings)

    def test_openstack_configuration_isolated_by_user(self) -> None:
        endpoint = self._endpoint("openstack_configuration")
        tokens = {"alice": "alice-token", "bob": ""}
        with (
            patch("app.control.find_module", return_value=None),
            patch(
                "app.control.load_user_named_secret",
                side_effect=lambda username, name: tokens[username] if name == "openstack_token" else "",
            ),
        ):
            alice = endpoint(_user=HarborUser(username="alice", password_hash="unused", role="viewer"))
            bob = endpoint(_user=HarborUser(username="bob", password_hash="unused", role="viewer"))

        self.assertTrue(alice["token_configured"])
        self.assertFalse(bob["token_configured"])
        self.assertEqual(alice["token_owner"], "alice")
        self.assertEqual(bob["token_owner"], "bob")
        self.assertFalse(alice["can_configure"])

    def test_openstack_token_update_stores_only_current_user_token(self) -> None:
        endpoint = self._endpoint("openstack_token_update")
        with (
            patch("app.control.save_user_named_secret") as save_secret,
            patch("app.control.delete_module_named_secret"),
            patch("app.control.record_audit"),
        ):
            result = endpoint(
                body=OpenStackTokenRequest(token="alice-token"),
                _user=HarborUser(username="alice", password_hash="unused", role="viewer"),
            )

        save_secret.assert_called_once_with("alice", "openstack_token", "alice-token")
        self.assertEqual(result["token_owner"], "alice")
        self.assertNotIn("alice-token", str(result))

    def test_netbox_configure_removes_legacy_token_and_uses_anonymous_access(self) -> None:
        captured: dict[str, object] = {}

        def fake_upsert(module: ModuleConfig) -> ModuleConfig:
            captured["module"] = module
            return module

        endpoint = self._endpoint("netbox_configure")
        body = NetBoxConfigureRequest(netbox_url="https://netbox.example")
        with (
            patch("app.control.find_module", return_value=None),
            patch("app.control.upsert_module", side_effect=fake_upsert),
            patch("app.control.delete_module_named_secret") as delete_secret,
            patch("app.control.module_status", return_value={"id": "netbox"}),
            patch("app.control.record_audit"),
        ):
            result = endpoint(body=body, _user=HarborUser(username="operator", password_hash="unused", role="operator"))

        module = captured["module"]
        delete_secret.assert_called_once_with("netbox", "netbox_token")
        self.assertNotIn("netbox_token", module.settings)
        self.assertEqual(result["authentication"], "anonymous")
        self.assertTrue(result["read_only"])


class ReadinessTests(unittest.TestCase):
    @staticmethod
    def _endpoint():
        application = create_app()
        return next(route.endpoint for route in application.routes if getattr(route, "name", "") == "readiness")

    def test_readiness_returns_503_when_llm_is_unreachable(self) -> None:
        endpoint = self._endpoint()
        with (
            patch("app.control.load_settings", return_value=HarborSettings()),
            patch("app.control.initialize_database", return_value=Path("/tmp/harbor.db")),
            patch("app.control.load_users", return_value=[HarborUser(username="admin", password_hash="x", role="admin")]),
            patch("app.control._llm_health", return_value={"ok": False, "status": "error"}),
        ):
            response = endpoint()

        self.assertEqual(response.status_code, 503)
        self.assertFalse(json.loads(response.body)["ok"])

    def test_readiness_returns_200_when_dependencies_are_ready(self) -> None:
        endpoint = self._endpoint()
        with (
            patch("app.control.load_settings", return_value=HarborSettings()),
            patch("app.control.initialize_database", return_value=Path("/tmp/harbor.db")),
            patch("app.control.load_users", return_value=[HarborUser(username="admin", password_hash="x", role="admin")]),
            patch("app.control._llm_health", return_value={"ok": True, "status": "connected"}),
        ):
            response = endpoint()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.body)["ok"])


if __name__ == "__main__":
    unittest.main()
