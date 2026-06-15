"""NetBox MCP server exposed through FastAPI at ``/mcp``."""
from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ..cache import BoundedTTLCache, SessionRegistry

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "netbox-mcp-server"
SERVER_VERSION = "0.3.0"
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 200
DEFAULT_MAX_PAGES = 10
MAX_PAGES = 20
MAX_RESULTS = 1000
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
DISCOVERY_CACHE_TTL_SECONDS = 300.0
DEFAULT_STAT_OBJECT_TYPES = [
    "dcim.sites",
    "dcim.racks",
    "dcim.devices",
    "dcim.interfaces",
    "ipam.ip-addresses",
    "ipam.prefixes",
    "virtualization.clusters",
    "virtualization.virtual-machines",
    "tenancy.tenants",
    "circuits.circuits",
]


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
    normalized = normalized.replace(".", "/")
    if not normalized or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", normalized):
        raise ValueError("object_type must be a NetBox API collection such as dcim.devices.")
    return normalized


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int, name: str) -> int:
    try:
        parsed = int(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{name}' must be an integer.") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"'{name}' must be between {minimum} and {maximum}.")
    return parsed


def _normalize_fields(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("'fields' must be an array of field names.")
    fields: list[str] = []
    for item in value:
        field_name = str(item).strip()
        if not field_name or not re.fullmatch(r"[A-Za-z0-9_.-]+", field_name):
            raise ValueError("'fields' contains an invalid field name.")
        if field_name not in fields:
            fields.append(field_name)
    if len(fields) > 50:
        raise ValueError("'fields' supports at most 50 field names.")
    return fields


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
                    "fields": {"type": "array", "items": {"type": "string"}, "maxItems": 50, "description": "Return only these fields to reduce token usage."},
                    "limit": {"type": "integer", "default": DEFAULT_PAGE_LIMIT, "minimum": 1, "maximum": MAX_PAGE_LIMIT},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                    "fetch_all": {"type": "boolean", "default": False},
                    "max_pages": {"type": "integer", "default": DEFAULT_MAX_PAGES, "minimum": 1, "maximum": MAX_PAGES},
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
                    "fields": {"type": "array", "items": {"type": "string"}, "maxItems": 50, "description": "Return only these fields to reduce token usage."},
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
                    "limit": {"type": "integer", "default": DEFAULT_PAGE_LIMIT, "minimum": 1, "maximum": MAX_PAGE_LIMIT},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                    "fetch_all": {"type": "boolean", "default": False},
                    "max_pages": {"type": "integer", "default": DEFAULT_MAX_PAGES, "minimum": 1, "maximum": MAX_PAGES},
                },
                "required": ["object_type", "id"],
            },
        },
        {
            "name": "call_endpoint",
            "description": "Call a read-only NetBox API endpoint using a relative API path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative API path, for example dcim/devices/ or dcim/devices/42/."},
                    "method": {"type": "string", "enum": ["GET"], "default": "GET"},
                    "params": {"type": "object", "description": "Query parameters."},
                    "paginate": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": DEFAULT_PAGE_LIMIT, "minimum": 1, "maximum": MAX_PAGE_LIMIT},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                    "max_pages": {"type": "integer", "default": DEFAULT_MAX_PAGES, "minimum": 1, "maximum": MAX_PAGES},
                },
                "required": ["path"],
            },
        },
        {
            "name": "discover_object_types",
            "description": "Discover NetBox core and plugin object collections exposed by the configured API.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Optional case-insensitive filter."},
                    "limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": 500},
                },
            },
        },
        {
            "name": "describe_object_type",
            "description": "Show OpenAPI fields, filters, and fields observed on a live NetBox object.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "object_type": {"type": "string", "description": "Collection path like dcim.devices."},
                    "max_fields": {"type": "integer", "default": 500, "minimum": 1, "maximum": 1000},
                    "include_sample": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include raw sample values; observed field names are collected regardless.",
                    },
                },
                "required": ["object_type"],
            },
        },
        {
            "name": "get_inventory_statistics",
            "description": "Get total object counts for selected NetBox collections.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "object_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 25,
                        "description": "Defaults to common DCIM, IPAM, virtualization, tenancy, and circuit collections.",
                    },
                },
            },
        },
    ]


