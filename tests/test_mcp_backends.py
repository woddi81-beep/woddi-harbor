from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from app.field_cache import load_field_catalog, update_catalog_from_tool_result
from app.mcp.netbox import NetBoxBackend
from app.mcp.netbox import create_app as create_netbox_app
from app.mcp.openstack import OpenStackBackend, OpenStackDiagnosticError, OpenStackUserBackendRegistry
from app.mcp.openstack import create_app as create_openstack_app


class FakeHTTPResponse:
    def __init__(self, payload: object, *, status_code: int = 200, reason_phrase: str = "OK") -> None:
        self.payload = payload
        self.status_code = status_code
        self.reason_phrase = reason_phrase
        self.text = "{}" if isinstance(payload, dict) else str(payload)
        self.content = self.text.encode("utf-8")
        self.request = httpx.Request("GET", "https://netbox.example/api/fake/")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code} {self.reason_phrase}",
                request=self.request,
                response=self,
            )

    def json(self) -> object:
        return self.payload


class FakeHTTPClient:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeHTTPResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        if isinstance(payload, FakeHTTPResponse):
            return payload
        return FakeHTTPResponse(payload)

    def close(self) -> None:
        return None


class Resource:
    def __init__(self, **payload: object) -> None:
        self.payload = payload

    def to_dict(self) -> dict[str, object]:
        return self.payload


class Compute:
    def __init__(self) -> None:
        self.server_calls = 0

    def servers(self, *, details: bool = False):
        self.server_calls += 1
        self.details = details
        return [
            Resource(id="vm-1", name="prod", status="ACTIVE", token="secret-token", admin_pass="secret-password"),
            Resource(id="vm-2", name="test", status="SHUTOFF"),
        ]

    def get_limits(self):
        return Resource(
            absolute={
                "maxTotalInstances": 20,
                "totalInstancesUsed": 5,
                "maxTotalCores": 40,
                "totalCoresUsed": 10,
                "maxTotalRAMSize": 102400,
                "totalRAMUsed": 25600,
            }
        )


class BlockStorage:
    def volumes(self, *, details: bool = False):
        self.details = details
        return [
            Resource(id="volume-1", name="database", status="available", size=100),
            Resource(id="volume-2", name="logs", status="in-use", size=300),
        ]

    def snapshots(self, *, details: bool = False):
        return [Resource(id="snapshot-1", status="available", size=100)]

    def backups(self, *, details: bool = False):
        return [Resource(id="backup-1", status="available", size=100)]

    def get_limits(self, project: str | None = None):
        self.project = project
        return Resource(
            absolute={
                "maxTotalVolumeGigabytes": 1000,
                "totalGigabytesUsed": 400,
                "maxTotalVolumes": 20,
                "totalVolumesUsed": 2,
                "maxTotalSnapshots": 10,
                "totalSnapshotsUsed": 1,
                "maxTotalBackups": 5,
                "totalBackupsUsed": 1,
                "maxTotalBackupGigabytes": 500,
                "totalBackupGigabytesUsed": 100,
            }
        )


class Connection:
    def __init__(self) -> None:
        self.compute = Compute()
        self.block_storage = BlockStorage()
        self.session = type(
            "Session",
            (),
            {
                "auth": type(
                    "Auth",
                    (),
                    {
                        "get_access": lambda _self, _session: type(
                            "Access",
                            (),
                            {
                                "project_id": "project-1",
                                "project_name": "production",
                                "project_scoped": True,
                                "has_service_catalog": lambda _self: True,
                            },
                        )()
                    },
                )()
            },
        )()

    def authorize(self) -> str:
        return "token"


class UnscopedConnection(Connection):
    def __init__(self) -> None:
        super().__init__()
        self.session = type(
            "Session",
            (),
            {
                "auth": type(
                    "Auth",
                    (),
                    {
                        "get_access": lambda _self, _session: type(
                            "Access",
                            (),
                            {
                                "project_id": "",
                                "project_name": "",
                                "project_scoped": False,
                                "has_service_catalog": lambda _self: False,
                            },
                        )()
                    },
                )()
            },
        )()


