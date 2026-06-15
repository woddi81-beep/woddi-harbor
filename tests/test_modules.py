from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI

from app import cli
from app import modules as modules_module
from app.config import (
    ModuleConfig,
    load_module_named_secret,
    load_user_named_secret,
    save_module_named_secret,
    save_user_named_secret,
)
from app.mcp.netbox import NetBoxBackend
from app.mcp.openstack import OpenStackBackend, create_openstack_connection
from app.modules import (
    discover_standard_mcp_module,
    execute_module,
    module_diagnostics,
    module_test,
    validation_errors_by_module,
    worker_execute,
)
from app.worker import ExecuteRequest, create_worker_app
from app.worker_netbox import create_worker_app as create_netbox_worker_app
from app.worker_netbox import run_worker as run_netbox_worker
from app.worker_openstack import create_worker_app as create_openstack_worker_app
from app.worker_sap_docs import create_worker_app as create_sap_docs_worker_app


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


class FakeUnavailableNetBoxClient(FakeMcpClient):
    def post(self, url: str, *, headers: dict | None = None, json: dict | None = None) -> FakeResponse:
        if (json or {}).get("method") != "tools/list":
            return super().post(url, headers=headers, json=json)
        return FakeResponse(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [
                        {
                            "name": "get_objects",
                            "annotations": {
                                "discovery": {
                                    "source": "unavailable",
                                    "error": "Temporary failure in name resolution",
                                }
                            },
                        }
                    ]
                },
            },
            headers={"mcp-session-id": "session-1"},
        )


class FakeWorkerHealthClient:
    execute_status_code = 200

    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[dict] = []

    def __enter__(self) -> FakeWorkerHealthClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, url: str) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url})
        return FakeResponse({"module_id": "10", "ready": True})

    def post(self, url: str, *, headers: dict | None = None, json: dict | None = None) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, "headers": headers or {}, "json": json or {}})
        return FakeResponse({"ok": self.execute_status_code == 200}, status_code=self.execute_status_code)


class FakeOpenStackResource:
    def __init__(self, **payload: object) -> None:
        self.payload = payload

    def to_dict(self) -> dict[str, object]:
        return self.payload


class FakeOpenStackCompute:
    def servers(self, *, details: bool = False):
        assert details
        return [
            FakeOpenStackResource(id="vm-1", name="prod-api-01", status="ACTIVE", project_id="project-1"),
            FakeOpenStackResource(id="vm-2", name="test-api-01", status="SHUTOFF", project_id="project-2"),
        ]

    def find_server(self, value: str, *, ignore_missing: bool):
        assert not ignore_missing
        return FakeOpenStackResource(id="vm-1", name=value, status="ACTIVE")

    def flavors(self):
        return [FakeOpenStackResource(id="flavor-1", name="m1.small", ram=2048)]

    def find_flavor(self, value: str, *, ignore_missing: bool):
        assert not ignore_missing
        return FakeOpenStackResource(id="flavor-1", name=value)


class FakeOpenStackIdentity:
    def projects(self):
        return [FakeOpenStackResource(id="project-1", name="production")]

    def find_project(self, value: str, *, ignore_missing: bool):
        assert not ignore_missing
        return FakeOpenStackResource(id="project-1", name=value)


class FakeOpenStackImage:
    def images(self):
        return [FakeOpenStackResource(id="image-1", name="ubuntu", status="active")]

    def find_image(self, value: str, *, ignore_missing: bool):
        assert not ignore_missing
        return FakeOpenStackResource(id="image-1", name=value)


class FakeOpenStackNetwork:
    def networks(self):
        return [FakeOpenStackResource(id="network-1", name="private")]

    def subnets(self):
        return [FakeOpenStackResource(id="subnet-1", name="private-v4", network_id="network-1")]

    def ports(self):
        return [FakeOpenStackResource(id="port-1", name="", network_id="network-1", device_id="vm-1")]

    def routers(self):
        return [FakeOpenStackResource(id="router-1", name="edge")]

    def find_network(self, value: str, *, ignore_missing: bool):
        assert not ignore_missing
        return FakeOpenStackResource(id="network-1", name=value)

    def find_subnet(self, value: str, *, ignore_missing: bool):
        assert not ignore_missing
        return FakeOpenStackResource(id="subnet-1", name=value)

    def find_port(self, value: str, *, ignore_missing: bool):
        assert not ignore_missing
        return FakeOpenStackResource(id="port-1", name=value)

    def find_router(self, value: str, *, ignore_missing: bool):
        assert not ignore_missing
        return FakeOpenStackResource(id="router-1", name=value)


