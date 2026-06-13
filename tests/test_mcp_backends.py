from __future__ import annotations

import unittest

from app.mcp.netbox import NetBoxBackend
from app.mcp.openstack import OpenStackBackend


class FakeHTTPResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.status_code = 200
        self.content = b"{}"
        self.text = "{}"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class FakeHTTPClient:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeHTTPResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeHTTPResponse(self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)])

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


class McpBackendTests(unittest.TestCase):
    def test_netbox_fields_use_native_filter_and_response_cache(self) -> None:
        backend = NetBoxBackend("https://netbox.example", "token")
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

    def test_netbox_rejects_cross_origin_and_write_calls(self) -> None:
        backend = NetBoxBackend("https://netbox.example", "")
        self.addCleanup(backend.close)

        with self.assertRaisesRegex(ValueError, "configured origin"):
            backend.call_tool("call_endpoint", {"path": "https://attacker.example/api/secrets/"})
        with self.assertRaisesRegex(ValueError, "read-only"):
            backend.call_tool("call_endpoint", {"path": "dcim/devices/", "method": "POST"})

    def test_netbox_supports_v1_and_v2_token_schemes(self) -> None:
        legacy = NetBoxBackend("https://netbox.example", "legacy-token")
        modern = NetBoxBackend("https://netbox.example", "nbt_key.secret")
        self.addCleanup(legacy.close)
        self.addCleanup(modern.close)

        self.assertEqual(legacy._headers()["Authorization"], "Token legacy-token")
        self.assertEqual(modern._headers()["Authorization"], "Bearer nbt_key.secret")

    def test_netbox_rejects_cross_origin_pagination_links(self) -> None:
        backend = NetBoxBackend("https://netbox.example", "")
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
        backend = NetBoxBackend("https://netbox.example", "token")
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

    def test_netbox_inventory_statistics_use_collection_counts(self) -> None:
        backend = NetBoxBackend("https://netbox.example", "token")
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


if __name__ == "__main__":
    unittest.main()
