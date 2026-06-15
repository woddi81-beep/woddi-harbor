"""OpenStack MCP server exposed through FastAPI at ``/mcp``."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ..cache import BoundedTTLCache, SessionRegistry

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "openstack-mcp-server"
SERVER_VERSION = "0.3.1"
DEFAULT_CACHE_TTL_SECONDS = 20.0
DEFAULT_RESULT_LIMIT = 100
MAX_RESULT_LIMIT = 500
RESOURCE_CATALOG: dict[str, dict[str, str]] = {
    "server": {"service": "compute", "tool": "list_servers"},
    "project": {"service": "identity", "tool": "list_projects"},
    "image": {"service": "image", "tool": "list_images"},
    "flavor": {"service": "compute", "tool": "list_flavors"},
    "network": {"service": "network", "tool": "list_networks"},
    "subnet": {"service": "network", "tool": "list_subnets"},
    "port": {"service": "network", "tool": "list_ports"},
    "router": {"service": "network", "tool": "list_routers"},
    "floating_ip": {"service": "network", "tool": "list_floating_ips"},
    "security_group": {"service": "network", "tool": "list_security_groups"},
    "volume": {"service": "block_storage", "tool": "list_volumes"},
    "volume_snapshot": {"service": "block_storage", "tool": "list_volume_snapshots"},
    "volume_backup": {"service": "block_storage", "tool": "list_volume_backups"},
    "keypair": {"service": "compute", "tool": "list_keypairs"},
    "server_group": {"service": "compute", "tool": "list_server_groups"},
    "stack": {"service": "orchestration", "tool": "list_stacks"},
    "load_balancer": {"service": "load_balancer", "tool": "list_load_balancers"},
    "availability_zone": {"service": "compute", "tool": "list_availability_zones"},
}


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
    token = credentials.get("OS_TOKEN", "").strip()
    if not token:
        raise ValueError("OS_TOKEN fehlt. Harbor unterstuetzt fuer OpenStack ausschliesslich User-Tokens.")
    options.update(
        {
            "auth_type": "token",
            "token": token,
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
    list_properties = {
        "name": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULT_LIMIT, "default": DEFAULT_RESULT_LIMIT},
        "fields": {"type": "array", "items": {"type": "string"}, "maxItems": 50, "description": "Return only these fields to reduce token usage."},
    }

    def list_tool(name: str, description: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "name": name,
            "description": description,
            "inputSchema": {"type": "object", "properties": {**list_properties, **(extra or {})}},
        }

    return [
        list_tool("list_servers", "List OpenStack compute servers.", {"status": {"type": "string"}, "project": {"type": "string"}}),
        {"name": "get_server", "description": "Show one OpenStack server by id or name.", "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}, "fields": list_properties["fields"]}, "required": ["server"]}},
        list_tool("list_projects", "List projects visible to the configured project-scoped credential."),
        list_tool("list_images", "List OpenStack images.", {"status": {"type": "string"}}),
        list_tool("list_flavors", "List OpenStack flavors."),
        list_tool("list_networks", "List OpenStack networks."),
        list_tool("list_subnets", "List OpenStack subnets.", {"network": {"type": "string"}}),
        list_tool("list_ports", "List OpenStack ports.", {"server": {"type": "string"}, "network": {"type": "string"}}),
        list_tool("list_routers", "List OpenStack routers."),
        list_tool("list_floating_ips", "List OpenStack floating IPs.", {"status": {"type": "string"}}),
        list_tool("list_security_groups", "List OpenStack security groups."),
        list_tool("list_volumes", "List OpenStack block-storage volumes.", {"status": {"type": "string"}}),
        list_tool("list_volume_snapshots", "List OpenStack volume snapshots.", {"status": {"type": "string"}}),
        list_tool("list_volume_backups", "List OpenStack volume backups.", {"status": {"type": "string"}}),
        list_tool("list_keypairs", "List OpenStack compute keypairs."),
        list_tool("list_server_groups", "List OpenStack server groups."),
        list_tool("list_stacks", "List OpenStack Heat stacks.", {"status": {"type": "string"}}),
        list_tool("list_load_balancers", "List OpenStack Octavia load balancers.", {"status": {"type": "string"}}),
        list_tool("list_availability_zones", "List OpenStack compute availability zones."),
        {"name": "get_compute_limits", "description": "Get absolute compute limits and quota usage for the scoped project.", "inputSchema": {"type": "object", "properties": {"fields": list_properties["fields"]}}},
        {
            "name": "discover_resources",
            "description": "Discover available OpenStack resources and fields observed in the scoped cloud.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "resources": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(RESOURCE_CATALOG)},
                        "maxItems": len(RESOURCE_CATALOG),
                    },
                    "include_sample": {"type": "boolean", "default": False},
                },
            },
        },
        {
            "name": "get_storage_statistics",
            "description": "Get Cinder storage quotas, utilization percentages, volume counts, statuses, and provisioned GiB.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_project_statistics",
            "description": "Get project-wide OpenStack inventory, status distributions, and compute/storage quota utilization.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "call_readonly",
            "description": "Execute a whitelisted read-only OpenStack SDK list/show operation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "resource": {"type": "string"},
                    "operation": {"type": "string", "enum": ["list", "show"]},
                    "target": {"type": "string"},
                    "filters": {"type": "object"},
                    "limit": list_properties["limit"],
                    "fields": list_properties["fields"],
                },
                "required": ["resource", "operation"],
            },
        },
    ]


@dataclass
class OpenStackBackend:
    credentials: dict[str, str]
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS
    cache_max_entries: int = 256
    connection_factory: Callable[[dict[str, str]], Any] = create_openstack_connection
    _cache: BoundedTTLCache[Any] = field(init=False, repr=False)
    _connection: Any = field(default=None, init=False, repr=False)
    _project_context: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _connection_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._cache = BoundedTTLCache(ttl_seconds=self.cache_ttl_seconds, max_entries=self.cache_max_entries)

    def close(self) -> None:
        connection = self._connection
        close = getattr(connection, "close", None)
        if callable(close):
            close()

    def _get_connection(self) -> Any:
        if self._connection is not None:
            return self._connection
        with self._connection_lock:
            if self._connection is not None:
                return self._connection
            connection = self.connection_factory(self.credentials)
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
            project_id = str(getattr(access, "project_id", "") or "").strip()
            project_name = str(getattr(access, "project_name", "") or "").strip()
            project_scoped = bool(getattr(access, "project_scoped", project_id))
            if not project_scoped or not project_id or not access.has_service_catalog():
                raise RuntimeError(
                    "OpenStack User-Token ist nicht projektgescoped. "
                    "Erzeuge den Token im Zielprojekt; Harbor nimmt kein separates Projekt und fuehrt kein Rescoping durch."
                )
            self._project_context = {"id": project_id, "name": project_name}
            self._connection = connection
        return self._connection

    def _cached(self, operation: str, arguments: dict[str, Any], loader: Callable[[], Any]) -> Any:
        cache_key = json.dumps([operation, arguments], ensure_ascii=False, sort_keys=True, default=str)

        def load() -> Any:
            try:
                return loader()
            except Exception as exc:
                if _is_timeout_error(exc):
                    raise RuntimeError(
                        f"OpenStack Operation {operation} hat nach "
                        f"{_timeout_seconds(self.credentials):.0f}s nicht geantwortet. "
                        "Pruefe Service-Katalog, Region, Routing und Firewall."
                    ) from exc
                raise

        return self._cache.get_or_load(cache_key, load)

    @classmethod
    def _serialize(cls, resource: Any) -> dict[str, Any]:
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
        serialized = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        return cls._redact(serialized)

    @classmethod
    def _redact(cls, value: Any) -> Any:
        sensitive_keys = {
            "admin_pass",
            "application_credential_secret",
            "auth_token",
            "connection_info",
            "password",
            "secret",
            "token",
            "user_data",
        }
        if isinstance(value, dict):
            return {
                key: "[redacted]" if str(key).lower() in sensitive_keys else cls._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        return value

    @staticmethod
    def _fields(arguments: dict[str, Any]) -> list[str]:
        value = arguments.get("fields")
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("fields muss eine Liste sein.")
        fields = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if len(fields) > 50:
            raise ValueError("fields unterstuetzt maximal 50 Eintraege.")
        return fields

    @classmethod
    def _project_fields(cls, payload: Any, arguments: dict[str, Any]) -> Any:
        fields = cls._fields(arguments)
        if not fields:
            return payload

        def project(row: dict[str, Any]) -> dict[str, Any]:
            return {field_name: row.get(field_name) for field_name in fields if field_name in row}

        if isinstance(payload, list):
            return [project(row) if isinstance(row, dict) else row for row in payload]
        if isinstance(payload, dict):
            return project(payload)
        return payload

    @staticmethod
    def _limit(arguments: dict[str, Any]) -> int:
        try:
            limit = int(arguments.get("limit", DEFAULT_RESULT_LIMIT) or DEFAULT_RESULT_LIMIT)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit muss eine Ganzzahl sein.") from exc
        if limit < 1 or limit > MAX_RESULT_LIMIT:
            raise ValueError(f"limit muss zwischen 1 und {MAX_RESULT_LIMIT} liegen.")
        return limit

    def _list_resources(
        self,
        operation: str,
        arguments: dict[str, Any],
        loader: Callable[[], Any],
        *,
        field_filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._cached(operation, {}, lambda: [self._serialize(item) for item in loader()])
        name = str(arguments.get("name", "")).strip().lower()
        if name:
            rows = [row for row in rows if name in str(row.get("name") or row.get("id") or "").lower()]
        for argument_name, field_name in (field_filters or {}).items():
            expected = str(arguments.get(argument_name, "")).strip().lower()
            if expected:
                rows = [row for row in rows if expected in str(row.get(field_name, "")).lower()]
        return self._project_fields(rows[: self._limit(arguments)], arguments)

    def health(self) -> dict[str, Any]:
        error = ""
        try:
            self._get_connection()
        except Exception as exc:
            error = str(exc)
        return {
            "ok": openstack_sdk_available() and not error,
            "server": SERVER_NAME,
            "backend": "openstacksdk",
            "openstack_sdk": openstack_sdk_available(),
            "auth_configured": bool(self.credentials.get("OS_AUTH_URL") and self.credentials.get("OS_TOKEN")),
            "timeout_seconds": _timeout_seconds(self.credentials),
            "scope_mode": "token_project",
            "credential_mode": "user_token",
            "project": self._project_context or None,
            "error": error or None,
            "cache": self._cache.stats(),
            "tool_count": len(_tool_schema()),
        }

    def list_tools(self) -> list[dict[str, Any]]:
        tools = _tool_schema()
        for tool in tools:
            tool["annotations"] = {
                "title": tool["name"],
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
            }
        return tools

    def _apply_generic_filters(self, rows: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
        for key, value in filters.items():
            if value is None or value == "":
                continue
            normalized = str(value).lower()
            rows = [item for item in rows if normalized in str(item.get(key) or "").lower()]
        return rows

    def _resource_list(
        self,
        resource: str,
        arguments: dict[str, Any],
        *,
        field_filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        connection = self._get_connection()
        loaders = self._resource_loaders(connection)
        return self._list_resources(f"{resource}.list", arguments, loaders[resource], field_filters=field_filters)

    @staticmethod
    def _resource_loaders(connection: Any) -> dict[str, Callable[[], Any]]:
        return {
            "server": lambda: connection.compute.servers(details=True),
            "project": lambda: connection.identity.projects(),
            "image": lambda: connection.image.images(),
            "flavor": lambda: connection.compute.flavors(),
            "network": lambda: connection.network.networks(),
            "subnet": lambda: connection.network.subnets(),
            "port": lambda: connection.network.ports(),
            "router": lambda: connection.network.routers(),
            "floating_ip": lambda: connection.network.ips(),
            "security_group": lambda: connection.network.security_groups(),
            "volume": lambda: connection.block_storage.volumes(details=True),
            "volume_snapshot": lambda: connection.block_storage.snapshots(details=True),
            "volume_backup": lambda: connection.block_storage.backups(details=True),
            "keypair": lambda: connection.compute.keypairs(),
            "server_group": lambda: connection.compute.server_groups(),
            "stack": lambda: connection.orchestration.stacks(),
            "load_balancer": lambda: connection.load_balancer.load_balancers(),
            "availability_zone": lambda: connection.compute.availability_zones(),
        }

    def _all_resources(self, resource: str) -> list[dict[str, Any]]:
        connection = self._get_connection()
        loader = self._resource_loaders(connection)[resource]
        rows = self._cached(f"{resource}.list", {}, lambda: [self._serialize(item) for item in loader()])
        return rows if isinstance(rows, list) else []

    @staticmethod
    def _status_counts(rows: list[dict[str, Any]], *field_names: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            raw_status = next((row.get(name) for name in field_names if row.get(name) not in {None, ""}), "unknown")
            status = str(raw_status).lower()
            counts[status] = counts.get(status, 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def _find_numeric(value: Any, candidates: set[str]) -> float | None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).replace("_", "").lower()
                if normalized in candidates and isinstance(item, (int, float)) and not isinstance(item, bool):
                    return float(item)
            for item in value.values():
                found = OpenStackBackend._find_numeric(item, candidates)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = OpenStackBackend._find_numeric(item, candidates)
                if found is not None:
                    return found
        return None

    @classmethod
    def _utilization(
        cls,
        limits: dict[str, Any],
        *,
        used_keys: tuple[str, ...],
        limit_keys: tuple[str, ...],
        unit: str,
    ) -> dict[str, Any]:
        used = cls._find_numeric(limits, {key.replace("_", "").lower() for key in used_keys})
        limit = cls._find_numeric(limits, {key.replace("_", "").lower() for key in limit_keys})
        finite_limit = limit is not None and limit >= 0
        if limit is not None and limit >= 0:
            percent = round((used or 0.0) / limit * 100.0, 2) if limit > 0 else None
            available = max(limit - (used or 0.0), 0.0)
        else:
            percent = None
            available = None

        def compact(number: float | None) -> int | float | None:
            if number is None:
                return None
            return int(number) if number.is_integer() else round(number, 2)

        return {
            "used": compact(used),
            "limit": compact(limit) if finite_limit else None,
            "available": compact(available),
            "percent": percent,
            "unit": unit,
            "unlimited": limit is not None and limit < 0,
        }

    def _storage_statistics(self) -> dict[str, Any]:
        connection = self._get_connection()
        project = self._project_context.get("id") or None
        errors: dict[str, str] = {}
        try:
            limits = self._cached(
                "block_storage.limits",
                {"project": project or ""},
                lambda: self._serialize(connection.block_storage.get_limits(project)),
            )
        except Exception as exc:
            limits = {}
            errors["limits"] = str(exc)

        def load(resource: str) -> list[dict[str, Any]]:
            try:
                return self._all_resources(resource)
            except Exception as exc:
                errors[resource] = str(exc)
                return []

        volumes = load("volume")
        snapshots = load("volume_snapshot")
        backups = load("volume_backup")
        provisioned_gib = float(
            sum(
                float(row.get("size", 0) or 0)
                for row in volumes
                if isinstance(row.get("size", 0), (int, float))
            )
        )
        return {
            "quota": {
                "capacity_gib": self._utilization(
                    limits,
                    used_keys=("totalGigabytesUsed", "total_gigabytes_used"),
                    limit_keys=("maxTotalVolumeGigabytes", "max_total_volume_gigabytes"),
                    unit="GiB",
                ),
                "volumes": self._utilization(
                    limits,
                    used_keys=("totalVolumesUsed", "total_volumes_used"),
                    limit_keys=("maxTotalVolumes", "max_total_volumes"),
                    unit="volumes",
                ),
                "snapshots": self._utilization(
                    limits,
                    used_keys=("totalSnapshotsUsed", "total_snapshots_used"),
                    limit_keys=("maxTotalSnapshots", "max_total_snapshots"),
                    unit="snapshots",
                ),
                "backups": self._utilization(
                    limits,
                    used_keys=("totalBackupsUsed", "total_backups_used"),
                    limit_keys=("maxTotalBackups", "max_total_backups"),
                    unit="backups",
                ),
                "backup_capacity_gib": self._utilization(
                    limits,
                    used_keys=("totalBackupGigabytesUsed", "total_backup_gigabytes_used"),
                    limit_keys=("maxTotalBackupGigabytes", "max_total_backup_gigabytes"),
                    unit="GiB",
                ),
            },
            "inventory": {
                "volume_count": len(volumes),
                "volume_statuses": self._status_counts(volumes, "status"),
                "provisioned_volume_gib": int(provisioned_gib)
                if provisioned_gib.is_integer()
                else round(provisioned_gib, 2),
                "snapshot_count": len(snapshots),
                "snapshot_statuses": self._status_counts(snapshots, "status"),
                "backup_count": len(backups),
                "backup_statuses": self._status_counts(backups, "status"),
            },
            "raw_limits": limits,
            "errors": errors,
        }

    def _compute_statistics(self) -> dict[str, Any]:
        connection = self._get_connection()
        limits = self._cached("compute.limits", {}, lambda: self._serialize(connection.compute.get_limits()))
        return {
            "quota": {
                "instances": self._utilization(
                    limits,
                    used_keys=("totalInstancesUsed", "instances_used", "total_instances_used"),
                    limit_keys=("maxTotalInstances", "instances"),
                    unit="instances",
                ),
                "cores": self._utilization(
                    limits,
                    used_keys=("totalCoresUsed", "total_cores_used"),
                    limit_keys=("maxTotalCores", "total_cores"),
                    unit="cores",
                ),
                "ram_mib": self._utilization(
                    limits,
                    used_keys=("totalRAMUsed", "total_ram_used"),
                    limit_keys=("maxTotalRAMSize", "total_ram"),
                    unit="MiB",
                ),
            },
            "raw_limits": limits,
        }

    def _discover_resources(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_resources = arguments.get("resources")
        if raw_resources is None:
            resources = list(RESOURCE_CATALOG)
        elif not isinstance(raw_resources, list):
            raise ValueError("resources muss eine Liste sein.")
        else:
            resources = list(dict.fromkeys(str(item).strip().lower() for item in raw_resources if str(item).strip()))
            if not resources or len(resources) > len(RESOURCE_CATALOG):
                raise ValueError(f"resources muss 1 bis {len(RESOURCE_CATALOG)} Eintraege enthalten.")
        unknown = [resource for resource in resources if resource not in RESOURCE_CATALOG]
        if unknown:
            raise ValueError(f"Unbekannte Ressourcen: {', '.join(unknown)}")
        include_sample = bool(arguments.get("include_sample", False))
        discovered: list[dict[str, Any]] = []
        for resource in resources:
            catalog = RESOURCE_CATALOG[resource]
            try:
                rows = self._resource_list(resource, {"limit": 1})
                sample = rows[0] if rows else None
                fields = sorted(sample.keys()) if isinstance(sample, dict) else []
                discovered.append(
                    {
                        "resource": resource,
                        **catalog,
                        "available": True,
                        "has_objects": bool(rows),
                        "observed_fields": fields,
                        "sample": sample if include_sample else None,
                    }
                )
            except Exception as exc:
                discovered.append(
                    {
                        "resource": resource,
                        **catalog,
                        "available": False,
                        "has_objects": False,
                        "observed_fields": [],
                        "sample": None,
                        "error": str(exc),
                    }
                )
        return {
            "resource_count": len(discovered),
            "available_resource_count": sum(bool(item["available"]) for item in discovered),
            "resources": discovered,
        }

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
            "floating_ip": lambda value: connection.network.find_ip(value, ignore_missing=False),
            "security_group": lambda value: connection.network.find_security_group(value, ignore_missing=False),
            "volume": lambda value: connection.block_storage.find_volume(value, ignore_missing=False),
            "volume_snapshot": lambda value: connection.block_storage.find_snapshot(value, ignore_missing=False),
            "volume_backup": lambda value: connection.block_storage.find_backup(value, ignore_missing=False),
            "keypair": lambda value: connection.compute.find_keypair(value, ignore_missing=False),
            "server_group": lambda value: connection.compute.find_server_group(value, ignore_missing=False),
            "stack": lambda value: connection.orchestration.find_stack(value, ignore_missing=False),
            "load_balancer": lambda value: connection.load_balancer.find_load_balancer(value, ignore_missing=False),
        }
        payload = self._cached(f"{resource}.show", {"target": target}, lambda: self._serialize(finders[resource](target)))
        return payload

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        connection = self._get_connection()
        payload: Any
        if name == "discover_resources":
            return self._tool_result(name, arguments, self._discover_resources(arguments))
        if name == "get_storage_statistics":
            return self._tool_result(name, arguments, self._storage_statistics())
        if name == "get_project_statistics":
            sections: dict[str, Any] = {}
            errors: dict[str, str] = {}
            for resource, status_fields in {
                "server": ("status",),
                "network": ("status",),
                "subnet": ("status",),
                "port": ("status",),
                "router": ("status",),
                "floating_ip": ("status",),
                "stack": ("stack_status", "status"),
                "load_balancer": ("provisioning_status", "operating_status", "status"),
            }.items():
                try:
                    rows = self._all_resources(resource)
                    sections[resource] = {
                        "count": len(rows),
                        "statuses": self._status_counts(rows, *status_fields),
                    }
                except Exception as exc:
                    errors[resource] = str(exc)
                    sections[resource] = {"count": None, "statuses": {}, "available": False}
            try:
                compute = self._compute_statistics()
            except Exception as exc:
                compute = {"quota": {}, "raw_limits": {}}
                errors["compute_limits"] = str(exc)
            storage = self._storage_statistics()
            if storage.get("errors"):
                errors["storage"] = json.dumps(storage["errors"], ensure_ascii=False)
            payload = {
                "project": {
                    "id": self._project_context.get("id") or None,
                    "name": self._project_context.get("name") or None,
                    "region": self.credentials.get("OS_REGION_NAME") or None,
                },
                "inventory": sections,
                "compute": compute,
                "storage": storage,
                "errors": errors,
            }
            return self._tool_result(name, arguments, payload)
        if name == "list_servers":
            payload = self._list_resources(
                "server.list",
                arguments,
                lambda: connection.compute.servers(details=True),
                field_filters={"status": "status", "project": "project_id"},
            )
            return self._tool_result(name, arguments, payload)
        if name == "get_server":
            show_payload = self._resource_show("server", str(arguments["server"]).strip())
            return self._tool_result(name, arguments, self._project_fields(show_payload, arguments))
        if name == "list_projects":
            payload = [self._project_context.copy()]
            return self._tool_result(name, arguments, self._project_fields(payload, arguments))
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
        resource_tools: dict[str, tuple[str, dict[str, str] | None]] = {
            "list_floating_ips": ("floating_ip", {"status": "status"}),
            "list_security_groups": ("security_group", None),
            "list_volumes": ("volume", {"status": "status"}),
            "list_volume_snapshots": ("volume_snapshot", {"status": "status"}),
            "list_volume_backups": ("volume_backup", {"status": "status"}),
            "list_keypairs": ("keypair", None),
            "list_server_groups": ("server_group", None),
            "list_stacks": ("stack", {"status": "status"}),
            "list_load_balancers": ("load_balancer", {"status": "provisioning_status"}),
        }
        if name in resource_tools:
            resource, field_filters = resource_tools[name]
            payload = self._resource_list(resource, arguments, field_filters=field_filters)
            return self._tool_result(name, arguments, payload)
        if name == "list_availability_zones":
            payload = self._list_resources(
                "availability_zone.list",
                arguments,
                lambda: connection.compute.availability_zones(),
            )
            return self._tool_result(name, arguments, payload)
        if name == "get_compute_limits":
            payload = self._cached(
                "compute.limits",
                {},
                lambda: self._serialize(connection.compute.get_limits()),
            )
            return self._tool_result(name, arguments, self._project_fields(payload, arguments))
        if name in {"call_readonly", "call_cli_readonly"}:
            resource = str(arguments["resource"]).strip().lower()
            operation = str(arguments["operation"]).strip().lower()
            allowed_resources = {
                "server",
                "project",
                "image",
                "flavor",
                "network",
                "subnet",
                "port",
                "router",
                "floating_ip",
                "security_group",
                "volume",
                "volume_snapshot",
                "volume_backup",
                "keypair",
                "server_group",
                "stack",
                "load_balancer",
            }
            if resource not in allowed_resources:
                raise ValueError("Resource ist nicht freigegeben.")
            if operation == "show":
                target = str(arguments.get("target", "")).strip()
                if not target:
                    raise ValueError("target ist fuer show erforderlich.")
                tool_payload: Any = self._project_fields(self._resource_show(resource, target), arguments)
            elif operation == "list":
                filters = arguments.get("filters")
                tool_payload = self._resource_list(resource, {"limit": arguments.get("limit", DEFAULT_RESULT_LIMIT)})
                if filters is not None:
                    if not isinstance(filters, dict):
                        raise ValueError("filters muss ein Objekt sein.")
                    tool_payload = self._apply_generic_filters(tool_payload, {str(key): value for key, value in filters.items()})
                tool_payload = self._project_fields(tool_payload, arguments)
            else:
                raise ValueError("Nur list/show ist erlaubt.")
            return self._tool_result(name, arguments, tool_payload)
        raise ValueError(f"Unknown tool: {name}")

    def _tool_result(self, tool_name: str, arguments: dict[str, Any], payload: Any) -> dict[str, Any]:
        summary = {"tool": tool_name, "arguments": arguments, "data": payload}
        return {"content": [{"type": "text", "text": f"{tool_name} completed successfully."}], "structuredContent": summary}


def create_app(credentials: dict[str, str]) -> FastAPI:
    backend = OpenStackBackend(credentials=credentials)
    sessions = SessionRegistry()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        backend.close()

    app = FastAPI(title="OpenStack MCP Server", lifespan=lifespan)

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
                session_id = sessions.create()
                return _jsonrpc_result(request_id, {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}, headers={"mcp-session-id": session_id})
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
        except KeyError as exc:
            return _jsonrpc_error(request_id, -32602, "Invalid params", data=f"Missing required argument: {exc.args[0]}", status_code=400, headers=session_headers)
        except ValueError as exc:
            return _jsonrpc_error(request_id, -32602, "Invalid params", data=str(exc), status_code=400, headers=session_headers)
        except Exception as exc:
            return _jsonrpc_error(request_id, -32000, "Server error", data=str(exc), headers=session_headers)

    return app


__all__ = ["create_app", "create_openstack_connection", "OpenStackBackend", "MCP_PROTOCOL_VERSION"]
