from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import modules as modules_module
from app.config import ModuleConfig
from app import cli
from app.modules import discover_standard_mcp_module, execute_module, module_test, validation_errors_by_module, worker_execute
from app.worker import ExecuteRequest, create_worker_app
from app.worker_netbox import create_worker_app as create_netbox_worker_app
from app.worker_netbox import run_worker as run_netbox_worker
from app.worker_openstack import create_worker_app as create_openstack_worker_app


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

    def post(self, url: str, *, json: dict | None = None) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, "json": json or {}})
        return FakeResponse({"ok": self.execute_status_code == 200}, status_code=self.execute_status_code)


class ModuleTests(unittest.TestCase):
    def test_worker_health_endpoint_does_not_call_module_status(self) -> None:
        module = ModuleConfig(id="docs-local", type="docs", transport="local", path="/tmp/docs", port=41001)
        captured: dict[str, object] = {}

        def fake_run(app, **kwargs) -> None:
            captured["app"] = app
            captured["kwargs"] = kwargs

        with (
            patch("app.cli.find_module", return_value=module),
            patch("app.cli.uvicorn.run", side_effect=fake_run),
            patch("app.cli.module_status", side_effect=AssertionError("module_status must not be used for worker /health")),
            patch("app.modules.load_index", side_effect=AssertionError("load_index must not be used for worker /health")),
        ):
            cli.worker("docs-local")

        app = captured["app"]
        response = TestClient(app).get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["module_id"], "docs-local")
        self.assertTrue(response.json()["ready"])

    def test_create_worker_app_health_endpoint_does_not_call_module_status(self) -> None:
        module = ModuleConfig(id="docs-local", type="docs", transport="local", path="/tmp/docs", port=41001)
        with (
            patch("app.worker.find_module", return_value=module),
            patch("app.cli.module_status", side_effect=AssertionError("module_status must not be used for worker /health")),
            patch("app.modules.load_index", side_effect=AssertionError("load_index must not be used for worker /health")),
        ):
            app = create_worker_app("docs-local")

        response = TestClient(app).get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["module_id"], "docs-local")
        self.assertTrue(response.json()["ready"])

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
            response = TestClient(app).post("/execute", json={"action": "search", "payload": {"query": "test"}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "data": {"hits": []}})
        execute_patch.assert_called_once_with(module, "search", {"query": "test"})

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

        def fake_create_app(*, netbox_url: str, netbox_token: str):
            captured["netbox_url"] = netbox_url
            captured["netbox_token"] = netbox_token
            return object()

        with (
            patch("app.worker_netbox.find_module", return_value=module),
            patch("app.worker_netbox.create_app", side_effect=fake_create_app),
            patch.dict("os.environ", {"NETBOX_URL": "https://netbox.example", "NETBOX_TOKEN": "secret"}, clear=False),
        ):
            app = create_netbox_worker_app("netbox")

        self.assertIsNotNone(app)
        self.assertEqual(captured["netbox_url"], "https://netbox.example")
        self.assertEqual(captured["netbox_token"], "secret")

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

    def test_create_openstack_worker_app_uses_env_credentials(self) -> None:
        module = ModuleConfig(id="openstack", type="openstack_mcp", transport="local", port=41003)
        captured: dict[str, object] = {}

        def fake_create_app(credentials: dict[str, str]):
            captured["credentials"] = credentials
            return object()

        with (
            patch("app.worker_openstack.find_module", return_value=module),
            patch("app.worker_openstack.create_app", side_effect=fake_create_app),
            patch.dict(
                "os.environ",
                {
                    "OS_AUTH_URL": "https://openstack.example/v3",
                    "OS_APPLICATION_CREDENTIAL_ID": "abc",
                    "OS_APPLICATION_CREDENTIAL_SECRET": "def",
                },
                clear=False,
            ),
        ):
            app = create_openstack_worker_app("openstack")

        self.assertIsNotNone(app)
        credentials = captured["credentials"]
        self.assertEqual(credentials["OS_AUTH_URL"], "https://openstack.example/v3")
        self.assertEqual(credentials["OS_APPLICATION_CREDENTIAL_ID"], "abc")

    def test_validation_errors_by_module_detects_duplicate_ports(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            modules = [
                ModuleConfig(id="docs-a", type="docs", transport="local", path=first_dir, port=41000),
                ModuleConfig(id="docs-b", type="docs", transport="local", path=second_dir, port=41000),
            ]
            errors = validation_errors_by_module(modules)
        self.assertIn("Port-Konflikt", " ".join(errors["docs-a"]))
        self.assertIn("Port-Konflikt", " ".join(errors["docs-b"]))

    def test_worker_execute_uses_query_cache_for_repeated_docs_searches(self) -> None:
        with tempfile.TemporaryDirectory() as docs_dir:
            module = ModuleConfig(id="docs-cache", type="docs", transport="local", path=docs_dir, port=41004, top_k=5)
            with open(f"{docs_dir}/readme.md", "w", encoding="utf-8") as handle:
                handle.write("router alpha beta\n")

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
