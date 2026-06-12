"""OpenStack MCP server exposed through FastAPI at ``/mcp``."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "openstack-mcp-server"
SERVER_VERSION = "0.1.0"
DEFAULT_CACHE_TTL_SECONDS = 20.0


def _timeout_seconds(credentials: dict[str, str]) -> float:
    try:
        return max(5.0, min(600.0, float(credentials.get("OS_TIMEOUT", "60"))))
    except ValueError:
        return 60.0


def _is_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return True
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return "timeout" in name or "timed out" in message


def openstack_sdk_available() -> bool:
    try:
        import openstack  # noqa: F401
    except ImportError:
        return False
    return True


def discover_accessible_projects(credentials: dict[str, str]) -> list[dict[str, str]]:
    auth_url = credentials["OS_AUTH_URL"].rstrip("/")
    token = credentials.get("OS_TOKEN", "")
    timeout_seconds = _timeout_seconds(credentials)
    target = f"{auth_url}/auth/projects"
    timeout = httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds))
    try:
        response = httpx.get(
            target,
            headers={"X-Auth-Token": token, "Accept": "application/json"},
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            f"OpenStack Projekt-Ermittlung an {target} hat nach {timeout_seconds:.0f}s nicht geantwortet."
        ) from exc
    response.raise_for_status()
    payload = response.json()
    projects = payload.get("projects", []) if isinstance(payload, dict) else []
    return [
        {"id": str(project.get("id", "")), "name": str(project.get("name", ""))}
        for project in projects
        if isinstance(project, dict) and project.get("id")
    ]


def create_openstack_connection(credentials: dict[str, str]) -> Any:
    try:
        from openstack.connection import Connection
    except ImportError as exc:
        raise RuntimeError(
            "OpenStack SDK nicht installiert. Installiere Harbor erneut oder fuehre aus: "
            "python -m pip install openstacksdk"
        ) from exc

    options: dict[str, Any] = {
        "auth_url": credentials["OS_AUTH_URL"],
        "region_name": credentials.get("OS_REGION_NAME") or None,
        "interface": credentials.get("OS_INTERFACE") or None,
        "force_ipv4": True,
        "timeout": _timeout_seconds(credentials),
        "connect_retries": 1,
    }
    token = credentials.get("OS_TOKEN", "")
    project_id = credentials.get("OS_PROJECT_ID", "")
    project_name = credentials.get("OS_PROJECT_NAME", "")
    if token:
        options.update(
            {
                "auth_type": "v3token" if project_id or project_name else "token",
                "token": token,
                "project_id": project_id or None,
                "project_name": project_name if not project_id else None,
                "project_domain_name": credentials.get("OS_PROJECT_DOMAIN_NAME") if project_name and not project_id else None,
            }
        )
    elif credentials.get("OS_APPLICATION_CREDENTIAL_ID") and credentials.get("OS_APPLICATION_CREDENTIAL_SECRET"):
        options.update(
            {
                "auth_type": "v3applicationcredential",
                "application_credential_id": credentials["OS_APPLICATION_CREDENTIAL_ID"],
                "application_credential_secret": credentials["OS_APPLICATION_CREDENTIAL_SECRET"],
            }
        )
    else:
        options.update(
            {
                "auth_type": credentials.get("OS_AUTH_TYPE") or "password",
                "username": credentials.get("OS_USERNAME"),
                "password": credentials.get("OS_PASSWORD"),
                "project_id": project_id or None,
                "project_name": project_name if not project_id else None,
                "user_domain_name": credentials.get("OS_USER_DOMAIN_NAME") or "Default",
                "project_domain_name": (credentials.get("OS_PROJECT_DOMAIN_NAME") or "Default") if not project_id else None,
            }
        )
    return Connection(**{key: value for key, value in options.items() if value is not None})


def _jsonrpc_result(request_id: Any, result: dict[str, Any], *, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result}, headers=headers)


def _jsonrpc_error(
    request_id: Any,
    code: int,
    message: str,
    *,
    data: Any = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if data is not None:
        payload["error"]["data"] = data
    return JSONResponse(payload, status_code=status_code, headers=headers)


def _tool_schema() -> list[dict[str, Any]]:
    return [
        {"name": "list_servers", "description": "List OpenStack compute servers.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "status": {"type": "string"}, "project": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}}}},
        {"name": "get_server", "description": "Show one OpenStack server by id or name.", "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}}, "required": ["server"]}},
        {"name": "list_projects", "description": "List OpenStack projects.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}}}},
        {"name": "list_images", "description": "List OpenStack images.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "status": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}}}},
        {"name": "list_flavors", "description": "List OpenStack flavors.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}}}},
        {"name": "list_networks", "description": "List OpenStack networks.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}}}},
        {"name": "list_subnets", "description": "List OpenStack subnets.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "network": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}}}},
        {"name": "list_ports", "description": "List OpenStack ports.", "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}, "network": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}}}},
        {"name": "list_routers", "description": "List OpenStack routers.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}}}},
        {
            "name": "call_readonly",
            "description": "Execute a whitelisted read-only OpenStack SDK list/show operation.",
            "inputSchema": {"type": "object", "properties": {"resource": {"type": "string"}, "operation": {"type": "string", "enum": ["list", "show"]}, "target": {"type": "string"}, "filters": {"type": "object"}, "limit": {"type": "integer", "minimum": 1}}, "required": ["resource", "operation"]},
        },
    ]


@dataclass
class CachedResult:
    payload: Any
    cached_at: float


@dataclass
class OpenStackBackend:
    credentials: dict[str, str]
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS
    connection_factory: Callable[[dict[str, str]], Any] = create_openstack_connection
    project_discovery: Callable[[dict[str, str]], list[dict[str, str]]] = discover_accessible_projects
    _cache: dict[str, CachedResult] = field(default_factory=dict)
    _connection: Any = field(default=None, init=False, repr=False)

    def _get_connection(self) -> Any:
        if self._connection is None:
            connection = self.connection_factory(self.credentials)
            token = self.credentials.get("OS_TOKEN", "")
            has_explicit_scope = bool(self.credentials.get("OS_PROJECT_ID") or self.credentials.get("OS_PROJECT_NAME"))
            if token and not has_explicit_scope:
                timeout_seconds = _timeout_seconds(self.credentials)
                try:
                    connection.authorize()
                    access = connection.session.auth.get_access(connection.session)
                except Exception as exc:
                    if _is_timeout_error(exc):
                        raise RuntimeError(
                            f"OpenStack Authentifizierung an {self.credentials['OS_AUTH_URL']} "
                            f"hat nach {timeout_seconds:.0f}s nicht geantwortet."
                        ) from exc
                    raise
                if not access.has_service_catalog():
                    projects = self.project_discovery(self.credentials)
                    if len(projects) == 1:
                        scoped_credentials = {**self.credentials, "OS_PROJECT_ID": projects[0]["id"]}
                        connection = self.connection_factory(scoped_credentials)
                    elif projects:
                        choices = ", ".join(f"{project['name']} ({project['id']})" for project in projects)
                        raise RuntimeError(
                            "OpenStack Token ist ungescoped und hat Zugriff auf mehrere Projekte. "
                            f"Konfiguriere eine Projekt-ID: {choices}"
                        )
                    else:
                        raise RuntimeError(
                            "OpenStack Token ist ungescoped und Keystone liefert kein erreichbares Projekt. "
                            "Erzeuge einen projektgebundenen Token oder konfiguriere eine Projekt-ID."
                        )
            self._connection = connection
        return self._connection

    def _cached(self, operation: str, arguments: dict[str, Any], loader: Callable[[], Any]) -> Any:
        cache_key = json.dumps([operation, arguments], ensure_ascii=False, sort_keys=True, default=str)
        cached = self._cache.get(cache_key)
        if cached is not None and time.monotonic() - cached.cached_at < self.cache_ttl_seconds:
            return cached.payload
        try:
            payload = loader()
        except Exception as exc:
            if _is_timeout_error(exc):
                raise RuntimeError(
                    f"OpenStack Operation {operation} hat nach "
                    f"{_timeout_seconds(self.credentials):.0f}s nicht geantwortet. "
                    "Pruefe Service-Katalog, Region, Routing und Firewall."
                ) from exc
            raise
        self._cache[cache_key] = CachedResult(payload=payload, cached_at=time.monotonic())
        return payload

    @staticmethod
    def _serialize(resource: Any) -> dict[str, Any]:
        if isinstance(resource, dict):
            payload = resource
        elif hasattr(resource, "to_dict"):
            payload = resource.to_dict()
        else:
            payload = {
                key: value
                for key, value in vars(resource).items()
                if not key.startswith("_")
            }
        return json.loads(json.dumps(payload, ensure_ascii=False, default=str))

    def _list_resources(
        self,
        operation: str,
        arguments: dict[str, Any],
        loader: Callable[[], Any],
        *,
        field_filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._cached(operation, arguments, lambda: [self._serialize(item) for item in loader()])
        name = str(arguments.get("name", "")).strip().lower()
        if name:
            rows = [row for row in rows if name in str(row.get("name") or row.get("id") or "").lower()]
        for argument_name, field_name in (field_filters or {}).items():
            expected = str(arguments.get(argument_name, "")).strip().lower()
            if expected:
                rows = [row for row in rows if expected in str(row.get(field_name, "")).lower()]
        limit = int(arguments.get("limit", 0) or 0)
        return rows[:limit] if limit > 0 else rows

    def health(self) -> dict[str, Any]:
        return {
            "ok": openstack_sdk_available(),
            "server": SERVER_NAME,
            "backend": "openstacksdk",
            "openstack_sdk": openstack_sdk_available(),
            "auth_configured": bool(self.credentials.get("OS_AUTH_URL")),
            "timeout_seconds": _timeout_seconds(self.credentials),
            "scope_mode": "project_id"
            if self.credentials.get("OS_PROJECT_ID")
            else "project_name"
            if self.credentials.get("OS_PROJECT_NAME")
            else "catalog_or_auto",
            "credential_mode": "token"
            if self.credentials.get("OS_TOKEN")
            else "application_credential"
            if self.credentials.get("OS_APPLICATION_CREDENTIAL_ID") and self.credentials.get("OS_APPLICATION_CREDENTIAL_SECRET")
            else "password"
            if self.credentials.get("OS_USERNAME") and self.credentials.get("OS_PASSWORD")
            else "unknown",
        }

    def list_tools(self) -> list[dict[str, Any]]:
        tools = _tool_schema()
        for tool in tools:
            tool["annotations"] = {"title": tool["name"], "readOnlyHint": True}
        return tools

    def _apply_generic_filters(self, rows: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
        for key, value in filters.items():
            if value is None or value == "":
                continue
            normalized = str(value).lower()
            rows = [item for item in rows if normalized in str(item.get(key) or "").lower()]
        return rows

    def _resource_list(self, resource: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        connection = self._get_connection()
        loaders: dict[str, Callable[[], Any]] = {
            "server": lambda: connection.compute.servers(details=True),
            "project": lambda: connection.identity.projects(),
            "image": lambda: connection.image.images(),
            "flavor": lambda: connection.compute.flavors(),
            "network": lambda: connection.network.networks(),
            "subnet": lambda: connection.network.subnets(),
            "port": lambda: connection.network.ports(),
            "router": lambda: connection.network.routers(),
        }
        return self._list_resources(f"{resource}.list", arguments, loaders[resource])

    def _resource_show(self, resource: str, target: str) -> dict[str, Any]:
        connection = self._get_connection()
        finders: dict[str, Callable[[str], Any]] = {
            "server": lambda value: connection.compute.find_server(value, ignore_missing=False),
            "project": lambda value: connection.identity.find_project(value, ignore_missing=False),
            "image": lambda value: connection.image.find_image(value, ignore_missing=False),
            "flavor": lambda value: connection.compute.find_flavor(value, ignore_missing=False),
            "network": lambda value: connection.network.find_network(value, ignore_missing=False),
            "subnet": lambda value: connection.network.find_subnet(value, ignore_missing=False),
            "port": lambda value: connection.network.find_port(value, ignore_missing=False),
            "router": lambda value: connection.network.find_router(value, ignore_missing=False),
        }
        return self._cached(f"{resource}.show", {"target": target}, lambda: self._serialize(finders[resource](target)))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        connection = self._get_connection()
        if name == "list_servers":
            payload = self._list_resources(
                "server.list",
                arguments,
                lambda: connection.compute.servers(details=True),
                field_filters={"status": "status", "project": "project_id"},
            )
            return self._tool_result(name, arguments, payload)
        if name == "get_server":
            payload = self._resource_show("server", str(arguments["server"]).strip())
            return self._tool_result(name, arguments, payload)
        if name == "list_projects":
            return self._tool_result(name, arguments, self._resource_list("project", arguments))
        if name == "list_images":
            payload = self._list_resources(
                "image.list",
                arguments,
                lambda: connection.image.images(),
                field_filters={"status": "status"},
            )
            return self._tool_result(name, arguments, payload)
        if name == "list_flavors":
            return self._tool_result(name, arguments, self._resource_list("flavor", arguments))
        if name == "list_networks":
            return self._tool_result(name, arguments, self._resource_list("network", arguments))
        if name == "list_subnets":
            payload = self._list_resources(
                "subnet.list",
                arguments,
                lambda: connection.network.subnets(),
                field_filters={"network": "network_id"},
            )
            return self._tool_result(name, arguments, payload)
        if name == "list_ports":
            payload = self._list_resources(
                "port.list",
                arguments,
                lambda: connection.network.ports(),
                field_filters={"server": "device_id", "network": "network_id"},
            )
            return self._tool_result(name, arguments, payload)
        if name == "list_routers":
            return self._tool_result(name, arguments, self._resource_list("router", arguments))
        if name in {"call_readonly", "call_cli_readonly"}:
            resource = str(arguments["resource"]).strip().lower()
            operation = str(arguments["operation"]).strip().lower()
            allowed_resources = {"server", "project", "image", "flavor", "network", "subnet", "port", "router"}
            if resource not in allowed_resources:
                raise ValueError("Resource ist nicht freigegeben.")
            if operation == "show":
                target = str(arguments.get("target", "")).strip()
                if not target:
                    raise ValueError("target ist fuer show erforderlich.")
                payload = self._resource_show(resource, target)
            elif operation == "list":
                filters = arguments.get("filters")
                payload = self._resource_list(resource, {"limit": arguments.get("limit")})
                if filters is not None:
                    if not isinstance(filters, dict):
                        raise ValueError("filters muss ein Objekt sein.")
                    payload = self._apply_generic_filters(payload, {str(key): value for key, value in filters.items()})
            else:
                raise ValueError("Nur list/show ist erlaubt.")
            return self._tool_result(name, arguments, payload)
        raise ValueError(f"Unknown tool: {name}")

    def _tool_result(self, tool_name: str, arguments: dict[str, Any], payload: Any) -> dict[str, Any]:
        summary = {"tool": tool_name, "arguments": arguments, "data": payload}
        return {"content": [{"type": "text", "text": f"{tool_name} completed successfully."}], "structuredContent": summary}


def create_app(credentials: dict[str, str]) -> FastAPI:
    backend = OpenStackBackend(credentials=credentials)
    sessions: set[str] = set()
    app = FastAPI(title="OpenStack MCP Server")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return backend.health()

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception as exc:
            return _jsonrpc_error(None, -32700, "Parse error", data=str(exc), status_code=400)
        if not isinstance(payload, dict):
            return _jsonrpc_error(None, -32600, "Invalid Request", status_code=400)
        request_id = payload.get("id")
        if payload.get("jsonrpc") != "2.0":
            return _jsonrpc_error(request_id, -32600, "Invalid Request", data="Expected jsonrpc='2.0'.", status_code=400)
        method = payload.get("method")
        params = payload.get("params") or {}
        session_id = request.headers.get("mcp-session-id")
        session_headers = {"mcp-session-id": session_id} if session_id else None
        if not isinstance(method, str) or not method:
            return _jsonrpc_error(request_id, -32600, "Invalid Request", data="Missing method.", status_code=400)
        if not isinstance(params, dict):
            return _jsonrpc_error(request_id, -32602, "Invalid params", data="Expected params to be an object.", status_code=400, headers=session_headers)
        try:
            if method == "initialize":
                session_id = str(uuid4())
                sessions.add(session_id)
                return _jsonrpc_result(request_id, {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}, headers={"mcp-session-id": session_id})
            if method == "notifications/initialized":
                headers = {"mcp-session-id": session_id} if session_id else None
                return Response(status_code=202, headers=headers)
            if session_id and session_id not in sessions:
                return _jsonrpc_error(request_id, -32001, "Unknown MCP session", status_code=400)
            if method == "tools/list":
                return _jsonrpc_result(request_id, {"tools": backend.list_tools()}, headers=session_headers)
            if method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(tool_name, str) or not tool_name:
                    return _jsonrpc_error(request_id, -32602, "Invalid params", data="Tool name is required.", status_code=400, headers=session_headers)
                if not isinstance(arguments, dict):
                    return _jsonrpc_error(request_id, -32602, "Invalid params", data="Tool arguments must be an object.", status_code=400, headers=session_headers)
                return _jsonrpc_result(request_id, backend.call_tool(tool_name, arguments), headers=session_headers)
            return _jsonrpc_error(request_id, -32601, "Method not found", headers=session_headers)
        except KeyError as exc:
            return _jsonrpc_error(request_id, -32602, "Invalid params", data=f"Missing required argument: {exc.args[0]}", status_code=400, headers=session_headers)
        except ValueError as exc:
            return _jsonrpc_error(request_id, -32602, "Invalid params", data=str(exc), status_code=400, headers=session_headers)
        except Exception as exc:
            return _jsonrpc_error(request_id, -32000, "Server error", data=str(exc), headers=session_headers)

    return app


__all__ = ["create_app", "create_openstack_connection", "OpenStackBackend", "MCP_PROTOCOL_VERSION"]
