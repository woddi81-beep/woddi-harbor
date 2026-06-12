"""OpenStack MCP server exposed through FastAPI at ``/mcp``."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "openstack-mcp-server"
SERVER_VERSION = "0.1.0"
DEFAULT_CACHE_TTL_SECONDS = 20.0


def resolve_openstack_cli() -> str:
    configured = os.getenv("OPENSTACK_CLI", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.append(Path(sys.executable).resolve().with_name("openstack"))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("openstack") or ""


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
            "name": "call_cli_readonly",
            "description": "Execute a whitelisted read-only OpenStack CLI list/show command.",
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
    _cache: dict[str, CachedResult] = field(default_factory=dict)

    def _base_env(self) -> dict[str, str]:
        import os

        env = os.environ.copy()
        for key, value in self.credentials.items():
            if value:
                env[key] = value
        default_auth_type = "token" if self.credentials.get("OS_TOKEN") else "v3applicationcredential"
        env.setdefault("OS_AUTH_TYPE", self.credentials.get("OS_AUTH_TYPE") or default_auth_type)
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def _cache_key(self, command: list[str]) -> str:
        return json.dumps(command, ensure_ascii=False, separators=(",", ":"))

    def _run_openstack(self, args: list[str], *, timeout: float = 30.0, use_cache: bool = True) -> Any:
        binary = resolve_openstack_cli()
        if not binary:
            expected = Path(sys.executable).resolve().with_name("openstack")
            raise RuntimeError(
                f"OpenStack CLI nicht gefunden. Installiere sie mit: {sys.executable} -m pip install python-openstackclient "
                f"(erwarteter Pfad: {expected})"
            )
        command = [binary, *args, "-f", "json"]
        cache_key = self._cache_key(command)
        if use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None and time.monotonic() - cached.cached_at < self.cache_ttl_seconds:
                return cached.payload
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=self._base_env(),
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"OpenStack CLI exit {completed.returncode}")
        try:
            payload = json.loads(completed.stdout.strip() or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenStack CLI lieferte kein gueltiges JSON: {exc}") from exc
        if use_cache:
            self._cache[cache_key] = CachedResult(payload=payload, cached_at=time.monotonic())
        return payload

    def _limit_rows(self, payload: Any, limit: int | None) -> Any:
        if limit is None or not isinstance(payload, list):
            return payload
        return payload[:limit]

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "server": SERVER_NAME,
            "openstack_cli": resolve_openstack_cli(),
            "auth_configured": bool(self.credentials.get("OS_AUTH_URL")),
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

    def _apply_name_filter(self, payload: Any, name: str | None) -> Any:
        if not name or not isinstance(payload, list):
            return payload
        normalized = name.lower()
        return [item for item in payload if isinstance(item, dict) and normalized in str(item.get("Name") or item.get("name") or item.get("ID") or "").lower()]

    def _apply_generic_filters(self, payload: Any, filters: dict[str, Any]) -> Any:
        if not isinstance(payload, list):
            return payload
        rows = payload
        for key, value in filters.items():
            if value is None or value == "":
                continue
            normalized = str(value).lower()
            rows = [item for item in rows if isinstance(item, dict) and normalized in str(item.get(key) or item.get(key.title()) or "").lower()]
        return rows

    def _call_list(self, noun: str, arguments: dict[str, Any], *, extra_args: list[str] | None = None, filter_keys: dict[str, str] | None = None) -> Any:
        payload = self._run_openstack([noun, "list", *(extra_args or [])])
        rows = payload
        if isinstance(filter_keys, dict):
            mapped_filters = {filter_keys[key]: value for key, value in arguments.items() if key in filter_keys}
            rows = self._apply_generic_filters(rows, mapped_filters)
        rows = self._apply_name_filter(rows, str(arguments.get("name", "")).strip() or None)
        return self._limit_rows(rows, int(arguments["limit"]) if "limit" in arguments and arguments.get("limit") else None)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "list_servers":
            payload = self._call_list("server", arguments, filter_keys={"status": "Status", "project": "Project ID"})
            return self._tool_result(name, arguments, payload)
        if name == "get_server":
            payload = self._run_openstack(["server", "show", str(arguments["server"]).strip()], use_cache=True)
            return self._tool_result(name, arguments, payload)
        if name == "list_projects":
            return self._tool_result(name, arguments, self._call_list("project", arguments))
        if name == "list_images":
            return self._tool_result(name, arguments, self._call_list("image", arguments, filter_keys={"status": "Status"}))
        if name == "list_flavors":
            return self._tool_result(name, arguments, self._call_list("flavor", arguments))
        if name == "list_networks":
            return self._tool_result(name, arguments, self._call_list("network", arguments))
        if name == "list_subnets":
            return self._tool_result(name, arguments, self._call_list("subnet", arguments, filter_keys={"network": "Network"}))
        if name == "list_ports":
            return self._tool_result(name, arguments, self._call_list("port", arguments, filter_keys={"server": "Device ID", "network": "Network"}))
        if name == "list_routers":
            return self._tool_result(name, arguments, self._call_list("router", arguments))
        if name == "call_cli_readonly":
            resource = str(arguments["resource"]).strip().lower()
            operation = str(arguments["operation"]).strip().lower()
            allowed_resources = {"server", "project", "image", "flavor", "network", "subnet", "port", "router"}
            if resource not in allowed_resources:
                raise ValueError("Resource ist nicht freigegeben.")
            if operation == "show":
                target = str(arguments.get("target", "")).strip()
                if not target:
                    raise ValueError("target ist fuer show erforderlich.")
                payload = self._run_openstack([resource, "show", target], use_cache=True)
            elif operation == "list":
                filters = arguments.get("filters")
                payload = self._call_list(resource, {"limit": arguments.get("limit")})
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
        except subprocess.TimeoutExpired as exc:
            return _jsonrpc_error(request_id, -32002, "OpenStack request timed out", data=str(exc), headers=session_headers)
        except Exception as exc:
            return _jsonrpc_error(request_id, -32000, "Server error", data=str(exc), headers=session_headers)

    return app


__all__ = ["create_app", "OpenStackBackend", "MCP_PROTOCOL_VERSION"]