class ProjectScopedNoCatalogConnection(Connection):
    def __init__(self) -> None:
        super().__init__()
        self.session = type(
            "Session",
            (),
            {
                "auth": type(
                    "Auth",
                    (),
                    {
                        "get_access": lambda _self, _session: type(
                            "Access",
                            (),
                            {
                                "project_id": "project-1",
                                "project_name": "production",
                                "project_scoped": True,
                                "has_service_catalog": lambda _self: False,
                            },
                        )()
                    },
                )()
            },
        )()


class ProjectPayloadNoCatalogConnection(Connection):
    def __init__(self) -> None:
        super().__init__()
        self.session = type(
            "Session",
            (),
            {
                "auth": type(
                    "Auth",
                    (),
                    {
                        "get_access": lambda _self, _session: type(
                            "Access",
                            (),
                            {
                                "_data": {
                                    "token": {
                                        "project": {"id": "project-1", "name": "production"},
                                    }
                                },
                                "has_service_catalog": lambda _self: False,
                            },
                        )()
                    },
                )()
            },
        )()


class McpBackendTests(unittest.TestCase):
    def test_netbox_health_does_not_block_on_upstream_discovery(self) -> None:
        with patch.object(
            NetBoxBackend,
            "discover_api_structure",
            side_effect=AssertionError("liveness must not call NetBox"),
        ):
            app = create_netbox_app("https://netbox.example")
            health = next(route.endpoint for route in app.routes if route.path == "/health")
            result = health()

        self.assertTrue(result["ok"])
        self.assertEqual(result["upstream_check"], "mcp_discovery")

    def test_openstack_worker_health_does_not_reference_missing_self(self) -> None:
        app = create_openstack_app({"OS_AUTH_URL": "https://identity.example/v3"})
        health = next(route.endpoint for route in app.routes if route.path == "/health")
        result = health()

        self.assertEqual(result["scope_mode"], "token_project")
        self.assertEqual(result["credential_mode"], "per_user_request_token")
        self.assertIn("credentials", result)

    def test_openstack_registry_separates_users_and_rotates_tokens(self) -> None:
        created: list[OpenStackBackend] = []

        class Backend(OpenStackBackend):
            def __init__(self, credentials: dict[str, str]) -> None:
                super().__init__(credentials, connection_factory=lambda _credentials: Connection())
                self.closed = False
                created.append(self)

            def close(self) -> None:
                self.closed = True
                super().close()

        registry = OpenStackUserBackendRegistry(
            {"OS_AUTH_URL": "https://identity.example/v3"},
            backend_factory=Backend,
        )
        self.addCleanup(registry.close)

        alice_first = registry.get("alice", "token-a")
        self.assertIs(alice_first, registry.get("alice", "token-a"))
        bob = registry.get("bob", "token-a")
        self.assertIsNot(alice_first, bob)

        alice_rotated = registry.get("alice", "token-b")
        self.assertIsNot(alice_first, alice_rotated)
        self.assertTrue(alice_first.closed)  # type: ignore[attr-defined]
        self.assertFalse(bob.closed)  # type: ignore[attr-defined]
        self.assertNotIn("token-a", str(registry.stats()))

    def test_openstack_registry_uses_password_credentials_provider(self) -> None:
        created: list[OpenStackBackend] = []

        class Backend(OpenStackBackend):
            def __init__(self, credentials: dict[str, str]) -> None:
                super().__init__(credentials, connection_factory=lambda _credentials: Connection())
                created.append(self)

        registry = OpenStackUserBackendRegistry(
            {"OS_AUTH_URL": "https://identity.example/v3"},
            credential_provider=lambda _username: {
                "OS_USERNAME": "alice",
                "OS_PASSWORD": "secret",
                "OS_PROJECT_NAME": "production",
            },
            backend_factory=Backend,
        )
        self.addCleanup(registry.close)

        backend = registry.get("alice")

        self.assertIs(backend, created[0])
        self.assertEqual(backend.credentials["OS_AUTH_TYPE"], "password")
        self.assertEqual(backend.credentials["OS_PROJECT_NAME"], "production")

    def test_netbox_fields_use_native_filter_and_response_cache(self) -> None:
        backend = NetBoxBackend("https://netbox.example")
        backend._client.close()
        client = FakeHTTPClient([{"count": 1, "results": [{"id": 1, "name": "edge"}], "next": None, "previous": None}])
        backend._client = client  # type: ignore[assignment]

        arguments = {
            "object_type": "dcim.devices",
            "fields": ["id", "name"],
            "limit": 5,
            "fetch_all": False,
        }
        first = backend.call_tool("get_objects", arguments)
        second = backend.call_tool("get_objects", arguments)

        self.assertEqual(first, second)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["params"]["fields"], "id,name")  # type: ignore[index]
        self.assertEqual(backend._response_cache.stats()["hits"], 1)

    def test_netbox_http_status_errors_are_raised_with_context(self) -> None:
        backend = NetBoxBackend("https://netbox.example")
        backend._client.close()
        backend._client = FakeHTTPClient(  # type: ignore[assignment]
            [FakeHTTPResponse({"detail": "anonymous access denied"}, status_code=403, reason_phrase="Forbidden")]
        )

        with self.assertRaisesRegex(httpx.HTTPStatusError, "NetBox HTTP 403 Forbidden"):
            backend.call_tool("get_objects", {"object_type": "dcim.devices"})

    def test_netbox_rejects_cross_origin_and_write_calls(self) -> None:
        backend = NetBoxBackend("https://netbox.example")
        self.addCleanup(backend.close)

        with self.assertRaisesRegex(ValueError, "configured origin"):
            backend.call_tool("call_endpoint", {"path": "https://attacker.example/api/secrets/"})
        with self.assertRaisesRegex(ValueError, "read-only"):
            backend.call_tool("call_endpoint", {"path": "dcim/devices/", "method": "POST"})

    def test_netbox_always_uses_anonymous_headers(self) -> None:
        backend = NetBoxBackend("https://netbox.example")
        self.addCleanup(backend.close)

        self.assertNotIn("Authorization", backend._headers())

    def test_netbox_rejects_cross_origin_pagination_links(self) -> None:
        backend = NetBoxBackend("https://netbox.example")
        backend._client.close()
        backend._client = FakeHTTPClient(  # type: ignore[assignment]
            [{"count": 2, "results": [{"id": 1}], "next": "https://attacker.example/api/devices/?offset=1", "previous": None}]
        )

        with self.assertRaisesRegex(ValueError, "configured origin"):
            backend.call_tool("get_objects", {"object_type": "dcim.devices", "fetch_all": True})

    def test_netbox_discovers_schema_and_observed_fields(self) -> None:
        schema = {
            "paths": {
                "/api/dcim/devices/": {
                    "get": {
                        "parameters": [
                            {"name": "status", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/PaginatedDeviceList"}
                                    }
                                }
                            }
                        },
                    }
                }
            },
            "components": {
                "schemas": {
                    "PaginatedDeviceList": {
                        "type": "object",
                        "properties": {
                            "count": {"type": "integer"},
                            "results": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/Device"},
                            },
                        },
                    },
                    "Device": {
                        "type": "object",
                        "required": ["id", "name"],
                        "properties": {
                            "id": {"type": "integer"},
                            "name": {"type": "string"},
                            "custom_fields": {
                                "type": "object",
                                "properties": {"owner": {"type": "string"}},
                            },
                        },
                    },
                }
            },
        }
        sample = {
            "count": 1,
            "results": [{"id": 1, "name": "edge", "custom_fields": {"owner": "network"}}],
        }
        backend = NetBoxBackend("https://netbox.example")
        backend._client.close()
        backend._client = FakeHTTPClient([schema, sample])  # type: ignore[assignment]

        discovery = backend.call_tool("discover_object_types", {})
        description = backend.call_tool("describe_object_type", {"object_type": "dcim.devices"})

        discovered = discovery["structuredContent"]["data"]["object_types"]
        described = description["structuredContent"]["data"]
        self.assertEqual(discovered[0]["object_type"], "dcim.devices")
        self.assertIn("custom_fields.owner", {field["path"] for field in described["schema_fields"]})
        self.assertIn("custom_fields.owner", {field["path"] for field in described["observed_fields"]})
        self.assertEqual(described["filter_parameters"][0]["name"], "status")

    def test_field_catalog_caches_netbox_descriptions(self) -> None:
        result = {
            "structuredContent": {
                "data": {
                    "object_type": "dcim.devices",
                    "endpoint": "/api/dcim/devices/",
                    "schema_fields": [{"path": "name", "type": "string"}],
                    "observed_fields": [{"path": "custom_fields.owner", "type": "string"}],
                    "filter_parameters": [{"name": "status", "type": "string"}],
                    "observed_field_count": 1,
                }
            }
        }
        with TemporaryDirectory() as tmp:
            with patch("app.field_cache.FIELD_CACHE_DIR", Path(tmp)):
                catalog = update_catalog_from_tool_result("netbox", "netbox", "describe_object_type", result)
                loaded = load_field_catalog("netbox")

        self.assertIsNotNone(catalog)
        self.assertEqual(loaded["resource_count"], 1)
        fields = {field["path"] for field in loaded["resources"]["dcim.devices"]["fields"]}
        self.assertIn("name", fields)
        self.assertIn("custom_fields.owner", fields)

    def test_netbox_inventory_statistics_use_collection_counts(self) -> None:
        backend = NetBoxBackend("https://netbox.example")
        backend._client.close()
        backend._client = FakeHTTPClient(
            [
                {"count": 12, "results": [{"id": 1}]},
                {"count": 3, "results": [{"id": 2}]},
            ]
        )  # type: ignore[assignment]

        result = backend.call_tool(
            "get_inventory_statistics",
            {"object_types": ["dcim.devices", "dcim.sites"]},
        )
        data = result["structuredContent"]["data"]
        self.assertEqual(data["total_objects_across_collections"], 15)
        self.assertEqual(data["statistics"][0]["count"], 12)

    def test_openstack_cache_is_shared_across_filters_and_redacts_secrets(self) -> None:
        connection = Connection()
        backend = OpenStackBackend(
            credentials={"OS_AUTH_URL": "https://identity.example/v3", "OS_TOKEN": "token", "OS_PROJECT_ID": "project-1"},
            connection_factory=lambda _credentials: connection,
        )

        active = backend.call_tool("list_servers", {"status": "ACTIVE", "fields": ["name"]})
        active_full = backend.call_tool("list_servers", {"status": "ACTIVE"})
        backend.call_tool("list_servers", {"status": "SHUTOFF"})

        self.assertEqual(active["structuredContent"]["data"], [{"name": "prod"}])
        self.assertEqual(connection.compute.server_calls, 1)
        self.assertEqual(active_full["structuredContent"]["data"][0]["token"], "[redacted]")
        self.assertEqual(active_full["structuredContent"]["data"][0]["admin_pass"], "[redacted]")

    def test_openstack_unscoped_token_reports_structured_diagnostics(self) -> None:
        backend = OpenStackBackend(
            credentials={"OS_AUTH_URL": "https://identity.example/v3", "OS_TOKEN": "token"},
            connection_factory=lambda _credentials: UnscopedConnection(),
        )

        with self.assertRaises(OpenStackDiagnosticError) as context:
            backend.call_tool("list_servers", {})

        diagnostics = context.exception.diagnostics
        self.assertEqual(diagnostics["phase"], "scope_validation")
        self.assertFalse(diagnostics["token_scope"]["project_scoped"])
        self.assertFalse(diagnostics["token_scope"]["has_service_catalog"])
        self.assertTrue(diagnostics["credentials"]["token_present"])

    def test_openstack_project_token_without_catalog_is_allowed(self) -> None:
        backend = OpenStackBackend(
            credentials={"OS_AUTH_URL": "https://identity.example/v3", "OS_TOKEN": "token"},
            connection_factory=lambda _credentials: ProjectScopedNoCatalogConnection(),
        )

        result = backend.call_tool("list_servers", {"status": "ACTIVE"})
        health = backend.health()

        self.assertEqual(result["structuredContent"]["data"][0]["name"], "prod")
        self.assertFalse(health["project"]["has_service_catalog"])
        self.assertIn("no Keystone service catalog", health["warnings"][0])
        self.assertIn("warnings", result["structuredContent"])

    def test_openstack_project_payload_without_catalog_is_allowed(self) -> None:
        backend = OpenStackBackend(
            credentials={"OS_AUTH_URL": "https://identity.example/v3", "OS_TOKEN": "token"},
            connection_factory=lambda _credentials: ProjectPayloadNoCatalogConnection(),
        )

        result = backend.call_tool("list_servers", {"status": "ACTIVE"})
        health = backend.health()

        self.assertEqual(result["structuredContent"]["data"][0]["name"], "prod")
        self.assertEqual(health["project"]["id"], "project-1")
        self.assertFalse(health["project"]["has_service_catalog"])

    def test_openstack_exposes_bounded_volume_listing(self) -> None:
        connection = Connection()
        backend = OpenStackBackend(
            credentials={"OS_AUTH_URL": "https://identity.example/v3", "OS_TOKEN": "token", "OS_PROJECT_ID": "project-1"},
            connection_factory=lambda _credentials: connection,
        )

        result = backend.call_tool("list_volumes", {"status": "available", "limit": 10})
        self.assertEqual(result["structuredContent"]["data"][0]["name"], "database")
        with self.assertRaisesRegex(ValueError, "zwischen"):
            backend.call_tool("list_volumes", {"limit": 10000})

    def test_openstack_storage_statistics_calculate_quota_percentages(self) -> None:
        connection = Connection()
        backend = OpenStackBackend(
            credentials={"OS_AUTH_URL": "https://identity.example/v3", "OS_TOKEN": "token", "OS_PROJECT_ID": "project-1"},
            connection_factory=lambda _credentials: connection,
        )

        result = backend.call_tool("get_storage_statistics", {})
        data = result["structuredContent"]["data"]
        self.assertEqual(data["quota"]["capacity_gib"]["percent"], 40.0)
        self.assertEqual(data["quota"]["volumes"]["percent"], 10.0)
        self.assertEqual(data["inventory"]["provisioned_volume_gib"], 400)
        self.assertEqual(data["inventory"]["volume_statuses"], {"available": 1, "in-use": 1})

    def test_openstack_resource_discovery_reports_observed_fields(self) -> None:
        connection = Connection()
        backend = OpenStackBackend(
            credentials={"OS_AUTH_URL": "https://identity.example/v3", "OS_TOKEN": "token", "OS_PROJECT_ID": "project-1"},
            connection_factory=lambda _credentials: connection,
        )

        result = backend.call_tool("discover_resources", {"resources": ["server", "volume"]})
        resources = result["structuredContent"]["data"]["resources"]
        self.assertEqual([item["resource"] for item in resources], ["server", "volume"])
        self.assertIn("status", resources[0]["observed_fields"])
        self.assertTrue(all(item["available"] for item in resources))

    def test_field_catalog_caches_openstack_resource_discovery(self) -> None:
        connection = Connection()
        backend = OpenStackBackend(
            credentials={"OS_AUTH_URL": "https://identity.example/v3", "OS_TOKEN": "token", "OS_PROJECT_ID": "project-1"},
            connection_factory=lambda _credentials: connection,
        )

        result = backend.call_tool("discover_resources", {"resources": ["server"]})
        with TemporaryDirectory() as tmp:
            with patch("app.field_cache.FIELD_CACHE_DIR", Path(tmp)):
                catalog = update_catalog_from_tool_result("openstack", "openstack", "discover_resources", result)
                loaded = load_field_catalog("openstack")

        self.assertIsNotNone(catalog)
        self.assertEqual(loaded["resource_count"], 1)
        self.assertIn("status", {field["path"] for field in loaded["resources"]["server"]["fields"]})


if __name__ == "__main__":
    unittest.main()