@dataclass
class DiscoveryCache:
    schema_paths: list[str] = field(default_factory=list)
    endpoint_paths: list[str] = field(default_factory=list)
    api_root: dict[str, Any] = field(default_factory=dict)
    schema: dict[str, Any] = field(default_factory=dict, repr=False)
    source: str = "static"
    error: str = ""

    def to_summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "schema_path_count": len(self.schema_paths),
            "endpoint_path_count": len(self.endpoint_paths),
            "endpoint_path_sample": self.endpoint_paths[:25],
            "api_root_sections": sorted(self.api_root.keys()),
            "error": self.error or None,
        }


class NetBoxBackend:
    def __init__(self, netbox_url: str, *, cache_ttl_seconds: float = 15.0, cache_max_entries: int = 256) -> None:
        self.base_url = netbox_url.rstrip("/")
        parsed_base = urlparse(self.base_url)
        if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc or parsed_base.username or parsed_base.password:
            raise ValueError("NETBOX_URL must be an http(s) URL without embedded credentials.")
        self.api_base = f"{self.base_url}/api/"
        self._discovery_cache = BoundedTTLCache[DiscoveryCache](
            ttl_seconds=DISCOVERY_CACHE_TTL_SECONDS,
            max_entries=1,
        )
        self._response_cache = BoundedTTLCache[Any](ttl_seconds=cache_ttl_seconds, max_entries=cache_max_entries)
        self._client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        normalized_method = method.upper()
        if normalized_method != "GET":
            raise ValueError("NetBox MCP is read-only; only GET requests are allowed.")
        url = self._resolve_url(path_or_url)
        cache_key = json.dumps([normalized_method, url, params or {}], ensure_ascii=True, sort_keys=True, default=str)

        def load() -> Any:
            response = self._client.request(normalized_method, url, headers=self._headers(), params=params, json=json_body)
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise ValueError(f"NetBox response exceeded {MAX_RESPONSE_BYTES} bytes.")
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        return self._response_cache.get_or_load(cache_key, load)

    def _resolve_url(self, path_or_url: str) -> str:
        raw = path_or_url.strip()
        if not raw or "\\" in raw:
            raise ValueError("NetBox API path is invalid.")
        if raw.startswith(("http://", "https://")):
            resolved = raw
        elif raw.startswith("/"):
            base = urlparse(self.base_url)
            resolved = urljoin(f"{base.scheme}://{base.netloc}", raw)
        else:
            resolved = urljoin(self.api_base, raw.lstrip("/"))
        parsed = urlparse(resolved)
        expected = urlparse(self.api_base)
        if parsed.scheme != expected.scheme or parsed.netloc != expected.netloc:
            raise ValueError("NetBox API URL must remain on the configured origin.")
        api_path = expected.path.rstrip("/") + "/"
        normalized_path = parsed.path.rstrip("/") + "/"
        if not normalized_path.startswith(api_path):
            raise ValueError("NetBox API URL must remain below the configured /api/ path.")
        return resolved

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

    def _load_api_structure(self) -> DiscoveryCache:
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
                    return DiscoveryCache(
                        schema_paths=schema_paths,
                        endpoint_paths=schema_paths,
                        schema=payload,
                        source=candidate,
                    )

        try:
            api_root_payload = self._request("GET", self.api_base)
        except Exception as exc:
            return DiscoveryCache(source="unavailable", error=str(exc))

        api_root = self._extract_api_root(api_root_payload if isinstance(api_root_payload, dict) else {})
        endpoint_paths: list[str] = []
        for section, value in api_root.items():
            if isinstance(value, str):
                endpoint_paths.append(value)
            elif isinstance(value, dict):
                endpoint_paths.extend(item for item in value.values() if isinstance(item, str))
        return DiscoveryCache(
            endpoint_paths=sorted(set(endpoint_paths)),
            api_root=api_root,
            source="/api/",
        )

    def discover_api_structure(self) -> DiscoveryCache:
        return self._discovery_cache.get_or_load("api-structure", self._load_api_structure)

    @staticmethod
    def _object_type_from_path(path: str) -> str | None:
        parsed = urlparse(path)
        normalized = parsed.path.strip("/")
        if normalized.startswith("api/"):
            normalized = normalized[4:]
        if not normalized or "{" in normalized or "}" in normalized:
            return None
        parts = [part for part in normalized.split("/") if part]
        if len(parts) < 2 or parts[-1] in {"schema", "status"}:
            return None
        return ".".join(parts)

    def _collection_entries(self, discovery: DiscoveryCache) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        schema_paths = discovery.schema.get("paths", {}) if isinstance(discovery.schema, dict) else {}
        for path in discovery.endpoint_paths:
            object_type = self._object_type_from_path(path)
            if not object_type or object_type in seen:
                continue
            path_definition = schema_paths.get(path, {}) if isinstance(schema_paths, dict) else {}
            if isinstance(path_definition, dict) and path_definition and "get" not in path_definition:
                continue
            seen.add(object_type)
            entries.append({"object_type": object_type, "endpoint": path, "source": discovery.source})
        return sorted(entries, key=lambda item: item["object_type"])

    @staticmethod
    def _resolve_schema_ref(schema: dict[str, Any], value: Any) -> Any:
        if not isinstance(value, dict) or "$ref" not in value:
            return value
        reference = value.get("$ref")
        if not isinstance(reference, str) or not reference.startswith("#/"):
            return value
        current: Any = schema
        for part in reference[2:].split("/"):
            if not isinstance(current, dict):
                return value
            current = current.get(part.replace("~1", "/").replace("~0", "~"))
        return current if current is not None else value

    @classmethod
    def _schema_fields(
        cls,
        document: dict[str, Any],
        node: Any,
        *,
        max_fields: int,
        prefix: str = "",
        required: set[str] | None = None,
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        if depth > 12 or max_fields < 1:
            return []
        resolved = cls._resolve_schema_ref(document, node)
        if not isinstance(resolved, dict):
            return []
        fields: list[dict[str, Any]] = []
        for branch_name in ("allOf", "oneOf", "anyOf"):
            branches = resolved.get(branch_name)
            if isinstance(branches, list):
                for branch in branches:
                    fields.extend(
                        cls._schema_fields(
                            document,
                            branch,
                            max_fields=max_fields - len(fields),
                            prefix=prefix,
                            required=required,
                            depth=depth + 1,
                        )
                    )
                    if len(fields) >= max_fields:
                        return fields[:max_fields]
        node_type = str(resolved.get("type", "object" if "properties" in resolved else ""))
        if node_type == "array" or "items" in resolved:
            return cls._schema_fields(
                document,
                resolved.get("items", {}),
                max_fields=max_fields,
                prefix=prefix,
                required=required,
                depth=depth + 1,
            )
        properties = resolved.get("properties")
        if not isinstance(properties, dict):
            return fields[:max_fields]
        node_required = {str(item) for item in resolved.get("required", []) if isinstance(item, str)}
        for name, property_schema in properties.items():
            if not isinstance(name, str):
                continue
            property_resolved = cls._resolve_schema_ref(document, property_schema)
            property_definition = property_resolved if isinstance(property_resolved, dict) else {}
            field_path = f"{prefix}.{name}" if prefix else name
            property_type = str(
                property_definition.get(
                    "type",
                    "object" if "properties" in property_definition or "$ref" in (property_schema or {}) else "unknown",
                )
            )
            item: dict[str, Any] = {
                "path": field_path,
                "type": property_type,
                "required": name in (required or node_required),
            }
            description = property_definition.get("description")
            if isinstance(description, str) and description.strip():
                item["description"] = description.strip()[:500]
            enum = property_definition.get("enum")
            if isinstance(enum, list):
                item["enum"] = enum[:50]
            fields.append(item)
            if len(fields) >= max_fields:
                break
            fields.extend(
                cls._schema_fields(
                    document,
                    property_definition,
                    max_fields=max_fields - len(fields),
                    prefix=field_path,
                    depth=depth + 1,
                )
            )
            if len(fields) >= max_fields:
                break
        return fields[:max_fields]

    @staticmethod
    def _observed_fields(value: Any, *, max_fields: int, prefix: str = "", depth: int = 0) -> list[dict[str, str]]:
        if depth > 12 or max_fields < 1:
            return []
        if isinstance(value, dict):
            fields: list[dict[str, str]] = []
            for name, item in value.items():
                path = f"{prefix}.{name}" if prefix else str(name)
                item_type = "null" if item is None else "array" if isinstance(item, list) else "object" if isinstance(item, dict) else type(item).__name__
                fields.append({"path": path, "type": item_type})
                if len(fields) >= max_fields:
                    break
                fields.extend(
                    NetBoxBackend._observed_fields(
                        item,
                        max_fields=max_fields - len(fields),
                        prefix=path,
                        depth=depth + 1,
                    )
                )
                if len(fields) >= max_fields:
                    break
            return fields[:max_fields]
        if isinstance(value, list) and value:
            return NetBoxBackend._observed_fields(
                value[0],
                max_fields=max_fields,
                prefix=f"{prefix}[]" if prefix else "[]",
                depth=depth + 1,
            )
        return []

    def _describe_object_type(self, object_type: str, *, max_fields: int, include_sample: bool) -> dict[str, Any]:
        discovery = self.discover_api_structure()
        normalized = _normalize_object_type(object_type)
        endpoint = f"/api/{normalized}/"
        paths = discovery.schema.get("paths", {}) if isinstance(discovery.schema, dict) else {}
        operation: dict[str, Any] = {}
        path_definition = paths.get(endpoint, {}) if isinstance(paths, dict) else {}
        if isinstance(path_definition, dict):
            get_operation = path_definition.get("get", {})
            operation = get_operation if isinstance(get_operation, dict) else {}

        parameters: list[dict[str, Any]] = []
        raw_parameters: list[Any] = []
        if isinstance(path_definition, dict) and isinstance(path_definition.get("parameters"), list):
            raw_parameters.extend(path_definition["parameters"])
        if isinstance(operation.get("parameters"), list):
            raw_parameters.extend(operation["parameters"])
        for raw_parameter in raw_parameters:
            parameter = self._resolve_schema_ref(discovery.schema, raw_parameter)
            if not isinstance(parameter, dict) or parameter.get("in") != "query":
                continue
            item = {
                "name": str(parameter.get("name", "")),
                "required": bool(parameter.get("required", False)),
            }
            parameter_schema = parameter.get("schema")
            if isinstance(parameter_schema, dict) and parameter_schema.get("type"):
                item["type"] = str(parameter_schema["type"])
            if isinstance(parameter.get("description"), str):
                item["description"] = str(parameter["description"])[:500]
            parameters.append(item)

        response_schema: Any = {}
        responses = operation.get("responses", {})
        if isinstance(responses, dict):
            success_response = responses.get("200") or responses.get(200) or {}
            success_response = self._resolve_schema_ref(discovery.schema, success_response)
            if isinstance(success_response, dict):
                content = success_response.get("content", {})
                if isinstance(content, dict) and content:
                    media: Any = content.get("application/json") or next(iter(content.values()), {})
                    if isinstance(media, dict):
                        response_schema = media.get("schema", {})
                if not response_schema and "schema" in success_response:
                    response_schema = success_response.get("schema", {})
        resolved_response = self._resolve_schema_ref(discovery.schema, response_schema)
        if isinstance(resolved_response, dict):
            properties = resolved_response.get("properties", {})
            if isinstance(properties, dict) and "results" in properties:
                results_schema = self._resolve_schema_ref(discovery.schema, properties["results"])
                if isinstance(results_schema, dict):
                    response_schema = results_schema.get("items", results_schema)

        schema_fields = self._schema_fields(discovery.schema, response_schema, max_fields=max_fields)
        sample: Any = None
        observed_fields: list[dict[str, str]] = []
        sample_error = ""
        try:
            payload = self._request("GET", f"{normalized}/", params={"limit": 1, "offset": 0})
            if isinstance(payload, dict) and isinstance(payload.get("results"), list):
                sample = payload["results"][0] if payload["results"] else None
            elif isinstance(payload, list):
                sample = payload[0] if payload else None
            observed_fields = self._observed_fields(sample, max_fields=max_fields)
        except Exception as exc:
            sample_error = str(exc)
        return {
            "object_type": normalized.replace("/", "."),
            "endpoint": endpoint,
            "schema_source": discovery.source,
            "schema_available": bool(discovery.schema),
            "filter_parameters": parameters,
            "schema_field_count": len(schema_fields),
            "schema_fields": schema_fields,
            "observed_field_count": len(observed_fields),
            "observed_fields": observed_fields,
            "sample": sample if include_sample else None,
            "sample_error": sample_error or None,
            "fields_truncated": len(schema_fields) >= max_fields or len(observed_fields) >= max_fields,
        }

    def _paginate_collection(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> dict[str, Any]:
        limit = _bounded_int(limit, default=DEFAULT_PAGE_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT, name="limit")
        offset = _bounded_int(offset, default=0, minimum=0, maximum=2_147_483_647, name="offset")
        max_pages = _bounded_int(max_pages, default=DEFAULT_MAX_PAGES, minimum=1, maximum=MAX_PAGES, name="max_pages")
        query = dict(params or {})
        query.setdefault("limit", limit)
        query.setdefault("offset", offset)
        page_count = 0
        results: list[Any] = []
        current_target = path
        next_target: str | None = None
        first_page: dict[str, Any] | None = None
        last_page: dict[str, Any] | None = None
        truncated = False

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
            last_page = payload
            page_results = payload.get("results")
            if not isinstance(page_results, list):
                raise ValueError("NetBox pagination payload did not contain a list in 'results'.")
            remaining = MAX_RESULTS - len(results)
            results.extend(page_results[:remaining])
            page_count += 1
            next_target = payload.get("next") if isinstance(payload.get("next"), str) and payload.get("next") else None
            if len(results) >= MAX_RESULTS:
                truncated = bool(next_target or len(page_results) > remaining)
                break
            if not next_target:
                break
            next_target = self._resolve_url(next_target)

        if next_target and page_count >= max_pages:
            truncated = True

        total_count = 0
        next_offset = None
        previous_offset = None
        if first_page is not None:
            total_count = int(first_page.get("count") or len(results))
            next_offset = self._offset_from_url(last_page.get("next") if last_page else None)
            previous_offset = self._offset_from_url(first_page.get("previous"))

        return {
            "paginated": True,
            "count": total_count,
            "limit": query.get("limit"),
            "offset": query.get("offset"),
            "next_offset": next_offset,
            "previous_offset": previous_offset,
            "pages_fetched": page_count,
            "truncated": truncated,
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
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "discovery": discovery.to_summary(),
            }
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload: Any
        if name == "discover_object_types":
            discovery_query = str(arguments.get("query", "")).strip().lower()
            limit = _bounded_int(arguments.get("limit"), default=200, minimum=1, maximum=500, name="limit")
            discovery = self.discover_api_structure()
            entries = self._collection_entries(discovery)
            if discovery_query:
                entries = [
                    entry
                    for entry in entries
                    if discovery_query in entry["object_type"].lower()
                    or discovery_query in entry["endpoint"].lower()
                ]
            payload = {
                "source": discovery.source,
                "total": len(entries),
                "returned": min(len(entries), limit),
                "truncated": len(entries) > limit,
                "object_types": entries[:limit],
            }
            return self._tool_result(name, arguments, payload)

        if name == "describe_object_type":
            max_fields = _bounded_int(
                arguments.get("max_fields"),
                default=500,
                minimum=1,
                maximum=1000,
                name="max_fields",
            )
            payload = self._describe_object_type(
                str(arguments["object_type"]),
                max_fields=max_fields,
                include_sample=bool(arguments.get("include_sample", False)),
            )
            return self._tool_result(name, arguments, payload)

        if name == "get_inventory_statistics":
            raw_object_types = arguments.get("object_types")
            if raw_object_types is None:
                object_types = DEFAULT_STAT_OBJECT_TYPES
            elif not isinstance(raw_object_types, list):
                raise ValueError("'object_types' must be an array.")
            else:
                if not raw_object_types or len(raw_object_types) > 25:
                    raise ValueError("'object_types' must contain between 1 and 25 entries.")
                object_types = [str(item) for item in raw_object_types]
            statistics: list[dict[str, Any]] = []
            total_objects = 0
            for requested_type in object_types:
                normalized = _normalize_object_type(requested_type)
                object_type = normalized.replace("/", ".")
                try:
                    payload = self._request("GET", f"{normalized}/", params={"limit": 1, "offset": 0})
                    if isinstance(payload, dict):
                        raw_results = payload.get("results")
                        fallback_count = len(raw_results) if isinstance(raw_results, list) else 0
                        raw_count = payload.get("count", fallback_count)
                        count = int(raw_count) if isinstance(raw_count, (int, str)) else fallback_count
                    elif isinstance(payload, list):
                        count = len(payload)
                    else:
                        count = 0
                    total_objects += count
                    statistics.append({"object_type": object_type, "count": count, "available": True})
                except Exception as exc:
                    statistics.append(
                        {"object_type": object_type, "count": None, "available": False, "error": str(exc)}
                    )
            payload = {
                "collection_count": len(statistics),
                "available_collection_count": sum(bool(item["available"]) for item in statistics),
                "total_objects_across_collections": total_objects,
                "statistics": statistics,
            }
            return self._tool_result(name, arguments, payload)

        if name == "get_objects":
            object_type = _normalize_object_type(str(arguments["object_type"]))
            filters = arguments.get("filters")
            if filters is not None and not isinstance(filters, dict):
                raise ValueError("'filters' must be an object.")
            fields = _normalize_fields(arguments.get("fields"))
            limit = _bounded_int(arguments.get("limit"), default=DEFAULT_PAGE_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT, name="limit")
            offset = _bounded_int(arguments.get("offset"), default=0, minimum=0, maximum=2_147_483_647, name="offset")
            fetch_all = bool(arguments.get("fetch_all", False))
            max_pages = _bounded_int(arguments.get("max_pages"), default=DEFAULT_MAX_PAGES, minimum=1, maximum=MAX_PAGES, name="max_pages")
            path = f"{object_type.strip('/')}/"
            query = {**(filters or {})}
            if fields:
                query["fields"] = ",".join(fields)
            payload = self._paginate_collection(path, params=query, limit=limit, offset=offset, max_pages=max_pages) if fetch_all else self._request(
                "GET",
                path,
                params={**query, "limit": limit, "offset": offset},
            )
            return self._tool_result(name, arguments, payload)

        if name == "get_object_by_id":
            object_type = _normalize_object_type(str(arguments["object_type"]))
            object_id = _bounded_int(arguments.get("id"), default=0, minimum=1, maximum=2_147_483_647, name="id")
            fields = _normalize_fields(arguments.get("fields"))
            payload = self._request(
                "GET",
                f"{object_type.strip('/')}/{object_id}/",
                params={"fields": ",".join(fields)} if fields else None,
            )
            return self._tool_result(name, arguments, payload)

        if name == "get_changelogs":
            object_type = _normalize_object_type(str(arguments["object_type"]))
            object_id = _bounded_int(arguments.get("id"), default=0, minimum=1, maximum=2_147_483_647, name="id")
            limit = _bounded_int(arguments.get("limit"), default=DEFAULT_PAGE_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT, name="limit")
            offset = _bounded_int(arguments.get("offset"), default=0, minimum=0, maximum=2_147_483_647, name="offset")
            fetch_all = bool(arguments.get("fetch_all", False))
            max_pages = _bounded_int(arguments.get("max_pages"), default=DEFAULT_MAX_PAGES, minimum=1, maximum=MAX_PAGES, name="max_pages")
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
            if method != "GET":
                raise ValueError("NetBox MCP is read-only; call_endpoint only supports GET.")
            params = arguments.get("params")
            if params is not None and not isinstance(params, dict):
                raise ValueError("'params' must be an object.")
            paginate = bool(arguments.get("paginate", False))
            limit = _bounded_int(arguments.get("limit"), default=DEFAULT_PAGE_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT, name="limit")
            offset = _bounded_int(arguments.get("offset"), default=0, minimum=0, maximum=2_147_483_647, name="offset")
            max_pages = _bounded_int(arguments.get("max_pages"), default=DEFAULT_MAX_PAGES, minimum=1, maximum=MAX_PAGES, name="max_pages")
            payload = self._paginate_collection(path, params=params, limit=limit, offset=offset, max_pages=max_pages) if paginate else self._request(
                method,
                path,
                params=params,
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


def create_app(netbox_url: str) -> FastAPI:
    backend = NetBoxBackend(netbox_url=netbox_url)
    sessions = SessionRegistry()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        backend.close()

    app = FastAPI(title="NetBox MCP Server", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, Any]:
        discovery = backend.discover_api_structure()
        return {
            "ok": discovery.source != "unavailable",
            "server": SERVER_NAME,
            "netbox_url": backend.base_url,
            "authentication": "anonymous",
            "read_only": True,
            "discovery": discovery.to_summary(),
            "cache": {
                "responses": backend._response_cache.stats(),
                "discovery": backend._discovery_cache.stats(),
            },
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
                session_id = sessions.create()
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

            if session_id and not sessions.contains(session_id):
                return _jsonrpc_error(request_id, -32001, "Unknown MCP session", status_code=400)

            if method == "tools/list":
                tools = await run_in_threadpool(backend.list_tools)
                return _jsonrpc_result(request_id, {"tools": tools}, headers=session_headers)

            if method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(tool_name, str) or not tool_name:
                    return _jsonrpc_error(request_id, -32602, "Invalid params", data="Tool name is required.", status_code=400, headers=session_headers)
                if not isinstance(arguments, dict):
                    return _jsonrpc_error(request_id, -32602, "Invalid params", data="Tool arguments must be an object.", status_code=400, headers=session_headers)
                result = await run_in_threadpool(backend.call_tool, tool_name, arguments)
                return _jsonrpc_result(request_id, result, headers=session_headers)

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