class FakeOpenStackAccess:
    def __init__(self, has_catalog: bool = True, *, project_id: str = "project-1", project_name: str = "production") -> None:
        self._has_catalog = has_catalog
        self.project_id = project_id if has_catalog else ""
        self.project_name = project_name if has_catalog else ""
        self.project_scoped = bool(self.project_id)

    def has_service_catalog(self) -> bool:
        return self._has_catalog


class FakeOpenStackAuth:
    def __init__(self, has_catalog: bool = True) -> None:
        self.access = FakeOpenStackAccess(has_catalog)

    def get_access(self, _session: object) -> FakeOpenStackAccess:
        return self.access


class FakeOpenStackSession:
    def __init__(self, has_catalog: bool = True) -> None:
        self.auth = FakeOpenStackAuth(has_catalog)


class FakeOpenStackConnection:
    def __init__(self, *, has_catalog: bool = True) -> None:
        self.compute = FakeOpenStackCompute()
        self.identity = FakeOpenStackIdentity()
        self.image = FakeOpenStackImage()
        self.network = FakeOpenStackNetwork()
        self.session = FakeOpenStackSession(has_catalog)

    def authorize(self) -> str:
        return "token"


class ModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker_token = patch.dict("os.environ", {"HARBOR_INTERNAL_WORKER_TOKEN": "test-token"}, clear=False)
        self.worker_token.start()
        self.addCleanup(self.worker_token.stop)

    def test_worker_health_endpoint_does_not_call_module_status(self) -> None:
        module = ModuleConfig(id="docs-local", type="docs", transport="local", path="/tmp/docs", port=41001)
        captured: dict[str, object] = {}

        def fake_run(app, **kwargs) -> None:
            captured["app"] = app
            captured["kwargs"] = kwargs

        with (
            patch("app.worker.find_module", return_value=module),
            patch("app.worker.uvicorn.run", side_effect=fake_run),
            patch("app.cli.module_status", side_effect=AssertionError("module_status must not be used for worker /health")),
            patch.dict("os.environ", {"HARBOR_INTERNAL_WORKER_TOKEN": "test-token"}, clear=False),
        ):
            cli.worker("docs-local")

        app = captured["app"]
        health_route = next(route for route in app.routes if route.path == "/health")
        response = health_route.endpoint()
        self.assertEqual(response["module_id"], "docs-local")
        self.assertTrue(response["ready"])

    def test_create_worker_app_health_endpoint_does_not_call_module_status(self) -> None:
        module = ModuleConfig(id="docs-local", type="docs", transport="local", path="/tmp/docs", port=41001)
        with (
            patch("app.worker.find_module", return_value=module),
            patch("app.cli.module_status", side_effect=AssertionError("module_status must not be used for worker /health")),
        ):
            app = create_worker_app("docs-local")

        health_route = next(route for route in app.routes if route.path == "/health")
        response = health_route.endpoint()
        self.assertEqual(response["module_id"], "docs-local")
        self.assertTrue(response["ready"])

    def test_create_worker_app_execute_endpoint_accepts_action_payload_body(self) -> None:
        module = ModuleConfig(id="docs-local", type="docs", transport="local", path="/tmp/docs", port=41001)
        with (
            patch("app.worker.find_module", return_value=module),
            patch("app.worker.worker_execute", return_value={"ok": True, "data": {"pong": True}}) as execute_patch,
        ):
            app = create_worker_app("docs-local")
            execute_route = next(route for route in app.routes if route.path == "/execute")
            self.assertIn("POST", execute_route.methods)
            response = execute_route.endpoint(ExecuteRequest(action=" health ", payload={}))

        self.assertEqual(response, {"ok": True, "data": {"pong": True}})
        execute_patch.assert_called_once_with(module, "health", {})

    def test_create_worker_app_execute_endpoint_is_available_at_root(self) -> None:
        module = ModuleConfig(id="docs-local", type="docs", transport="local", path="/tmp/docs", port=41001)
        with (
            patch("app.worker.find_module", return_value=module),
            patch("app.worker.worker_execute", return_value={"ok": True, "data": {"hits": []}}) as execute_patch,
        ):
            app = create_worker_app("docs-local")
            execute_route = next(route for route in app.routes if route.path == "/execute")
            self.assertIn("POST", execute_route.methods)
            response = execute_route.endpoint(ExecuteRequest(action="search", payload={"query": "test"}))

        self.assertEqual(response, {"ok": True, "data": {"hits": []}})
        execute_patch.assert_called_once_with(module, "search", {"query": "test"})

    def test_execute_local_docs_directly_without_http_worker(self) -> None:
        module = ModuleConfig(id="10", type="docs", transport="local", path="/tmp/docs")
        with (
            patch("app.modules.find_module", return_value=module),
            patch("app.modules.worker_execute", return_value={"ok": True, "data": {"hits": []}}) as direct_execute,
            patch("app.modules.httpx.Client", side_effect=AssertionError("HTTP worker must not be used")),
        ):
            result = execute_module("10", "search", {"query": "installation"})

        self.assertEqual(result, {"ok": True, "data": {"hits": []}})
        direct_execute.assert_called_once_with(module, "search", {"query": "installation"})

    def test_workerless_docs_module_is_valid_without_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            module = ModuleConfig(id="10", type="docs", transport="local", path=temporary, port=0)

            errors = validation_errors_by_module([module])

        self.assertEqual(errors["10"], [])

    @patch("app.modules.update_module_runtime_state", lambda *args, **kwargs: {})
    @patch("app.modules.httpx.Client", FakeWorkerHealthClient)
    def test_module_health_reachable_requires_execute_endpoint_for_local_worker(self) -> None:
        module = ModuleConfig(id="10", type="docs", transport="local", host="127.0.0.1", port=41001)
        FakeWorkerHealthClient.execute_status_code = 404

        self.assertFalse(modules_module._module_health_reachable(module))

        FakeWorkerHealthClient.execute_status_code = 200
        self.assertTrue(modules_module._module_health_reachable(module))

    def test_create_netbox_worker_app_uses_env_credentials(self) -> None:
        module = ModuleConfig(id="netbox", type="netbox_mcp", transport="local", port=41002)
        captured: dict[str, object] = {}

        def fake_create_app(*, netbox_url: str):
            captured["netbox_url"] = netbox_url
            return FastAPI()

        with (
            patch("app.worker_netbox.find_module", return_value=module),
            patch("app.worker_netbox.create_app", side_effect=fake_create_app),
            patch.dict("os.environ", {"NETBOX_URL": "https://netbox.example"}, clear=False),
        ):
            app = create_netbox_worker_app("netbox")

        self.assertIsNotNone(app)
        self.assertEqual(captured["netbox_url"], "https://netbox.example")

    def test_netbox_module_validation_allows_empty_token(self) -> None:
        module = ModuleConfig(
            id="netbox",
            type="netbox_mcp",
            transport="local",
            port=41002,
            settings={"netbox_url": "https://netbox.example", "netbox_token": ""},
        )

        self.assertEqual(modules_module.validate_module_config(module), [])

    def test_create_netbox_worker_app_allows_empty_token(self) -> None:
        module = ModuleConfig(id="netbox", type="netbox_mcp", transport="local", port=41002)
        captured: dict[str, object] = {}

        def fake_create_app(*, netbox_url: str):
            captured["netbox_url"] = netbox_url
            return FastAPI()

        with (
            patch("app.worker_netbox.find_module", return_value=module),
            patch("app.worker_netbox.create_app", side_effect=fake_create_app),
            patch.dict(
                "os.environ",
                {
                    "NETBOX_URL": "https://netbox.example",
                    "HARBOR_INTERNAL_WORKER_TOKEN": "test-token",
                },
                clear=True,
            ),
        ):
            app = create_netbox_worker_app("netbox")

        self.assertIsNotNone(app)
        self.assertEqual(captured["netbox_url"], "https://netbox.example")

    def test_netbox_backend_omits_authorization_header_without_token(self) -> None:
        backend = NetBoxBackend(netbox_url="https://netbox.example")

        self.assertNotIn("Authorization", backend._headers())

    def test_run_netbox_worker_uses_uvicorn_server_with_signal_handlers(self) -> None:
        module = ModuleConfig(id="netbox", type="netbox_mcp", transport="local", host="127.0.0.1", port=41002)
        captured: dict[str, object] = {}

        class FakeServer:
            def __init__(self, config) -> None:
                captured["config"] = config
                self.should_exit = False
                self.force_exit = False

            def run(self) -> None:
                captured["run_called"] = True

        def fake_signal(signum, handler):
            handlers = captured.setdefault("handlers", {})
            previous = f"previous-{signum}"
            handlers[signum] = handler
            return previous

        with (
            patch("app.worker_netbox.find_module", return_value=module),
            patch("app.worker_netbox.create_worker_app", return_value=object()),
            patch("app.worker_netbox.uvicorn.Server", FakeServer),
            patch("app.worker_netbox.signal.getsignal", side_effect=lambda signum: f"previous-{signum}"),
            patch("app.worker_netbox.signal.signal", side_effect=fake_signal) as signal_patch,
        ):
            run_netbox_worker("netbox", 59999)

        config = captured["config"]
        self.assertTrue(captured["run_called"])
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 59999)
        self.assertEqual(signal_patch.call_count, 4)

    def test_create_sap_docs_worker_app_uses_configured_docs_url(self) -> None:
        module = ModuleConfig(id="sap_docs", type="sap_docs_mcp", transport="local", port=41005, settings={"docs_url": "https://help.example"})
        captured: dict[str, str] = {}

        def fake_create_app(*, base_url: str = ""):
            captured["base_url"] = base_url
            return FastAPI()

        with (
            patch("app.worker_sap_docs.find_module", return_value=module),
            patch("app.worker_sap_docs.create_sap_docs_app", side_effect=fake_create_app),
        ):
            app = create_sap_docs_worker_app("sap_docs")

        self.assertIsNotNone(app)
        self.assertEqual(captured["base_url"], "https://help.example")

    def test_sap_docs_module_validation_requires_only_docs_url(self) -> None:
        module = ModuleConfig(
            id="sap_docs",
            type="sap_docs_mcp",
            transport="local",
            port=41005,
            settings={"docs_url": "https://help.sap.com/docs/SAP_Cloud_Infrastructure"},
        )

        self.assertEqual(modules_module.validate_module_config(module), [])

    def test_create_openstack_worker_app_starts_without_shared_cloud_credentials(self) -> None:
        module = ModuleConfig(id="openstack", type="openstack_mcp", transport="local", port=41003)
        captured: dict[str, object] = {}

        def fake_create_app(credentials: dict[str, str]):
            captured["credentials"] = credentials
            return FastAPI()

        with (
            patch("app.worker_openstack.find_module", return_value=module),
            patch("app.worker_openstack.create_app", side_effect=fake_create_app),
            patch.dict(
                "os.environ",
                {
                    "OS_AUTH_URL": "https://openstack.example/v3",
                    "OS_APPLICATION_CREDENTIAL_ID": "abc",
                    "OS_APPLICATION_CREDENTIAL_SECRET": "def",
                    "HARBOR_INTERNAL_WORKER_TOKEN": "worker-token",
                },
                clear=True,
            ),
        ):
            app = create_openstack_worker_app("openstack")

        self.assertIsNotNone(app)
        credentials = captured["credentials"]
        self.assertEqual(credentials["OS_TOKEN"], "")
        self.assertNotIn("OS_APPLICATION_CREDENTIAL_ID", credentials)

    def test_create_openstack_worker_app_drops_environment_token(self) -> None:
        module = ModuleConfig(id="openstack", type="openstack_mcp", transport="local", port=41003)
        captured: dict[str, object] = {}

        def fake_create_app(credentials: dict[str, str]):
            captured["credentials"] = credentials
            return FastAPI()

        with (
            patch("app.worker_openstack.find_module", return_value=module),
            patch("app.worker_openstack.create_app", side_effect=fake_create_app),
            patch.dict(
                "os.environ",
                {
                    "OS_AUTH_URL": "https://openstack.example/v3",
                    "OS_AUTH_TYPE": "token",
                    "OS_TOKEN": "token-value",
                    "HARBOR_INTERNAL_WORKER_TOKEN": "worker-token",
                },
                clear=True,
            ),
        ):
            app = create_openstack_worker_app("openstack")

        self.assertIsNotNone(app)
        credentials = captured["credentials"]
        self.assertEqual(credentials["OS_TOKEN"], "")
        self.assertEqual(credentials["OS_AUTH_TYPE"], "token")
        self.assertNotIn("OS_PROJECT_NAME", credentials)

    def test_openstack_backend_lists_servers_through_sdk(self) -> None:
        backend = OpenStackBackend(
            credentials={"OS_AUTH_URL": "https://identity.example/v3", "OS_TOKEN": "token"},
            connection_factory=lambda _credentials: FakeOpenStackConnection(),
        )

        result = backend.call_tool("list_servers", {"status": "ACTIVE", "limit": 5})

        rows = result["structuredContent"]["data"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "prod-api-01")

    def test_openstack_backend_generic_readonly_uses_sdk(self) -> None:
        backend = OpenStackBackend(
            credentials={"OS_AUTH_URL": "https://identity.example/v3", "OS_TOKEN": "token"},
            connection_factory=lambda _credentials: FakeOpenStackConnection(),
        )

        result = backend.call_tool(
            "call_readonly",
            {"resource": "network", "operation": "list", "filters": {"name": "private"}},
        )

        rows = result["structuredContent"]["data"]
        self.assertEqual(rows, [{"id": "network-1", "name": "private"}])

    def test_openstack_backend_rejects_unscoped_token(self) -> None:
        backend = OpenStackBackend(
            credentials={"OS_AUTH_URL": "https://identity.example/v3", "OS_TOKEN": "unscoped-token"},
            connection_factory=lambda _credentials: FakeOpenStackConnection(has_catalog=False),
        )

        with self.assertRaisesRegex(RuntimeError, "nicht projektgescoped"):
            backend.call_tool("list_servers", {})

    def test_openstack_backend_reports_compute_timeout_with_operation(self) -> None:
        connection = FakeOpenStackConnection()
        connection.compute.servers = lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("read timed out"))
        backend = OpenStackBackend(
            credentials={
                "OS_AUTH_URL": "https://identity.example/v3",
                "OS_TOKEN": "token",
                "OS_TIMEOUT": "90",
            },
            connection_factory=lambda _credentials: connection,
        )

        with self.assertRaisesRegex(RuntimeError, "server.list.*90s"):
            backend.call_tool("list_servers", {})

    def test_openstack_sdk_connection_preserves_project_scoped_token(self) -> None:
        connection = create_openstack_connection(
            {
                "OS_AUTH_URL": "https://identity.example/v3",
                "OS_TOKEN": "project-scoped-token",
                "OS_PROJECT_NAME": "",
                "OS_PROJECT_DOMAIN_NAME": "",
            }
        )

        self.assertEqual(connection.config.config["auth_type"], "token")
        self.assertEqual(
            connection.config.config["auth"],
            {
                "auth_url": "https://identity.example/v3",
                "token": "project-scoped-token",
            },
        )

    def test_openstack_sdk_connection_ignores_separate_project_name(self) -> None:
        connection = create_openstack_connection(
            {
                "OS_AUTH_URL": "https://identity.example/v3",
                "OS_TOKEN": "unscoped-token",
                "OS_PROJECT_NAME": "production",
                "OS_PROJECT_DOMAIN_NAME": "Default",
            }
        )

        self.assertEqual(connection.config.config["auth_type"], "token")
        self.assertEqual(
            connection.config.config["auth"],
            {"auth_url": "https://identity.example/v3", "token": "unscoped-token"},
        )

    def test_openstack_sdk_connection_ignores_separate_project_id(self) -> None:
        connection = create_openstack_connection(
            {
                "OS_AUTH_URL": "https://identity.example/v3",
                "OS_TOKEN": "unscoped-token",
                "OS_PROJECT_ID": "project-1",
                "OS_PROJECT_NAME": "",
                "OS_PROJECT_DOMAIN_NAME": "",
            }
        )

        self.assertEqual(connection.config.config["auth_type"], "token")
        self.assertNotIn("project_id", connection.config.config["auth"])
        self.assertNotIn("project_name", connection.config.config["auth"])
        self.assertEqual(connection.config.config["timeout"], "60.0")

    def test_openstack_module_validation_requires_only_shared_auth_url(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            transport="local",
            port=41003,
            settings={
                "auth_url": "https://openstack.example/v3",
                "project_name": "demo",
                "project_domain_name": "Default",
                "auth_type": "v3token",
            },
        )
        self.assertEqual(modules_module.validate_module_config(module), [])

    def test_openstack_settings_do_not_rescope_project_scoped_token(self) -> None:
        module = ModuleConfig(
            id="openstack",
            type="openstack_mcp",
            transport="local",
            settings={
                "auth_url": "https://openstack.example/v3",
                "auth_type": "v3token",
                "project_domain_name": "Default",
            },
        )
        settings = modules_module._openstack_settings(module, "token-value")

        self.assertEqual(settings["OS_AUTH_TYPE"], "token")
        self.assertEqual(settings["OS_TOKEN"], "token-value")
        self.assertNotIn("OS_PROJECT_NAME", settings)
        self.assertNotIn("OS_PROJECT_DOMAIN_NAME", settings)

    def test_module_diagnostics_reports_connection_refused_without_raising(self) -> None:
        module = ModuleConfig(
            id="netbox",
            type="netbox_mcp",
            transport="local",
            port=41002,
            settings={"netbox_url": "http://netbox.example"},
        )
        refused = ConnectionRefusedError(111, "Connection refused")
        with (
            patch("app.modules.find_module", return_value=module),
            patch("app.modules.module_status", side_effect=refused),
            patch("app.modules.health_check_module", side_effect=refused),
            patch("app.modules.discover_remote_module", side_effect=refused),
            patch("app.modules._read_module_log_tail", return_value=[]),
        ):
            result = module_diagnostics("netbox")

        self.assertFalse(result["ok"])
        self.assertIn("Connection refused", result["health"]["error"])
        self.assertIn("module start netbox", result["hint"])

    def test_module_named_secret_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as secret_dir:
            with patch("app.config.SECRETS_DIR", Path(secret_dir)):
                path = save_module_named_secret("openstack", "openstack_token", "token-value")
                self.assertEqual(load_module_named_secret("openstack", "openstack_token"), "token-value")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_user_named_secrets_are_private_and_separated(self) -> None:
        with tempfile.TemporaryDirectory() as secret_dir:
            with patch("app.config.SECRETS_DIR", Path(secret_dir)):
                alice_path = save_user_named_secret("alice", "openstack_token", "alice-token")
                bob_path = save_user_named_secret("bob", "openstack_token", "bob-token")

                self.assertNotEqual(alice_path.parent, bob_path.parent)
                self.assertEqual(load_user_named_secret("alice", "openstack_token"), "alice-token")
                self.assertEqual(load_user_named_secret("bob", "openstack_token"), "bob-token")
                self.assertEqual(alice_path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(alice_path.stat().st_mode & 0o777, 0o600)

    def test_validation_errors_by_module_detects_duplicate_ports(self) -> None:
        modules = [
            ModuleConfig(
                id="netbox-a",
                type="netbox_mcp",
                transport="local",
                port=41000,
                settings={"netbox_url": "https://netbox-a.example/api"},
            ),
            ModuleConfig(
                id="netbox-b",
                type="netbox_mcp",
                transport="local",
                port=41000,
                settings={"netbox_url": "https://netbox-b.example/api"},
            ),
        ]
        errors = validation_errors_by_module(modules)
        self.assertIn("Port-Konflikt", " ".join(errors["netbox-a"]))
        self.assertIn("Port-Konflikt", " ".join(errors["netbox-b"]))

    def test_worker_execute_uses_query_cache_for_repeated_docs_searches(self) -> None:
        with tempfile.TemporaryDirectory() as docs_dir, tempfile.TemporaryDirectory() as runtime_dir:
            module = ModuleConfig(id="docs-cache", type="docs", transport="local", path=docs_dir, port=41004, top_k=5)
            with open(f"{docs_dir}/readme.md", "w", encoding="utf-8") as handle:
                handle.write("router alpha beta\n")

            with (
                patch("app.modules.module_index_path", return_value=Path(runtime_dir) / "index.json"),
                patch("app.modules.module_query_cache_dir", return_value=Path(runtime_dir) / "query-cache"),
                patch("app.modules.load_module_runtime_state", return_value={}),
                patch("app.modules.update_module_runtime_state", return_value={}),
            ):
                first = worker_execute(module, "search", {"query": "router", "top_k": 5})
                self.assertTrue(first["ok"])
                self.assertFalse(first["data"]["cache_hit"])

                with patch("app.modules.search_index", side_effect=AssertionError("search_index should not run on cache hit")):
                    second = worker_execute(module, "search", {"query": "router", "top_k": 5})

                self.assertTrue(second["ok"])
                self.assertTrue(second["data"]["cache_hit"])

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

    @patch("app.modules.internal_worker_token", return_value="worker-token")
    @patch("app.modules.update_module_runtime_state", lambda *args, **kwargs: {})
    @patch("app.modules.httpx.Client", FakeUnavailableNetBoxClient)
    def test_discover_netbox_module_rejects_unavailable_upstream(self, _worker_token) -> None:
        module = ModuleConfig(
            id="netbox",
            type="netbox_mcp",
            provider="netbox-mcp-server",
            transport="local",
            remote_protocol="mcp",
            port=41002,
            settings={"netbox_url": "https://netbox.example"},
        )

        payload = discover_standard_mcp_module(module)

        self.assertFalse(payload["ok"])
        self.assertIn("Temporary failure in name resolution", payload["attempts"][-1]["error"])

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
