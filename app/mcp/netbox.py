"""NetBox MCP server exposed through FastAPI at ``/mcp``."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "netbox-mcp-server"
SERVER_VERSION = "0.1.0"
DEFAULT_PAGE_LIMIT = 100
DEFAULT_MAX_PAGES = 100


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


def _normalize_object_type(object_type: str) -> str:
    normalized = object_type.strip().strip("/")
    if normalized.startswith("api/"):
        normalized = normalized[4:]
    return normalized.replace(".", "/")


def _tool_schema() -> list[dict[str, Any]]:
    return [
        {
            "name": "get_objects",
            "description": "List NetBox objects for an API collection such as dcim.devices or ipam.prefixes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "object_type": {"type": "string", "description": "Collection path like dcim.devices or dcim/devices."},
                    "filters": {"type": "object", "description": "Query parameters sent to NetBox."},
                    "limit": {"type": "integer", "default": DEFAULT_PAGE_LIMIT, "minimum": 1},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                    "fetch_all": {"type": "boolean", "default": True},
                    "max_pages": {"type": "integer", "default": DEFAULT_MAX_PAGES, "minimum": 1},
                },
                "required": ["object_type"],
            },
        },
        {
            "name": "get_object_by_id",
            "description": "Fetch a single NetBox object by numeric ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "object_type": {"type": "string", "description": "Collection path like dcim.devices or dcim/devices."},
                    "id": {"type": "integer", "minimum": 1},
                },
                "required": ["object_type", "id"],
            },
        },
        {
            "name": "get_changelogs",
            "description": "Fetch the changelog feed for a single NetBox object.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "object_type": {"type": "string", "description": "Collection path like dcim.devices or dcim/devices."},
                    "id": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "default": DEFAULT_PAGE_LIMIT, "minimum": 1},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                    "fetch_all": {"type": "boolean", "default": True},
                    "max_pages": {"type": "integer", "default": DEFAULT_MAX_PAGES, "minimum": 1},
                },
                "required": ["object_type", "id"],
            },
        },
        {
            "name": "call_endpoint",
            "description": "Call any NetBox API endpoint using a relative API path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative API path, for example dcim/devices/ or dcim/devices/42/."},
                    "method": {"type": "string", "default": "GET"},
                    "params": {"type": "object", "description": "Query parameters."},
                    "json_body": {"type": "object", "description": "JSON request body for write operations."},
                    "paginate": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": DEFAULT_PAGE_LIMIT, "minimum": 1},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                    "max_pages": {"type": "integer", "default": DEFAULT_MAX_PAGES, "minimum": 1},
                },
                "required": ["path"],
            },
        },
    ]


@dataclass
class DiscoveryCache:
    schema_paths: list[str] = field(default_factory=list)
    endpoint_paths: list[str] = field(default_factory=list)
    api_root: dict[str, Any] = field(default_factory=dict)
    source: str = "static"

    def to_summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "schema_paths": self.schema_paths,
            "endpoint_paths": self.endpoint_paths,
            "api_root_sections": sorted(self.api_root.keys()),
        }


class NetBoxBackend:
    def __init__(self, netbox_url: str, netbox_token: str) -> None:
        self.base_url = netbox_url.rstrip("/")
        self.api_base = f"{self.base_url}/api/"
        self.token = netbox_token
        self._discovery_cache: DiscoveryCache | None = None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = path_or_url if path_or_url.startswith("http://") or path_or_url.startswith("https://") else urljoin(self.api_base, path_or_url.lstrip("/"))
        with httpx.Client(timeout=30.0) as client:
            response = client.request(method.upper(), url, headers=self._headers(), params=params, json=json_body)
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def _extract_paths_from_schema(self, schema: dict[str, Any]) -> list[str]:
        paths = schema.get("paths")
        if not isinstance(paths, dict):
            return []
        return sorted(path for path in paths.keys() if isinstance(path, str) and path.startswith("/api/"))

    def _extract_api_root(self, root_payload: dict[str, Any]) -> dict[str, Any]:
        extracted: dict[str, Any] = {}
        for section, value in root_payload.items():
            if not isinstance(section, str):
                continue
            if isinstance(value, str):
                extracted[section] = value
            elif isinstance(value, dict):
                extracted[section] = {key: item for key, item in value.items() if isinstance(key, str)}
        return extracted

    def discover_api_structure(self) -> DiscoveryCache:
        if self._discovery_cache is not None:
            return self._discovery_cache

        schema_candidates = [
            "/api/schema/?format=json",
            "/api/schema/?format=openapi",
            "/api/schema/",
        ]
        for candidate in schema_candidates:
            try:
                payload = self._request("GET", urljoin(self.base_url + "/", candidate.lstrip("/")))
            except Exception:
                continue
            if isinstance(payload, dict):
                schema_paths = self._extract_paths_from_schema(payload)
                if schema_paths:
                    self._discovery_cache = DiscoveryCache(
                        schema_paths=schema_paths,
                        endpoint_paths=schema_paths,
                        source=candidate,
                    )
                    return self._discovery_cache

        try:
            api_root_payload = self._request("GET", self.api_base)
        except Exception:
            self._discovery_cache = DiscoveryCache()
            return self._discovery_cache

        api_root = self._extract_api_root(api_root_payload if isinstance(api_root_payload, dict) else {})
        endpoint_paths: list[str] = []
        for section, value in api_root.items():
            if isinstance(value, str):
                endpoint_paths.append(value)
            elif isinstance(value, dict):
                endpoint_paths.extend(item for item in value.values() if isinstance(item, str))
        self._discovery_cache = DiscoveryCache(
            endpoint_paths=sorted(set(endpoint_paths)),
            api_root=api_root,
            source="/api/",
        )
        return self._discovery_cache

    def _paginate_collection(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> dict[str, Any]:
        query = dict(params or {})
        query.setdefault("limit", limit)
        query.setdefault("offset", offset)
        page_count = 0
        results: list[Any] = []
        current_target = path
        next_target: str | None = None
        first_page: dict[str, Any] | None = None

        while page_count < max_pages:
            payload = self._request("GET", next_target or current_target, params=None if next_target else query)
            if not isinstance(payload, dict) or "results" not in payload:
                return {
                    "paginated": False,
                    "count": len(payload) if isinstance(payload, list) else 1,
                    "limit": query.get("limit"),
                    "offset": query.get("offset"),
                    "results": payload if isinstance(payload, list) else [payload],
                }
            if first_page is None:
                first_page = payload
            page_results = payload.get("results")
            if not isinstance(page_results, list):
                raise ValueError("NetBox pagination payload did not contain a list in 'results'.")
            results.extend(page_results)
            page_count += 1
            next_target = payload.get("next") if isinstance(payload.get("next"), str) and payload.get("next") else None
            if not next_target:
                break
            if next_target.startswith("/"):
                next_target = urljoin(self.base_url + "/", next_target.lstrip("/"))

        total_count = 0
        next_offset = None
        previous_offset = None
        if first_page is not None:
            total_count = int(first_page.get("count") or len(results))
            next_offset = self._offset_from_url(first_page.get("next"))
            previous_offset = self._offset_from_url(first_page.get("previous"))

        return {
            "paginated": True,
            "count": total_count,
            "limit": query.get("limit"),
            "offset": query.get("offset"),
            "next_offset": next_offset,
            "previous_offset": previous_offset,
            "pages_fetched": page_count,
            "results": results,
        }

    def _offset_from_url(self, value: Any) -> int | None:
        if not isinstance(value, str) or not value:
            return None
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        offsets = query.get("offset")
        if not offsets:
            return None
        try:
            return int(offsets[0])
        except (TypeError, ValueError):
            return None

    def list_tools(self) -> list[dict[str, Any]]:
        discovery = self.discover_api_structure()
        tools = _tool_schema()
        for tool in tools:
            tool["annotations"] = {
                "title": tool["name"],
                "readOnlyHint": tool["name"] in {"get_objects", "get_object_by_id", "get_changelogs"},
                "discovery": discovery.to_summary(),
            }
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "get_objects":
            object_type = _normalize_object_type(str(arguments["object_type"]))
            filters = arguments.get("filters")
            if filters is not None and not isinstance(filters, dict):
                raise ValueError("'filters' must be an object.")
            limit = int(arguments.get("limit", DEFAULT_PAGE_LIMIT))
            offset = int(arguments.get("offset", 0))
            fetch_all = bool(arguments.get("fetch_all", True))
            max_pages = int(arguments.get("max_pages", DEFAULT_MAX_PAGES))
            path = f"{object_type.strip('/')}/"
            payload = self._paginate_collection(path, params=filters, limit=limit, offset=offset, max_pages=max_pages) if fetch_all else self._request(
                "GET",
                path,
                params={**(filters or {}), "limit": limit, "offset": offset},
            )
            return self._tool_result(name, arguments, payload)

        if name == "get_object_by_id":
            object_type = _normalize_object_type(str(arguments["object_type"]))
            object_id = int(arguments["id"])
            payload = self._request("GET", f"{object_type.strip('/')}/{object_id}/")
            return self._tool_result(name, arguments, payload)

        if name == "get_changelogs":
            object_type = _normalize_object_type(str(arguments["object_type"]))
            object_id = int(arguments["id"])
            limit = int(arguments.get("limit", DEFAULT_PAGE_LIMIT))
            offset = int(arguments.get("offset", 0))
            fetch_all = bool(arguments.get("fetch_all", True))
            max_pages = int(arguments.get("max_pages", DEFAULT_MAX_PAGES))
            path = f"{object_type.strip('/')}/{object_id}/changelog/"
            payload = self._paginate_collection(path, limit=limit, offset=offset, max_pages=max_pages) if fetch_all else self._request(
                "GET",
                path,
                params={"limit": limit, "offset": offset},
            )
            return self._tool_result(name, arguments, payload)

        if name == "call_endpoint":
            path = str(arguments["path"]).strip()
            method = str(arguments.get("method", "GET")).upper()
            params = arguments.get("params")
            if params is not None and not isinstance(params, dict):
                raise ValueError("'params' must be an object.")
            json_body = arguments.get("json_body")
            if json_body is not None and not isinstance(json_body, dict):
                raise ValueError("'json_body' must be an object.")
            paginate = bool(arguments.get("paginate", False))
            limit = int(arguments.get("limit", DEFAULT_PAGE_LIMIT))
            offset = int(arguments.get("offset", 0))
            max_pages = int(arguments.get("max_pages", DEFAULT_MAX_PAGES))
            if paginate and method != "GET":
                raise ValueError("Pagination is only supported for GET requests.")
            payload = self._paginate_collection(path, params=params, limit=limit, offset=offset, max_pages=max_pages) if paginate else self._request(
                method,
                path,
                params=params,
                json_body=json_body,
            )
            return self._tool_result(name, arguments, payload)

        raise ValueError(f"Unknown tool: {name}")

    def _tool_result(self, tool_name: str, arguments: dict[str, Any], payload: Any) -> dict[str, Any]:
        summary = {
            "tool": tool_name,
            "arguments": arguments,
            "data": payload,
        }
        return {
            "content": [
                {"type": "text", "text": f"{tool_name} completed successfully."},
            ],
            "structuredContent": summary,
        }


def create_app(netbox_url: str, netbox_token: str) -> FastAPI:
    backend = NetBoxBackend(netbox_url=netbox_url, netbox_token=netbox_token)
    sessions: set[str] = set()
    app = FastAPI(title="NetBox MCP Server")

    @app.get("/health")
    def health() -> dict[str, Any]:
        discovery = backend.discover_api_structure()
        return {
            "ok": True,
            "server": SERVER_NAME,
            "netbox_url": backend.base_url,
            "discovery": discovery.to_summary(),
        }

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
                return _jsonrpc_result(
                    request_id,
                    {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    },
                    headers={"mcp-session-id": session_id},
                )

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
        except httpx.HTTPStatusError as exc:
            detail = {
                "status_code": exc.response.status_code,
                "response": exc.response.text[:1000],
                "url": str(exc.request.url),
            }
            return _jsonrpc_error(request_id, -32002, "NetBox request failed", data=detail, headers=session_headers)
        except KeyError as exc:
            return _jsonrpc_error(request_id, -32602, "Invalid params", data=f"Missing required argument: {exc.args[0]}", status_code=400, headers=session_headers)
        except ValueError as exc:
            return _jsonrpc_error(request_id, -32602, "Invalid params", data=str(exc), status_code=400, headers=session_headers)
        except Exception as exc:
            return _jsonrpc_error(request_id, -32000, "Server error", data=str(exc), headers=session_headers)

    return app


__all__ = ["create_app", "NetBoxBackend", "MCP_PROTOCOL_VERSION"]
