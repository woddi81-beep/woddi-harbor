from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha1
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from .cache import BoundedTTLCache
from .config import (
    BASE_DIR,
    LOG_DIR,
    PID_DIR,
    RUNTIME_DIR,
    ModuleConfig,
    find_module,
    internal_worker_token,
    load_modules,
    module_secret,
    module_sources,
    resolve_module_source_path,
)
from .field_cache import load_field_catalog, save_field_catalog, update_catalog_from_tool_result
from .search import IndexKind, SearchIndexMeta, ensure_index, load_index_meta, search_index
from .version import __version__

MCP_PROTOCOL_VERSION = "2024-11-05"
QUERY_CACHE_TTL_SECONDS = 45.0
PERSISTENT_QUERY_CACHE_TTL_SECONDS = 600.0
WARMUP_INTERVAL_SECONDS = 20.0
MODULE_MUTATION_LOCK_PATH = RUNTIME_DIR / "locks" / "modules.lock"
OPENSTACK_TOKEN_HEADER = "X-Harbor-OpenStack-Token"
OPENSTACK_USER_HEADER = "X-Harbor-User"


_QUERY_CACHE = BoundedTTLCache[dict[str, Any]](
    ttl_seconds=QUERY_CACHE_TTL_SECONDS,
    max_entries=512,
)
_REINDEX_LOCK = threading.Lock()
_REINDEX_THREADS: dict[str, threading.Thread] = {}
_HEALTH_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
HEALTH_CACHE_TTL_SECONDS = 2.5


def module_url(module: ModuleConfig) -> str:
    return f"http://{module.host}:{module.port}"


def _local_netbox_health_url(module: ModuleConfig) -> str:
    return f"{module_url(module)}/health"


def _netbox_url(module: ModuleConfig) -> str:
    netbox_url = str(module.settings.get("netbox_url", "")).strip()
    url_env = str(module.settings.get("netbox_url_env", "")).strip()
    if not netbox_url and url_env:
        netbox_url = os.getenv(url_env, "").strip()
    return netbox_url


def _openstack_settings(module: ModuleConfig, token: str = "") -> dict[str, str]:
    def _resolve(primary_key: str, env_key: str) -> str:
        direct = str(module.settings.get(primary_key, "")).strip()
        if direct:
            return direct
        env_name = str(module.settings.get(env_key, "")).strip()
        if env_name:
            return os.getenv(env_name, "").strip()
        return ""

    return {
        "OS_AUTH_URL": _resolve("auth_url", "auth_url_env"),
        "OS_REGION_NAME": _resolve("region_name", "region_name_env"),
        "OS_INTERFACE": _resolve("interface", "interface_env"),
        "OS_TIMEOUT": str(max(1.0, module.timeout_seconds)),
        "OS_AUTH_TYPE": "token",
        "OS_TOKEN": token.strip(),
    }


def _sap_docs_url(module: ModuleConfig) -> str:
    return str(module.settings.get("docs_url", "")).strip()


def _is_local_mcp_module(module: ModuleConfig) -> bool:
    return module.type in {"netbox_mcp", "openstack_mcp", "sap_docs_mcp"}


def _field_catalog_service(module: ModuleConfig) -> str:
    if module.type == "netbox_mcp":
        return "netbox"
    if module.type == "openstack_mcp":
        return "openstack"
    return ""


def _local_mcp_server_name(module: ModuleConfig) -> str:
    if module.type == "netbox_mcp":
        return "netbox-mcp-server"
    if module.type == "openstack_mcp":
        return "openstack-mcp-server"
    if module.type == "sap_docs_mcp":
        return "sap-docs-mcp-server"
    return ""


def _local_mcp_auth_configured(module: ModuleConfig, openstack_token: str = "", openstack_user: str = "") -> bool:
    if module.type == "netbox_mcp":
        return False
    if module.type == "openstack_mcp":
        settings = _openstack_settings(module, openstack_token)
        return bool(settings.get("OS_AUTH_URL") and (settings.get("OS_TOKEN") or os.getenv("OS_TOKEN", "").strip()))
    return False


def module_pid_path(module_id: str) -> Path:
    return PID_DIR / f"{module_id}.pid"


def module_log_path(module_id: str) -> Path:
    return LOG_DIR / f"{module_id}.log"


def module_index_path(module_id: str) -> Path:
    return RUNTIME_DIR / "indexes" / f"{module_id}.json"


def module_runtime_path(module_id: str) -> Path:
    return RUNTIME_DIR / "state" / f"{module_id}.json"


def module_query_cache_dir(module_id: str) -> Path:
    return RUNTIME_DIR / "query_cache" / module_id


def _auth_headers(module: ModuleConfig, *, force_auth: bool = False) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    secret = module_secret(module)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    elif force_auth:
        headers["Authorization"] = "Bearer "
    return headers


def _local_worker_headers(*, openstack_token: str = "", openstack_user: str = "") -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {internal_worker_token()}",
    }
    if openstack_user.strip():
        headers[OPENSTACK_USER_HEADER] = openstack_user.strip()
    if openstack_token.strip():
        headers[OPENSTACK_TOKEN_HEADER] = openstack_token.strip()
        headers.setdefault(OPENSTACK_USER_HEADER, "cli")
    return headers


def _redact_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = key.lower()
            if isinstance(item, (dict, list)):
                redacted[key] = _redact_mapping(item)
            elif isinstance(item, bool) or item is None:
                redacted[key] = item
            elif any(marker in normalized for marker in ("password", "secret", "token", "api_key")):
                redacted[key] = "***" if item else ""
            else:
                redacted[key] = _redact_mapping(item)
        return redacted
    if isinstance(value, list):
        return [_redact_mapping(item) for item in value]
    return value


def _inline_secret_paths(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if not isinstance(value, dict):
        return found
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        normalized = key.lower()
        if normalized.endswith("_env"):
            continue
        if any(marker in normalized for marker in ("password", "secret", "token", "api_key", "credential")):
            if isinstance(item, str) and item.strip():
                found.append(path)
        elif isinstance(item, dict):
            found.extend(_inline_secret_paths(item, path))
    return found


def _runtime_defaults(module_id: str) -> dict[str, Any]:
    return {
        "module_id": module_id,
        "last_start_attempt_at": "",
        "last_started_at": "",
        "last_stopped_at": "",
        "last_health_ok_at": "",
        "last_health_checked_at": "",
        "last_health_latency_ms": 0.0,
        "health_checks": 0,
        "health_cache_hits": 0,
        "last_test_at": "",
        "last_test_ok": False,
        "last_test_connected": False,
        "last_test_meaningful_output": False,
        "last_test_message": "",
        "last_discovery_at": "",
        "last_discovery_ok": False,
        "last_error": "",
        "last_start_error": "",
        "last_discovery_error": "",
        "last_discovered_tools": [],
        "last_execute_error": "",
        "last_field_cache_at": "",
        "last_field_cache_ok": False,
        "last_field_cache_error": "",
        "last_field_cache_resource_count": 0,
        "last_index_started_at": "",
        "last_index_completed_at": "",
        "last_index_duration_seconds": 0.0,
        "last_index_document_count": 0,
        "last_index_inventory_count": 0,
        "last_index_error": "",
        "index_job_active": False,
        "index_job_id": "",
        "index_job_status": "",
        "query_cache_hits": 0,
        "query_cache_disk_hits": 0,
        "query_cache_misses": 0,
        "query_cache_writes": 0,
        "last_query_duration_ms": 0.0,
        "restart_count": 0,
    }


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def load_module_runtime_state(module_id: str) -> dict[str, Any]:
    path = module_runtime_path(module_id)
    payload = _runtime_defaults(module_id)
    if not path.exists():
        return payload
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return payload
    if isinstance(raw, dict):
        payload.update(raw)
    return payload


def save_module_runtime_state(module_id: str, state: dict[str, Any]) -> dict[str, Any]:
    path = module_runtime_path(module_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = _runtime_defaults(module_id)
    merged.update(state)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return merged


def update_module_runtime_state(module_id: str, **updates: Any) -> dict[str, Any]:
    state = load_module_runtime_state(module_id)
    state.update(updates)
    return save_module_runtime_state(module_id, state)


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _worker_python_executable() -> str:
    venv_python = BASE_DIR / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    executable = sys.executable.strip()
    if executable and Path(executable).exists():
        return executable
    for candidate in ("python3", "python"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise RuntimeError("No Python interpreter found. Expected sys.executable or python3 on PATH.")


def module_worker_command(module: ModuleConfig) -> list[str]:
    python_executable = _worker_python_executable()
    if module.type == "netbox_mcp":
        return [python_executable, "-m", "app.worker_netbox", module.id, str(module.port)]
    if module.type == "openstack_mcp":
        return [python_executable, "-m", "app.worker_openstack", module.id, str(module.port)]
    if module.type == "sap_docs_mcp":
        return [python_executable, "-m", "app.worker_sap_docs", module.id, str(module.port)]
    return [python_executable, "-m", "app.worker", module.id]


def _append_module_log(module_id: str, message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with module_log_path(module_id).open("a", encoding="utf-8", buffering=1) as handle:
        handle.write(f"[{timestamp}] {message}\n")


def _read_module_log_tail(module_id: str, *, lines: int = 20) -> str:
    log_path = module_log_path(module_id)
    if not log_path.exists():
        return ""
    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _port_bindable(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        return True
    except OSError:
        return False


def _module_health_reachable(module: ModuleConfig, *, timeout: float = 1.0) -> bool:
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(_local_netbox_health_url(module) if _is_local_mcp_module(module) else f"{module_url(module)}/health")
            if response.status_code != 200:
                return False
            payload = response.json()
            if _is_local_mcp_module(module):
                ok = bool(payload.get("ok")) and str(payload.get("server", "")).strip() == _local_mcp_server_name(module)
            else:
                ok = str(payload.get("module_id", "")) == module.id
                if ok:
                    execute_response = client.post(
                        f"{module_url(module)}/execute",
                        headers=_local_worker_headers(),
                        json={"action": "health", "payload": {}},
                    )
                    ok = execute_response.status_code == 200
        if ok:
            update_module_runtime_state(module.id, last_health_ok_at=_timestamp())
        return ok
    except Exception:
        return False


def _module_health(module: ModuleConfig, *, timeout: float = 2.5) -> dict[str, Any] | None:
    cache_key = module.id
    cached = _HEALTH_CACHE.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < HEALTH_CACHE_TTL_SECONDS:
        state = load_module_runtime_state(module.id)
        update_module_runtime_state(
            module.id,
            health_cache_hits=int(state.get("health_cache_hits", 0)) + 1,
            last_health_checked_at=_timestamp(),
        )
        return cached[1]
    health: dict[str, Any] | None = None
    started_at = time.monotonic()
    if module.transport == "local" and module.port > 0:
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(_local_netbox_health_url(module) if _is_local_mcp_module(module) else f"{module_url(module)}/health")
                response.raise_for_status()
                payload = response.json()
                if _is_local_mcp_module(module):
                    if bool(payload.get("ok")) and str(payload.get("server", "")).strip() == _local_mcp_server_name(module):
                        health = payload
                elif str(payload.get("module_id", "")) == module.id:
                    health = payload
        except Exception:
            health = None
    _HEALTH_CACHE[cache_key] = (now, health)
    state = load_module_runtime_state(module.id)
    updates: dict[str, Any] = {
        "last_health_checked_at": _timestamp(),
        "last_health_latency_ms": round((time.monotonic() - started_at) * 1000.0, 2),
        "health_checks": int(state.get("health_checks", 0)) + 1,
    }
    if health is not None:
        updates["last_health_ok_at"] = _timestamp()
    update_module_runtime_state(module.id, **updates)
    return health


def _ensure_startable_port(module: ModuleConfig) -> ModuleConfig:
    if module.port <= 0 or module.port > 65535:
        module.port = reserve_port()
        upsert_module(module)
        _append_module_log(module.id, f"Reserved new port: {module.port}")
        return module
    if _module_health_reachable(module, timeout=0.5):
        return module
    if _port_bindable(module.host, module.port):
        return module
    previous_port = module.port
    module.port = reserve_port()
    upsert_module(module)
    _append_module_log(module.id, f"Port {previous_port} was already in use. New port: {module.port}")
    return module


def _spawn_worker(module: ModuleConfig) -> subprocess.Popen[str]:
    python_executable = _worker_python_executable()
    log_path = module_log_path(module.id)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["HARBOR_INTERNAL_WORKER_TOKEN"] = internal_worker_token()
    command = module_worker_command(module)
    if module.type == "netbox_mcp":
        env["NETBOX_URL"] = _netbox_url(module)
    if module.type == "openstack_mcp":
        env.update(
            {
                key: value
                for key, value in _openstack_settings(module).items()
                if value and key != "OS_TOKEN"
            }
        )
    _append_module_log(module.id, f"Starting worker for module {module.id} on {module.host}:{module.port} with {python_executable}")
    with log_path.open("a", encoding="utf-8", buffering=1) as handle:
        return subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
            text=True,
        )


def _cleanup_failed_start(module_id: str, process: subprocess.Popen[str] | None = None) -> None:
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    module_pid_path(module_id).unlink(missing_ok=True)


def _wait_for_worker_start(process: subprocess.Popen[str], module: ModuleConfig, *, timeout_seconds: float = 6.0) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _module_health_reachable(module, timeout=0.5):
            return True, ""
        returncode = process.poll()
        if returncode is not None:
            return False, f"Worker process exited early (exit code {returncode})."
        time.sleep(0.2)
    return False, f"Health check for {module.host}:{module.port} did not respond within {timeout_seconds:.1f}s."


def validate_module_config(module: ModuleConfig) -> list[str]:
    errors: list[str] = []
    if not module.id.strip():
        errors.append("Module ID is missing.")
    if module.type not in {"docs", "maildir", "mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"}:
        errors.append(f"Unknown module type: {module.type}")
    if module.transport not in {"local", "remote"}:
        errors.append(f"Invalid transport: {module.transport}")
    if module.remote_protocol not in {"auto", "harbor_execute", "mcp"}:
        errors.append(f"Invalid remote protocol: {module.remote_protocol}")
    if module.api_key.strip():
        errors.append("Inline API keys are not allowed; use api_key_env.")
    inline_secret_paths = _inline_secret_paths(module.settings)
    if inline_secret_paths:
        errors.append(
            "Inline secrets are not allowed; use environment references: "
            + ", ".join(sorted(inline_secret_paths))
        )
    if module.type in {"docs", "maildir"}:
        if module.transport != "local":
            errors.append(f"{module.type} must be local.")
        sources = module_sources(module, enabled_only=False)
        if not sources:
            errors.append("At least one local source is required.")
        seen_source_ids: set[str] = set()
        for source in sources:
            if not source.id.strip():
                errors.append("Source ID is missing.")
                continue
            if source.id in seen_source_ids:
                errors.append(f"Duplicate source ID: {source.id}")
            seen_source_ids.add(source.id)
            root = resolve_module_source_path(source)
            if not source.path.strip():
                errors.append(f"Path is missing for source {source.id}.")
            elif not root.exists():
                errors.append(f"Path does not exist: {root}")
            elif not root.is_dir():
                errors.append(f"Path is not a directory: {root}")
        if module.port < 0 or module.port > 65535:
            errors.append(f"Invalid port: {module.port}")
        if module.top_k <= 0:
            errors.append("top_k must be greater than 0.")
    if module.type == "mcp_http":
        if module.transport != "remote":
            errors.append("mcp_http must be remote.")
        if not module.base_url.strip():
            errors.append("Base URL is missing.")
        else:
            parsed = urlparse(module.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"Invalid Base URL: {module.base_url}")
    if module.type == "netbox_mcp":
        if module.transport != "local":
            errors.append("netbox_mcp must be local.")
        netbox_url = _netbox_url(module)
        if not netbox_url:
            errors.append("NetBox URL is missing.")
        else:
            parsed = urlparse(netbox_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"Invalid NetBox URL: {netbox_url}")
        if module.port < 0 or module.port > 65535:
            errors.append(f"Invalid port: {module.port}")
    if module.type == "openstack_mcp":
        if module.transport != "local":
            errors.append("openstack_mcp must be local.")
        openstack = _openstack_settings(module)
        if not openstack["OS_AUTH_URL"]:
            errors.append("OpenStack Auth URL is missing.")
        else:
            parsed = urlparse(openstack["OS_AUTH_URL"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"Invalid OpenStack Auth URL: {openstack['OS_AUTH_URL']}")
        if module.port < 0 or module.port > 65535:
            errors.append(f"Invalid port: {module.port}")
    if module.type == "sap_docs_mcp":
        if module.transport != "local":
            errors.append("sap_docs_mcp must be local.")
        docs_url = _sap_docs_url(module)
        if not docs_url:
            errors.append("SAP Docs URL is missing.")
        else:
            parsed = urlparse(docs_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"Invalid SAP Docs URL: {docs_url}")
        if module.port < 0 or module.port > 65535:
            errors.append(f"Invalid port: {module.port}")
    return errors


def validation_errors_by_module(modules: list[ModuleConfig] | None = None) -> dict[str, list[str]]:
    current_modules = modules or load_modules()
    errors = {module.id: list(validate_module_config(module)) for module in current_modules}
    port_usage: dict[tuple[str, int], list[str]] = {}
    for module in current_modules:
        if module.transport != "local" or module.type in {"docs", "maildir"} or module.port <= 0:
            continue
        key = (module.host, module.port)
        port_usage.setdefault(key, []).append(module.id)
    for (host, port), module_ids in port_usage.items():
        if len(module_ids) < 2:
            continue
        detail = f"Port conflict: {host}:{port} is used multiple times ({', '.join(sorted(module_ids))})."
        for module_id in module_ids:
            errors.setdefault(module_id, []).append(detail)
    return errors


@contextmanager
def _module_mutation_lock():
    MODULE_MUTATION_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MODULE_MUTATION_LOCK_PATH.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate_or_raise(module: ModuleConfig) -> None:
    errors = validate_module_config(module)
    if errors:
        raise ValueError(" ".join(errors))


def upsert_module(module: ModuleConfig) -> ModuleConfig:
    with _module_mutation_lock():
        modules = load_modules()
        replaced = False
        for index, existing in enumerate(modules):
            if existing.id == module.id:
                modules[index] = module
                replaced = True
                break
        if not replaced:
            modules.append(module)
        errors = validation_errors_by_module(modules).get(module.id, [])
        if errors:
            raise ValueError(" ".join(errors))
        from .config import save_modules

        save_modules(modules)
    return module


def remove_module(module_id: str) -> bool:
    with _module_mutation_lock():
        current_modules = load_modules()
        modules = [module for module in current_modules if module.id != module_id]
        if len(modules) == len(current_modules):
            return False
        from .config import save_modules

        save_modules(modules)
        return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_module_pid(module_id: str) -> int | None:
    pid_file = module_pid_path(module_id)
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    if not _pid_alive(pid):
        pid_file.unlink(missing_ok=True)
        return None
    return pid


def _index_meta_payload(module_id: str) -> SearchIndexMeta | None:
    return load_index_meta(module_index_path(module_id))


def module_status(module: ModuleConfig) -> dict[str, Any]:
    current_modules = load_modules()
    all_errors = validation_errors_by_module(current_modules)
    pid = read_module_pid(module.id)
    health = _module_health(module)
    running = pid is not None or health is not None
    sources = module_sources(module, enabled_only=False)
    runtime_state = load_module_runtime_state(module.id)
    index = _index_meta_payload(module.id) if module.type in {"docs", "maildir"} else None
    validation_errors = all_errors.get(module.id, [])
    field_catalog = load_field_catalog(module.id) if _field_catalog_service(module) else None
    return {
        "id": module.id,
        "name": module.display_name(),
        "type": module.type,
        "provider": module.provider,
        "enabled": module.enabled,
        "transport": module.transport,
        "remote_protocol": module.remote_protocol,
        "running": running,
        "pid": pid,
        "path": module.path,
        "source_count": len(sources),
        "enabled_source_count": len([source for source in sources if source.enabled]),
        "sources": [
            {
                "id": source.id,
                "label": source.display_name(),
                "path": source.path,
                "enabled": source.enabled,
            }
            for source in sources
        ],
        "base_url": module.base_url,
        "host": module.host,
        "port": module.port,
        "timeout_seconds": module.timeout_seconds,
        "top_k": module.top_k,
        "api_key_env": module.api_key_env,
        "notes": module.notes,
        "tool_names": module.tool_names,
        "test_action": module.test_action,
        "test_payload": _redact_mapping(module.test_payload),
        "test_expect_contains": module.test_expect_contains,
        "settings": _redact_mapping(module.settings),
        "health": health,
        "index_path": str(module_index_path(module.id)) if module.type in {"docs", "maildir"} else "",
        "index": {
            "exists": index is not None,
            "built_at": index.built_at if index else "",
            "document_count": index.document_count if index else 0,
            "inventory_count": index.inventory_count if index else 0,
        }
        if module.type in {"docs", "maildir"}
        else None,
        "cache": {
            "query_entries": _module_query_cache_count(module.id) if module.type in {"docs", "maildir"} else 0,
        },
        "field_catalog": {
            "ok": bool(field_catalog.get("ok")) if field_catalog else False,
            "updated_at": field_catalog.get("updated_at", "") if field_catalog else "",
            "service": field_catalog.get("service", "") if field_catalog else "",
            "resource_count": int(field_catalog.get("resource_count", 0) or 0) if field_catalog else 0,
            "cache_path": field_catalog.get("cache_path", "") if field_catalog else "",
            "errors": field_catalog.get("errors", []) if field_catalog else [],
        }
        if field_catalog
        else None,
        "runtime_state": runtime_state,
        "validation_errors": validation_errors,
    }


def module_overview(module: ModuleConfig) -> dict[str, Any]:
    status = module_status(module)
    validation_errors = status["validation_errors"]
    if not module.enabled:
        state = "disabled"
        tone = "muted"
    elif validation_errors:
        state = "invalid"
        tone = "danger"
    elif module.transport == "remote":
        state = "remote"
        tone = "info"
    elif status["running"]:
        state = "running"
        tone = "success"
    else:
        state = "stopped"
        tone = "warning"
    endpoint = module.base_url or module.path or f"http://{module.host}:{module.port}"
    return {
        "id": module.id,
        "name": module.display_name(),
        "type": module.type,
        "provider": module.provider,
        "transport": module.transport,
        "state": state,
        "tone": tone,
        "enabled": module.enabled,
        "endpoint": endpoint,
        "running": status["running"],
        "source_count": status["source_count"],
        "enabled_source_count": status["enabled_source_count"],
        "runtime_state": status["runtime_state"],
        "validation_errors": validation_errors,
        "status": status,
    }


def list_module_overview() -> list[dict[str, Any]]:
    return [module_overview(module) for module in load_modules()]


def health_check_module(
    module_id: str,
    *,
    openstack_token: str = "",
    openstack_user: str = "",
) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unknown module: {module_id}")
    errors = validation_errors_by_module().get(module.id, [])
    status = module_status(module)
    payload: dict[str, Any] = {
        "ok": not errors,
        "module_id": module_id,
        "validation_errors": errors,
        "status": status,
    }
    if module.type in {"docs", "maildir"}:
        index = _index_meta_payload(module.id)
        payload["index"] = {
            "exists": index is not None,
            "built_at": index.built_at if index else "",
            "document_count": index.document_count if index else 0,
        }
    if module.type == "mcp_http" and not errors:
        discovery = discover_remote_module(module)
        payload["remote"] = discovery
        payload["ok"] = payload["ok"] and bool(discovery.get("ok"))
    if module.type in {"netbox_mcp", "openstack_mcp", "sap_docs_mcp"} and not errors:
        effective_openstack_token = openstack_token.strip()
        if module.type == "openstack_mcp" and not effective_openstack_token:
            effective_openstack_token = os.getenv("OS_TOKEN", "").strip()
        payload["local"] = status.get("health")
        discovery = discover_standard_mcp_module(
            module,
            openstack_token=effective_openstack_token,
            openstack_user=openstack_user,
        )
        payload["remote"] = discovery
        payload["ok"] = payload["ok"] and bool(status.get("running")) and bool(discovery.get("ok"))
    return payload


def module_diagnostics(
    module_id: str,
    *,
    log_lines: int = 40,
    openstack_token: str = "",
    openstack_user: str = "",
) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unknown module: {module_id}")
    payload: dict[str, Any] = {
        "ok": True,
        "module_id": module_id,
        "log_path": str(module_log_path(module_id)),
        "log_tail": _read_module_log_tail(module_id, lines=log_lines),
        "errors": [],
    }
    try:
        payload["status"] = module_status(module)
    except Exception as exc:
        payload["ok"] = False
        payload["status"] = {"running": False, "error": str(exc)}
        payload["errors"].append(f"Status: {exc}")
    try:
        payload["health"] = health_check_module(
            module_id,
            openstack_token=openstack_token,
            openstack_user=openstack_user,
        )
        payload["ok"] = payload["ok"] and bool(payload["health"].get("ok"))
    except Exception as exc:
        payload["ok"] = False
        payload["health"] = {"ok": False, "error": str(exc)}
        payload["errors"].append(f"Health: {exc}")
    if module.type in {"mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"}:
        try:
            remote = discover_remote_module(
                module,
                openstack_token=openstack_token,
                openstack_user=openstack_user,
            )
            payload["remote"] = remote
            payload["ok"] = payload["ok"] and bool(remote.get("ok"))
        except Exception as exc:
            payload["ok"] = False
            payload["remote"] = {"ok": False, "error": str(exc)}
            payload["errors"].append(f"Discovery: {exc}")
    service = _field_catalog_service(module)
    if service:
        payload["field_catalog"] = load_field_catalog(module.id)
        source_tool = "discover_object_types" if service == "netbox" else "discover_resources"
        source_payload = {"limit": 25} if service == "netbox" else {"include_sample": False}
        effective_openstack_token = openstack_token.strip()
        if module.type == "openstack_mcp" and not effective_openstack_token:
            effective_openstack_token = os.getenv("OS_TOKEN", "").strip()
        try:
            payload["source_diagnostics"] = execute_module(
                module_id,
                source_tool,
                source_payload,
                openstack_token=effective_openstack_token,
                openstack_user=openstack_user,
            )
            payload["field_catalog"] = load_field_catalog(module.id)
        except Exception as exc:
            payload["ok"] = False
            payload["source_diagnostics"] = {"ok": False, "tool": source_tool, "error": str(exc)}
            payload["errors"].append(f"Source discovery: {exc}")
    if module.type in {"docs", "maildir", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"} or module.provider in {
        "netbox-mcp-server",
        "openstack-mcp-server",
        "sap-docs-mcp-server",
    }:
        try:
            payload["probes"] = module_default_probes(
                module_id,
                openstack_token=openstack_token,
                openstack_user=openstack_user,
            )
            if payload["probes"].get("probe_count"):
                payload["ok"] = payload["ok"] and bool(payload["probes"].get("ok"))
        except Exception as exc:
            payload["probes"] = {"ok": False, "error": str(exc)}
            payload["errors"].append(f"Default probes: {exc}")
            payload["ok"] = False
    payload["data_flow"] = {
        "diagnostic_path": [
            "module_diagnostics",
            "health_check_module/discover_remote_module",
            "source_diagnostics",
            "module_default_probes",
            "execute_module",
            "Worker/MCP Upstream",
        ],
        "llm_used": False,
        "note": "This diagnostics path does not call an LLM completion.",
    }
    if not payload["ok"] and module.transport == "local":
        payload["hint"] = _module_diagnostics_hint(module, payload)
    return payload


def _diagnostic_check(
    key: str,
    label: str,
    ok: bool,
    message: str,
    *,
    severity: str | None = None,
    detail: Any = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ok": bool(ok),
        "severity": severity or ("ok" if ok else "error"),
        "message": message,
        "detail": _redact_mapping(detail if detail is not None else {}),
    }


def _join_error_fragments(value: Any) -> str:
    fragments: list[str] = []

    def collect(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            if item.strip():
                fragments.append(item.strip())
            return
        if isinstance(item, dict):
            for key in ("error", "detail", "message", "hint", "errors"):
                collect(item.get(key))
            attempts = item.get("attempts")
            if isinstance(attempts, list):
                for attempt in attempts:
                    if isinstance(attempt, dict) and not attempt.get("ok", False):
                        label = str(attempt.get("label", "")).strip()
                        text = str(attempt.get("error") or attempt.get("body_preview") or "").strip()
                        status = attempt.get("status_code")
                        if text:
                            fragments.append(f"{label}: {text}" if label else text)
                        elif status:
                            fragments.append(f"{label}: HTTP {status}" if label else f"HTTP {status}")
            return
        if isinstance(item, list):
            for child in item:
                collect(child)

    collect(value)
    deduped: list[str] = []
    seen: set[str] = set()
    for fragment in fragments:
        normalized = fragment.lower()
        if normalized not in seen:
            deduped.append(fragment)
            seen.add(normalized)
    return " | ".join(deduped)


def _module_diagnostics_hint(module: ModuleConfig, payload: dict[str, Any]) -> str:
    error_text = _join_error_fragments(payload)
    lower = error_text.lower()
    if module.type == "openstack_mcp" and any(
        marker in lower
        for marker in (
            "credentials missing",
            "renew token",
            "projektgescoped",
            "project-scoped",
            "project scoped",
            "scope_validation",
            "unauthorized",
            "401",
        )
    ):
        return (
            "Renew OpenStack access for this Harbor user: "
            "save a project-scoped user token in chat or the admin dialog."
        )
    return f"Check or start the local worker: ./harbor.sh module start {module.id}"


def _connect_endpoint(module: ModuleConfig) -> str:
    return module.base_url or module.path or f"http://{module.host}:{module.port}"


def _module_browse_payload(diagnostics: dict[str, Any]) -> dict[str, Any] | None:
    remote = diagnostics.get("remote")
    if isinstance(remote, dict):
        return remote
    source = diagnostics.get("source_diagnostics")
    if isinstance(source, dict):
        return source
    return None


def _browse_summary(browse: dict[str, Any] | None) -> tuple[bool, str, dict[str, Any]]:
    if not browse:
        return False, "No discovery/browse response was produced.", {}
    tools = browse.get("tools") or browse.get("actions") or browse.get("capabilities") or []
    count = len(tools) if isinstance(tools, list) else 0
    if browse.get("ok"):
        if count:
            return True, f"Browse succeeded; {count} tool(s)/action(s) visible.", {"items": tools[:40]}
        return True, "Browse succeeded, but returned no tool/action list.", {}
    reason = _join_error_fragments(browse) or "Browse did not return a successful response."
    return False, f"Browse failed: {reason}", browse


def _structured_tool_payload(result: dict[str, Any]) -> Any:
    data = result.get("data")
    if isinstance(data, dict):
        structured = data.get("structuredContent")
        if isinstance(structured, dict) and "data" in structured:
            return structured.get("data")
        if "hits" in data or "document_count" in data or "messages" in data:
            return data
    return data if data is not None else result


def _probe_numeric_value(value: Any, candidates: set[str]) -> int | float | None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).replace("_", "").lower()
            if normalized in candidates and isinstance(item, (int, float)) and not isinstance(item, bool):
                return item
        for item in value.values():
            found = _probe_numeric_value(item, candidates)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _probe_numeric_value(item, candidates)
            if found is not None:
                return found
    return None


def _probe_collection_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        rows = data.get("results")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def _probe_collection_count(data: Any) -> int | None:
    if isinstance(data, dict):
        count = data.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            return count
        if isinstance(count, str) and count.isdigit():
            return int(count)
        rows = data.get("results")
        if isinstance(rows, list):
            return len(rows)
    if isinstance(data, list):
        return len(data)
    return None


def _probe_name(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("name", "Name", "display", "label", "id", "ID"):
            item = value.get(key)
            if item not in {None, ""}:
                if isinstance(item, dict):
                    nested = _probe_name(item)
                    if nested:
                        return nested
                return str(item)
    return str(value) if value not in {None, ""} else ""


def _probe_memory_mib(row: dict[str, Any]) -> int | float | None:
    candidates: list[Any] = [
        row.get("memory"),
        row.get("memory_mb"),
        row.get("ram"),
        row.get("ram_mb"),
    ]
    custom_fields = row.get("custom_fields")
    if isinstance(custom_fields, dict):
        candidates.extend(
            custom_fields.get(key)
            for key in ("memory", "memory_mb", "ram", "ram_mb", "arbeitsspeicher")
        )
    for value in candidates:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            cleaned = value.strip().replace("GiB", "").replace("GB", "").replace("MiB", "").replace("MB", "").strip()
            try:
                parsed = float(cleaned.replace(",", "."))
            except ValueError:
                continue
            if "gib" in value.lower() or re.search(r"\bgb\b", value.lower()):
                return parsed * 1024
            return parsed
    return None


def _summarize_openstack_server_count(data: Any) -> str:
    used = _probe_numeric_value(data, {"totalinstancesused", "instancesused", "usedinstances"})
    limit = _probe_numeric_value(data, {"maxtotalinstances", "instances", "maxinstances", "totalinstances"})
    if used is None and isinstance(data, dict):
        inventory = data.get("inventory")
        if isinstance(inventory, dict):
            server = inventory.get("server")
            if isinstance(server, dict):
                used = server.get("count")
    if used is None:
        return "The server count could not be read from the OpenStack response."
    used_text = int(used) if isinstance(used, float) and used.is_integer() else used
    if limit is None:
        return f"OpenStack reports {used_text} servers in the project."
    limit_text = int(limit) if isinstance(limit, float) and limit.is_integer() else limit
    return f"OpenStack reports {used_text} servers in the project; quota {used_text} of {limit_text}."


def _summarize_named_rows(data: Any, resource_label: str) -> str:
    rows = _probe_collection_rows(data)
    count = _probe_collection_count(data)
    names = [_probe_name(row) for row in rows[:10]]
    names = [name for name in names if name]
    prefix = f"{count} {resource_label}" if count is not None else f"{len(rows)} {resource_label}"
    if names:
        return f"{prefix}: {', '.join(names)}"
    return f"{prefix}; no names found in the response."


def _summarize_netbox_count(data: Any) -> str:
    count = _probe_collection_count(data)
    if count is None:
        return "The NetBox count could not be read from the response."
    return f"NetBox reports {count} matching systems/devices for manufacturer NetApp in eu-de-1."


def _summarize_memory_top10(data: Any) -> str:
    rows = _probe_collection_rows(data)
    ranked: list[tuple[float, str]] = []
    for row in rows:
        memory = _probe_memory_mib(row)
        if memory is None:
            continue
        name = _probe_name(row) or str(row.get("id") or row.get("ID") or "unknown")
        ranked.append((float(memory), name))
    ranked.sort(reverse=True)
    if not ranked:
        return "No RAM/memory values were found in the returned NetBox objects."
    parts = [f"{name}: {int(memory) if memory.is_integer() else round(memory, 1)} MiB" for memory, name in ranked[:10]]
    return "Top 10 by memory: " + "; ".join(parts)


def _summarize_docs_hits(data: Any) -> str:
    hits = []
    if isinstance(data, dict):
        if isinstance(data.get("hits"), list):
            hits = data["hits"]
        elif isinstance(data.get("results"), list):
            hits = data["results"]
    if not hits:
        return "No document hits found."
    parts: list[str] = []
    for item in hits[:3]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or item.get("location") or item.get("url") or "").strip()
        location = str(item.get("location") or item.get("url") or "").strip()
        if title and location and title != location:
            parts.append(f"{title} ({location})")
        elif title or location:
            parts.append(title or location)
    return f"{len(hits)} document hits: " + ("; ".join(parts) if parts else "hits without title/link.")


def _diagnostic_probe(
    module_id: str,
    label: str,
    question: str,
    tool: str,
    payload: dict[str, Any],
    summary_builder: Callable[[Any], str],
    *,
    openstack_token: str = "",
    openstack_user: str = "",
) -> dict[str, Any]:
    started_at = time.monotonic()
    try:
        result = execute_module(
            module_id,
            tool,
            payload,
            openstack_token=openstack_token,
            openstack_user=openstack_user,
        )
        data = _structured_tool_payload(result)
        return {
            "ok": True,
            "label": label,
            "question": question,
            "tool": tool,
            "payload": _redact_mapping(payload),
            "duration_ms": round((time.monotonic() - started_at) * 1000.0, 2),
            "summary": summary_builder(data),
            "data": _redact_mapping(data),
            "raw": _redact_mapping(result),
        }
    except Exception as exc:
        return {
            "ok": False,
            "label": label,
            "question": question,
            "tool": tool,
            "payload": _redact_mapping(payload),
            "duration_ms": round((time.monotonic() - started_at) * 1000.0, 2),
            "summary": f"Probe failed: {exc}",
            "error": str(exc),
        }


def _first_successful_probe(
    module_id: str,
    label: str,
    question: str,
    candidates: list[tuple[str, dict[str, Any], Callable[[Any], str]]],
    *,
    openstack_token: str = "",
    openstack_user: str = "",
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for tool, payload, summary_builder in candidates:
        probe = _diagnostic_probe(
            module_id,
            label,
            question,
            tool,
            payload,
            summary_builder,
            openstack_token=openstack_token,
            openstack_user=openstack_user,
        )
        attempts.append(probe)
        if probe["ok"]:
            if len(attempts) > 1:
                probe["attempts"] = [dict(attempt) for attempt in attempts]
            return probe
    failed = attempts[-1] if attempts else {
        "ok": False,
        "label": label,
        "question": question,
        "tool": "",
        "payload": {},
        "summary": "No probe defined.",
    }
    return {**failed, "attempts": attempts}


def module_default_probes(
    module_id: str,
    *,
    openstack_token: str = "",
    openstack_user: str = "",
) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unknown module: {module_id}")
    probes: list[dict[str, Any]] = []
    if module.type == "openstack_mcp" or module.provider == "openstack-mcp-server" or module.id == "openstack":
        effective_token = openstack_token.strip() or os.getenv("OS_TOKEN", "").strip()
        probes.extend(
            [
                _first_successful_probe(
                    module_id,
                    "OpenStack servers in project",
                    "How many servers are in my project?",
                    [
                        ("get_compute_limits", {}, _summarize_openstack_server_count),
                        ("get_project_statistics", {}, _summarize_openstack_server_count),
                    ],
                    openstack_token=effective_token,
                    openstack_user=openstack_user,
                ),
                _diagnostic_probe(
                    module_id,
                    "OpenStack networks",
                    "Which networks do you see?",
                    "list_networks",
                    {"limit": 20},
                    lambda data: _summarize_named_rows(data, "network(s)"),
                    openstack_token=effective_token,
                    openstack_user=openstack_user,
                ),
                _diagnostic_probe(
                    module_id,
                    "OpenStack Flavor",
                    "Which flavors are available?",
                    "list_flavors",
                    {"limit": 20},
                    lambda data: _summarize_named_rows(data, "Flavor"),
                    openstack_token=effective_token,
                    openstack_user=openstack_user,
                ),
                _diagnostic_probe(
                    module_id,
                    "OpenStack regions/zones",
                    "Which regions or availability zones are available?",
                    "list_availability_zones",
                    {"limit": 20},
                    lambda data: _summarize_named_rows(data, "Zone(n)"),
                    openstack_token=effective_token,
                    openstack_user=openstack_user,
                ),
            ]
        )
    elif module.type == "netbox_mcp" or module.provider == "netbox-mcp-server" or module.id == "netbox":
        probes.extend(
            [
                _first_successful_probe(
                    module_id,
                    "NetBox NetApp in eu-de-1",
                    "How many NetApp systems are in eu-de-1?",
                    [
                        (
                            "get_objects",
                            {
                                "object_type": "dcim.devices",
                                "filters": {"device_type__manufacturer": "NetApp", "site": "eu-de-1"},
                                "limit": 1,
                                "fetch_all": False,
                            },
                            _summarize_netbox_count,
                        ),
                        (
                            "get_objects",
                            {
                                "object_type": "dcim.devices",
                                "filters": {"manufacturer": "NetApp", "site": "eu-de-1"},
                                "limit": 1,
                                "fetch_all": False,
                            },
                            _summarize_netbox_count,
                        ),
                        (
                            "get_objects",
                            {
                                "object_type": "dcim.devices",
                                "filters": {"q": "NetApp eu-de-1"},
                                "limit": 10,
                                "fetch_all": False,
                            },
                            _summarize_netbox_count,
                        ),
                    ],
                ),
                _first_successful_probe(
                    module_id,
                    "NetBox memory top 10",
                    "Which servers have the most memory? Please return the top 10.",
                    [
                        (
                            "get_objects",
                            {
                                "object_type": "virtualization.virtual-machines",
                                "filters": {"ordering": "-memory"},
                                "fields": ["id", "name", "memory", "site", "cluster", "status"],
                                "limit": 10,
                                "fetch_all": False,
                            },
                            _summarize_memory_top10,
                        ),
                        (
                            "get_objects",
                            {
                                "object_type": "dcim.devices",
                                "filters": {"ordering": "-custom_fields.memory"},
                                "fields": ["id", "name", "site", "device_type", "custom_fields", "status"],
                                "limit": 100,
                                "fetch_all": False,
                            },
                            _summarize_memory_top10,
                        ),
                    ],
                ),
            ]
        )
    elif module.type in {"docs", "maildir"}:
        for label, query in (
            ("Docs Cinder vs Manila", "what is the difference between cinder and manila"),
            ("Docs regions", "which regions are available"),
            ("Docs flavor", "which flavors are available"),
        ):
            probes.append(
                _diagnostic_probe(
                    module_id,
                    label,
                    query,
                    "search",
                    {"query": query, "top_k": 3},
                    _summarize_docs_hits,
                )
            )
    elif module.type == "sap_docs_mcp":
        for label, query in (
            ("Docs Cinder vs Manila", "cinder manila difference"),
            ("Docs regions", "available regions"),
            ("Docs flavor", "available flavors"),
        ):
            probes.append(
                _diagnostic_probe(
                    module_id,
                    label,
                    query,
                    "search_sap_docs",
                    {"query": query, "limit": 5},
                    _summarize_docs_hits,
                )
            )
    ok_probes = [probe for probe in probes if probe.get("ok")]
    return {
        "ok": bool(probes) and len(ok_probes) == len(probes),
        "module_id": module.id,
        "type": module.type,
        "probe_count": len(probes),
        "ok_probe_count": len(ok_probes),
        "summary": (
            f"{len(ok_probes)} of {len(probes)} default probe(s) succeeded."
            if probes
            else f"No domain-specific default probes are defined for module type {module.type}."
        ),
        "data_flow": {
            "path": "module_default_probes -> execute_module -> worker/MCP upstream",
            "llm_used": False,
            "note": "These probes bypass the LLM completely and check the source directly.",
        },
        "probes": probes,
    }


def _connect_next_steps(module: ModuleConfig, checks: list[dict[str, Any]], error_text: str) -> list[str]:
    lower = error_text.lower()
    failed_keys = {check["key"] for check in checks if not check.get("ok")}
    steps: list[str] = []

    config_check = next((check for check in checks if check["key"] == "config"), None)
    config_detail = config_check.get("detail") if isinstance(config_check, dict) else None
    if isinstance(config_detail, list) and config_detail:
        steps.append("Fix module configuration: " + "; ".join(str(item) for item in config_detail[:4]))

    if "enabled" in failed_keys:
        steps.append("Enable the module or leave it intentionally disabled; disabled modules are not used in chat.")

    if module.transport == "local" and "worker" in failed_keys:
        steps.append(f"Start or restart the local worker: ./harbor.sh module start {module.id}")
        steps.append(f"Check the worker log: {module_log_path(module.id)}")

    if "index" in failed_keys:
        steps.append(f"Rebuild the local search index: ./harbor.sh module reindex {module.id}")

    if "fields" in failed_keys:
        steps.append("Refresh the field catalog from the module view, then rerun Browse/Test.")

    if "credential" in lower or "token" in lower or "unauthorized" in lower or "401" in lower:
        if module.type == "openstack_mcp":
            steps.append("Renew OpenStack access for this Harbor user: save a project-scoped user token.")
        elif module.type == "netbox_mcp":
            steps.append("Make the NetBox API reachable anonymously from the Harbor network; check the URL, trusted networks, and read-only API permissions.")
        else:
            steps.append("Check the module API token/auth configuration, then rerun Browse.")

    if "projektgescoped" in lower or "project-scoped" in lower or "project scoped" in lower:
        steps.append("The OpenStack token is not project-scoped: create the token directly in the target project and save it again.")

    if "connection refused" in lower or "connect call failed" in lower:
        steps.append("The target process is not listening on the configured host/port; check the port, service, and firewall.")

    if "temporary failure in name resolution" in lower or "name or service not known" in lower or "dns" in lower:
        steps.append("Check upstream DNS/URL; the name must resolve from the Harbor host.")

    if "timed out" in lower or "timeout" in lower:
        steps.append("The upstream is too slow or blocked; check reachability and increase the module timeout if needed.")

    if "browse" in failed_keys or "test" in failed_keys:
        steps.append("Compare Browse JSON and the module test: discovery must return tools and the test must return meaningful data.")

    if not steps and failed_keys:
        steps.append("Check the raw JSON, health block, and log excerpt in this view; they contain the latest technical cause.")
    if not steps:
        steps.append("No Connect problem is visible. If chat still fails, check role/tool permissions and prompt routing.")

    deduped: list[str] = []
    for step in steps:
        if step not in deduped:
            deduped.append(step)
    return deduped


def module_connect_diagnostics(
    module_id: str,
    *,
    openstack_token: str = "",
    openstack_user: str = "",
    run_checks: bool = True,
    log_lines: int = 60,
) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unknown module: {module_id}")

    status = module_status(module)
    validation_errors = status.get("validation_errors") or []
    checks: list[dict[str, Any]] = [
        _diagnostic_check(
            "config",
            "Configuration",
            not validation_errors,
            "Configuration is valid." if not validation_errors else "Configuration blocks the module.",
            detail=validation_errors,
        ),
        _diagnostic_check(
            "enabled",
            "Activation",
            bool(module.enabled),
            "Module is enabled." if module.enabled else "Module is disabled.",
            severity="warning" if not module.enabled else "ok",
        ),
    ]

    if module.transport == "local":
        running = bool(status.get("running"))
        checks.append(
            _diagnostic_check(
                "worker",
                "Worker",
                running,
                "Local worker responds to health checks."
                if running
                else "Local worker does not respond to health checks.",
                detail={"pid": status.get("pid"), "health": status.get("health")},
            )
        )
    else:
        checks.append(
            _diagnostic_check(
                "worker",
                "Worker",
                True,
                "Remote module does not need a Harbor worker.",
                severity="ok",
                detail={"endpoint": _connect_endpoint(module)},
            )
        )

    if module.type == "openstack_mcp":
        effective_openstack_token = openstack_token.strip() or os.getenv("OS_TOKEN", "").strip()
        checks.append(
            _diagnostic_check(
                "credential",
                "OpenStack Token",
                bool(effective_openstack_token),
                "A project-scoped user token is present for this diagnostics run."
                if effective_openstack_token
                else "No OpenStack user token is stored for this Harbor user.",
                detail={
                    "token_present": bool(effective_openstack_token),
                    "token_source": "request_or_user_secret" if openstack_token.strip() else "environment" if effective_openstack_token else "missing",
                    "scope_mode": "project_from_token",
                },
            )
        )

    if module.type == "netbox_mcp":
        checks.append(
            _diagnostic_check(
                "auth_mode",
                "NetBox Auth",
                True,
                "NetBox is queried anonymously and read-only.",
                severity="ok",
                detail={"authentication": "anonymous", "read_only": True, "netbox_url": _netbox_url(module)},
            )
        )

    index = status.get("index")
    if isinstance(index, dict):
        indexed = bool(index.get("exists"))
        checks.append(
            _diagnostic_check(
                "index",
                "Index",
                indexed,
                f"Index exists with {index.get('document_count', 0)} document(s)."
                if indexed
                else "No local search index exists yet.",
                severity="warning" if not indexed else "ok",
                detail=index,
            )
        )

    field_catalog = status.get("field_catalog")
    if isinstance(field_catalog, dict):
        field_ok = bool(field_catalog.get("ok"))
        checks.append(
            _diagnostic_check(
                "fields",
                "Field Catalog",
                field_ok,
                f"Field catalog contains {field_catalog.get('resource_count', 0)} resource(s)."
                if field_ok
                else "Field catalog is empty or invalid.",
                severity="warning" if not field_ok else "ok",
                detail=field_catalog,
            )
        )

    diagnostics: dict[str, Any] | None = None
    test_result: dict[str, Any] | None = None
    browse: dict[str, Any] | None = None
    probes: dict[str, Any] | None = None

    if run_checks:
        diagnostics = module_diagnostics(
            module_id,
            log_lines=log_lines,
            openstack_token=openstack_token,
            openstack_user=openstack_user,
        )
        checks.append(
            _diagnostic_check(
                "diagnose",
                "Diagnostics",
                bool(diagnostics.get("ok")),
                "Diagnostics report no technical errors."
                if diagnostics.get("ok")
                else (_join_error_fragments(diagnostics) or "Diagnostics found errors."),
                detail={
                    "errors": diagnostics.get("errors", []),
                    "hint": diagnostics.get("hint", ""),
                    "health": diagnostics.get("health", {}),
                },
            )
        )
        source_diagnostics = diagnostics.get("source_diagnostics")
        if isinstance(source_diagnostics, dict):
            source_ok = bool(source_diagnostics.get("ok", True)) and not source_diagnostics.get("error")
            source_tool = str(source_diagnostics.get("tool") or "").strip()
            checks.append(
                _diagnostic_check(
                    "source",
                    "Upstream Data",
                    source_ok,
                    f"{source_tool or 'Source discovery'} returns usable data."
                    if source_ok
                    else (
                        f"{source_tool or 'Source discovery'} failed: "
                        f"{source_diagnostics.get('error') or _join_error_fragments(source_diagnostics) or 'unknown error'}"
                    ),
                    detail=source_diagnostics,
                )
            )
        browse = _module_browse_payload(diagnostics)
        if module.type in {"mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"}:
            browse_ok, browse_message, browse_detail = _browse_summary(browse)
            checks.append(
                _diagnostic_check(
                    "browse",
                    "Browse",
                    browse_ok,
                    browse_message,
                    detail=browse_detail,
                )
            )
        test_result = module_test(
            module_id,
            openstack_token=openstack_token,
            openstack_user=openstack_user,
        )
        if test_result.get("ok"):
            test_message = str(test_result.get("message") or "Module test succeeded.")
        elif test_result.get("connected"):
            test_message = str(test_result.get("message") or "Connection is up, but the output is not meaningful.")
        else:
            test_message = str(test_result.get("message") or "Module test could not connect.")
        checks.append(
            _diagnostic_check(
                "test",
                "Module Test",
                bool(test_result.get("ok")),
                test_message,
                severity="warning" if test_result.get("connected") and not test_result.get("ok") else None,
                detail={
                    "action": test_result.get("action"),
                    "connected": test_result.get("connected"),
                    "meaningful_output": test_result.get("meaningful_output"),
                    "expected_terms": test_result.get("expected_terms", []),
                    "output_summary": test_result.get("output_summary", ""),
                },
            )
        )

        if module.type in {"docs", "maildir", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"} or module.provider in {
            "netbox-mcp-server",
            "openstack-mcp-server",
            "sap-docs-mcp-server",
        }:
            diagnostics_probes = diagnostics.get("probes") if isinstance(diagnostics, dict) else None
            probes = diagnostics_probes if isinstance(diagnostics_probes, dict) else module_default_probes(
                module_id,
                openstack_token=openstack_token,
                openstack_user=openstack_user,
            )
            probe_count = int(probes.get("probe_count", 0) or 0)
            ok_probe_count = int(probes.get("ok_probe_count", 0) or 0)
            checks.append(
                _diagnostic_check(
                    "default_probes",
                    "Domain Probes",
                    bool(probes.get("ok")),
                    str(probes.get("summary") or "Default probes executed."),
                    severity="ok" if probes.get("ok") else "warning" if ok_probe_count else "error",
                    detail={
                        "probe_count": probe_count,
                        "ok_probe_count": ok_probe_count,
                        "data_flow": probes.get("data_flow", {}),
                    },
                )
            )

    has_error = any(not check.get("ok") and check.get("severity") != "warning" for check in checks)
    has_warning = any(not check.get("ok") or check.get("severity") == "warning" for check in checks)
    severity = "error" if has_error else "warning" if has_warning else "ok"
    error_text = _join_error_fragments(
        {
            "checks": checks,
            "diagnostics": diagnostics or {},
            "test": test_result or {},
            "runtime": status.get("runtime_state", {}),
        }
    )
    next_steps = _connect_next_steps(module, checks, error_text)
    if severity == "ok":
        summary = "Connect path is clean: status, Browse, and test return usable responses." if run_checks else "Base status has no visible Connect errors."
    elif severity == "warning":
        summary = "Connect path is reachable, but there are warnings or incomplete data."
    else:
        failing = next((check for check in checks if not check.get("ok") and check.get("severity") != "warning"), None)
        summary = failing["message"] if failing else "Connect diagnostics found blocking errors."

    return {
        "ok": severity != "error",
        "severity": severity,
        "module_id": module.id,
        "name": module.display_name(),
        "type": module.type,
        "transport": module.transport,
        "endpoint": _connect_endpoint(module),
        "ran_checks": run_checks,
        "updated_at": _timestamp(),
        "summary": summary,
        "checks": checks,
        "next_steps": next_steps,
        "data_flow": {
            "chat_path": [
                "Browser/CLI",
                "Harbor API /api/chat or /api/chat/stream",
                "_context_for_chat",
                "execute_module",
                "Worker/MCP Upstream",
                "LLM only for wording when no direct deterministic answer path applies",
            ],
            "diagnostic_path": [
                "Connect Diagnostics",
                "module_connect_diagnostics",
                "module_default_probes",
                "execute_module",
                "Worker/MCP Upstream",
            ],
            "diagnostic_llm_used": False,
        },
        "status": status,
        "browse": browse,
        "diagnostics": diagnostics,
        "test": test_result,
        "probes": probes,
    }


def module_field_catalog(module_id: str) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unknown module: {module_id}")
    if not _field_catalog_service(module):
        raise ValueError(f"Field catalog is not available for module type {module.type}.")
    return load_field_catalog(module.id)


def _update_field_catalog_from_result(
    module: ModuleConfig,
    tool_name: str,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    service = _field_catalog_service(module)
    if not service:
        return None
    catalog = update_catalog_from_tool_result(module.id, service, tool_name, result)
    if catalog is None:
        return None
    update_module_runtime_state(
        module.id,
        last_field_cache_at=_timestamp(),
        last_field_cache_ok=bool(catalog.get("ok")),
        last_field_cache_error="; ".join(str(item) for item in catalog.get("errors", []) if item),
        last_field_cache_resource_count=int(catalog.get("resource_count", 0) or 0),
    )
    return catalog


def refresh_module_field_catalog(
    module_id: str,
    *,
    openstack_token: str = "",
    openstack_user: str = "",
    limit: int = 25,
) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unknown module: {module_id}")
    service = _field_catalog_service(module)
    if not service:
        raise ValueError(f"Field catalog is not available for module type {module.type}.")
    limit = max(1, min(500, int(limit or 25)))
    errors: list[str] = []
    try:
        if service == "openstack":
            effective_openstack_token = openstack_token.strip() or os.getenv("OS_TOKEN", "").strip()
            if not effective_openstack_token and not openstack_user.strip():
                raise ValueError(
                    "OpenStack credentials are missing for this user. "
                    "Save a project-scoped user token in the OpenStack dialog."
                )
            execute_module(
                module.id,
                "discover_resources",
                {"include_sample": False},
                openstack_token=effective_openstack_token,
                openstack_user=openstack_user,
            )
        else:
            discovery_result = execute_module(
                module.id,
                "discover_object_types",
                {"limit": limit},
                openstack_token=openstack_token,
                openstack_user=openstack_user,
            )
            discovery_data = (
                discovery_result.get("data", {})
                .get("structuredContent", {})
                .get("data", {})
            )
            object_types = [
                str(item.get("object_type", "")).strip()
                for item in discovery_data.get("object_types", [])
                if isinstance(item, dict) and str(item.get("object_type", "")).strip()
            ]
            for object_type in object_types[:limit]:
                try:
                    execute_module(
                        module.id,
                        "describe_object_type",
                        {
                            "object_type": object_type,
                            "max_fields": 500,
                            "include_sample": False,
                        },
                        openstack_token=openstack_token,
                        openstack_user=openstack_user,
                    )
                except Exception as exc:
                    errors.append(f"{object_type}: {exc}")
        catalog = load_field_catalog(module.id)
        if errors:
            catalog = save_field_catalog(
                module.id,
                service,
                catalog.get("resources", {}) if isinstance(catalog.get("resources"), dict) else {},
                errors=errors,
            )
        update_module_runtime_state(
            module.id,
            last_field_cache_at=_timestamp(),
            last_field_cache_ok=not errors and bool(catalog.get("ok")),
            last_field_cache_error="; ".join(errors),
            last_field_cache_resource_count=int(catalog.get("resource_count", 0) or 0),
        )
        return catalog
    except Exception as exc:
        update_module_runtime_state(
            module.id,
            last_field_cache_at=_timestamp(),
            last_field_cache_ok=False,
            last_field_cache_error=str(exc),
        )
        raise


def _default_test_config(module: ModuleConfig) -> tuple[str, dict[str, Any], list[str]]:
    if module.type == "docs":
        query = str(module.settings.get("default_test_query", "")).strip()
        if query:
            return "search", {"query": query, "top_k": module.top_k}, []
        return "stats", {}, []
    if module.type == "maildir":
        query = str(module.settings.get("default_test_query", "")).strip()
        if query:
            return "search", {"query": query, "top_k": module.top_k}, []
        return "stats", {}, []
    if module.type == "mcp_http":
        if module.remote_protocol == "mcp" or module.base_url.rstrip("/").endswith("/mcp"):
            if module.tool_names:
                return module.tool_names[0], {}, []
            return "discover", {}, []
        return "health", {}, []
    if module.type in {"netbox_mcp", "openstack_mcp", "sap_docs_mcp"}:
        if module.tool_names:
            return module.tool_names[0], {}, []
        return "discover", {}, []
    return "health", {}, []


def _contains_expected_terms(output_text: str, expected_terms: list[str]) -> bool:
    if not expected_terms:
        return True
    normalized = output_text.lower()
    return all(term.lower() in normalized for term in expected_terms)


def module_test(
    module_id: str,
    *,
    openstack_token: str = "",
    openstack_user: str = "",
) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unknown module: {module_id}")
    action, payload, expected_terms = _default_test_config(module)
    if module.test_action.strip():
        action = module.test_action.strip()
    if module.test_payload:
        payload = module.test_payload
    if module.test_expect_contains:
        expected_terms = module.test_expect_contains
    result: dict[str, Any]
    connected = False
    meaningful_output = False
    output_summary = ""
    try:
        if module.type in {"mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"} and action == "discover":
            result = discover_remote_module(
                module,
                openstack_token=openstack_token,
                openstack_user=openstack_user,
            )
            connected = bool(result.get("ok"))
            tools = result.get("tools") or result.get("actions") or []
            meaningful_output = bool(tools)
            output_summary = json.dumps(tools, ensure_ascii=False)
        else:
            result = execute_module(
                module.id,
                action,
                payload,
                openstack_token=openstack_token,
                openstack_user=openstack_user,
            )
            connected = bool(result.get("ok", True))
            result_data = result.get("data", result)
            if module.type in {"docs", "maildir"} and action == "search":
                hits = result_data.get("hits", [])
                meaningful_output = bool(hits)
                output_summary = json.dumps(hits[:3], ensure_ascii=False)
            elif module.type in {"docs", "maildir"} and action == "stats":
                meaningful_output = int(result_data.get("document_count", 0) or result_data.get("messages", 0) or 0) >= 0
                output_summary = json.dumps(result_data, ensure_ascii=False)
            elif module.type in {"mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"}:
                output_summary = json.dumps(result_data, ensure_ascii=False)
                meaningful_output = bool(output_summary.strip() and output_summary not in {"{}", "[]", '""'})
            else:
                output_summary = json.dumps(result_data, ensure_ascii=False)
                meaningful_output = bool(output_summary.strip())
            if meaningful_output and expected_terms:
                meaningful_output = _contains_expected_terms(output_summary, expected_terms)
        message = "Module test succeeded." if connected and meaningful_output else "Connection is OK, but the output is not meaningful enough."
        if not connected:
            message = "Connection test failed."
        update_module_runtime_state(
            module.id,
            last_test_at=_timestamp(),
            last_test_ok=connected and meaningful_output,
            last_test_connected=connected,
            last_test_meaningful_output=meaningful_output,
            last_test_message=message,
            last_error="" if connected else message,
        )
        return {
            "ok": connected and meaningful_output,
            "connected": connected,
            "meaningful_output": meaningful_output,
            "module_id": module_id,
            "action": action,
            "payload": payload,
            "expected_terms": expected_terms,
            "message": message,
            "result": result,
            "output_summary": output_summary[:1200],
        }
    except Exception as exc:
        update_module_runtime_state(
            module.id,
            last_test_at=_timestamp(),
            last_test_ok=False,
            last_test_connected=False,
            last_test_meaningful_output=False,
            last_test_message=str(exc),
            last_execute_error=str(exc),
            last_error=str(exc),
        )
        return {
            "ok": False,
            "connected": False,
            "meaningful_output": False,
            "module_id": module_id,
            "action": action,
            "payload": payload,
            "expected_terms": expected_terms,
            "message": str(exc),
        }


def _mcp_endpoint(module: ModuleConfig) -> str:
    if _is_local_mcp_module(module):
        return f"{module_url(module)}/mcp"
    return module.base_url.rstrip("/")


def _mcp_request(
    client: httpx.Client,
    module: ModuleConfig,
    session_id: str | None,
    method: str,
    params: dict[str, Any] | None,
    *,
    request_id: int | None,
    openstack_token: str = "",
    openstack_user: str = "",
) -> tuple[dict[str, Any], str | None]:
    headers = (
        _local_worker_headers(openstack_token=openstack_token, openstack_user=openstack_user)
        if _is_local_mcp_module(module)
        else _auth_headers(module)
    )
    if session_id:
        headers["mcp-session-id"] = session_id
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    response = client.post(_mcp_endpoint(module), headers=headers, json=body)
    response.raise_for_status()
    next_session_id = response.headers.get("mcp-session-id") or session_id
    if request_id is None:
        return {"ok": True}, next_session_id
    payload = response.json()
    if "error" in payload:
        detail = _redact_mapping(payload["error"])
        raise ValueError(
            f"MCP error {method}: "
            f"{json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)}"
        )
    return payload, next_session_id


def _mcp_session(
    client: httpx.Client,
    module: ModuleConfig,
    *,
    openstack_token: str = "",
    openstack_user: str = "",
) -> str | None:
    response, session_id = _mcp_request(
        client,
        module,
        None,
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "woddi-harbor", "version": __version__},
        },
        request_id=1,
        openstack_token=openstack_token,
        openstack_user=openstack_user,
    )
    _mcp_request(
        client,
        module,
        session_id,
        "notifications/initialized",
        {},
        request_id=None,
        openstack_token=openstack_token,
        openstack_user=openstack_user,
    )
    return session_id or response.get("result", {}).get("sessionId")


def _call_mcp_tool(
    module: ModuleConfig,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    openstack_token: str = "",
    openstack_user: str = "",
) -> dict[str, Any]:
    with httpx.Client(timeout=module.timeout_seconds) as client:
        session_id = _mcp_session(
            client,
            module,
            openstack_token=openstack_token,
            openstack_user=openstack_user,
        )
        payload, _ = _mcp_request(
            client,
            module,
            session_id,
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            request_id=3,
            openstack_token=openstack_token,
            openstack_user=openstack_user,
        )
    return payload.get("result", payload)


def discover_remote_module(
    module: ModuleConfig,
    *,
    openstack_token: str = "",
    openstack_user: str = "",
) -> dict[str, Any]:
    if _is_local_mcp_module(module):
        return discover_standard_mcp_module(
            module,
            openstack_token=openstack_token,
            openstack_user=openstack_user,
        )
    if module.remote_protocol == "mcp" or module.base_url.rstrip("/").endswith("/mcp"):
        return discover_standard_mcp_module(module)
    base_url = module.base_url.rstrip("/")
    timeout = min(module.timeout_seconds, 10.0)
    attempts: list[dict[str, Any]] = []
    capabilities: list[str] = []
    actions: list[str] = []

    candidate_calls = [
        ("GET", "/", None, "root"),
        ("GET", "/health", None, "health"),
        ("GET", "/capabilities", None, "capabilities"),
        ("GET", "/.well-known/mcp", None, "well_known_mcp"),
        ("POST", "/execute", {"action": "capabilities", "payload": {}}, "execute_capabilities"),
        ("POST", "/execute", {"action": "list_capabilities", "payload": {}}, "execute_list_capabilities"),
        ("POST", "/execute", {"action": "health", "payload": {}}, "execute_health"),
    ]

    def extract_details(body: Any) -> tuple[list[str], list[str]]:
        local_capabilities: list[str] = []
        local_actions: list[str] = []
        if isinstance(body, dict):
            for key in ("capabilities", "supported_capabilities", "features"):
                value = body.get(key)
                if isinstance(value, list):
                    local_capabilities.extend(str(item) for item in value)
                elif isinstance(value, dict):
                    local_capabilities.extend(str(item) for item in value.keys())
            for key in ("actions", "supported_actions", "tools", "methods"):
                value = body.get(key)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and "name" in item:
                            local_actions.append(str(item["name"]))
                        else:
                            local_actions.append(str(item))
                elif isinstance(value, dict):
                    local_actions.extend(str(item) for item in value.keys())
        return local_capabilities, local_actions

    secret_present = bool(module_secret(module))
    auth_modes = [False, True] if secret_present else [False]

    with httpx.Client(timeout=timeout) as client:
        for use_auth in auth_modes:
            headers = _auth_headers(module, force_auth=False) if use_auth else {"Content-Type": "application/json"}
            for method, suffix, body, label in candidate_calls:
                url = base_url + suffix
                try:
                    if method == "GET":
                        response = client.get(url, headers=headers)
                    else:
                        response = client.post(url, headers=headers, json=body)
                    content_type = response.headers.get("content-type", "")
                    parsed_body: Any
                    if "json" in content_type:
                        try:
                            parsed_body = response.json()
                        except Exception:
                            parsed_body = response.text[:800]
                    else:
                        parsed_body = response.text[:800]
                    attempt = {
                        "label": label,
                        "auth": use_auth,
                        "method": method,
                        "url": url,
                        "status_code": response.status_code,
                        "ok": response.is_success,
                    }
                    if response.is_success:
                        attempt["body"] = parsed_body
                        new_capabilities, new_actions = extract_details(parsed_body)
                        capabilities.extend(new_capabilities)
                        actions.extend(new_actions)
                    else:
                        attempt["body_preview"] = response.text[:300]
                    attempts.append(attempt)
                except Exception as exc:
                    attempts.append(
                        {
                            "label": label,
                            "auth": use_auth,
                            "method": method,
                            "url": url,
                            "ok": False,
                            "error": str(exc),
                        }
                    )

    dedup_capabilities = sorted({item for item in capabilities if item})
    dedup_actions = sorted({item for item in actions if item})
    successful = [attempt for attempt in attempts if attempt.get("ok")]
    if successful:
        update_module_runtime_state(
            module.id,
            last_discovery_at=_timestamp(),
            last_discovery_ok=True,
            last_discovery_error="",
            last_discovered_tools=dedup_actions,
        )
    else:
        update_module_runtime_state(
            module.id,
            last_discovery_at=_timestamp(),
            last_discovery_ok=False,
            last_discovery_error="Discovery produced no successful response.",
            last_error="Discovery produced no successful response.",
        )
    return {
        "ok": bool(successful),
        "base_url": base_url,
        "auth_configured": secret_present,
        "successful_attempts": len(successful),
        "capabilities": dedup_capabilities,
        "actions": dedup_actions,
        "attempts": attempts,
    }


def discover_standard_mcp_module(
    module: ModuleConfig,
    *,
    openstack_token: str = "",
    openstack_user: str = "",
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    tools: list[str] = []
    session_id: str | None = None
    endpoint = _mcp_endpoint(module)
    try:
        with httpx.Client(timeout=min(module.timeout_seconds, 10.0)) as client:
            initialize_payload, session_id = _mcp_request(
                client,
                module,
                None,
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "woddi-harbor", "version": __version__},
                },
                request_id=1,
                openstack_token=openstack_token,
                openstack_user=openstack_user,
            )
            attempts.append({"label": "initialize", "ok": True, "body": initialize_payload})
            _mcp_request(
                client,
                module,
                session_id,
                "notifications/initialized",
                {},
                request_id=None,
                openstack_token=openstack_token,
                openstack_user=openstack_user,
            )
            attempts.append({"label": "notifications/initialized", "ok": True})
            tools_payload, session_id = _mcp_request(
                client,
                module,
                session_id,
                "tools/list",
                {},
                request_id=2,
                openstack_token=openstack_token,
                openstack_user=openstack_user,
            )
            attempts.append({"label": "tools/list", "ok": True, "body": tools_payload})
            for item in tools_payload.get("result", {}).get("tools", []):
                if isinstance(item, dict) and str(item.get("name", "")).strip():
                    tools.append(str(item["name"]))
                    discovery = item.get("annotations", {}).get("discovery", {})
                    if (
                        module.type == "netbox_mcp"
                        and isinstance(discovery, dict)
                        and discovery.get("source") == "unavailable"
                    ):
                        raise ValueError(
                            "NetBox upstream is unreachable: "
                            + str(discovery.get("error") or "Discovery unavailable")
                        )
    except Exception as exc:
        attempts.append({"label": "mcp", "ok": False, "error": str(exc)})
        update_module_runtime_state(
            module.id,
            last_discovery_at=_timestamp(),
            last_discovery_ok=False,
            last_discovery_error=str(exc),
            last_error=str(exc),
        )
        return {
            "ok": False,
            "base_url": endpoint,
            "protocol": "mcp",
            "auth_configured": bool(
                module_secret(module)
                or (
                    _is_local_mcp_module(module)
                    and _local_mcp_auth_configured(module, openstack_token, openstack_user)
                )
            ),
            "session_id": session_id or "",
            "tools": [],
            "attempts": attempts,
        }
    deduped_tools = sorted({item for item in tools if item})
    update_module_runtime_state(
        module.id,
        last_discovery_at=_timestamp(),
        last_discovery_ok=True,
        last_discovery_error="",
        last_discovered_tools=deduped_tools,
    )
    return {
        "ok": True,
        "base_url": endpoint,
        "protocol": "mcp",
        "auth_configured": bool(
            module_secret(module)
            or (
                _is_local_mcp_module(module)
                and _local_mcp_auth_configured(module, openstack_token, openstack_user)
            )
        ),
        "session_id": session_id or "",
        "tools": deduped_tools,
        "capabilities": ["tools"],
        "attempts": attempts,
    }


def start_module(module_id: str) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unknown module: {module_id}")
    if module.transport != "local":
        return {"ok": True, "message": "Remote module has no local process.", "status": module_status(module)}
    update_module_runtime_state(module.id, last_start_attempt_at=_timestamp(), last_start_error="")
    if read_module_pid(module_id) is not None:
        return {"ok": True, "message": "Module is already running.", "status": module_status(module)}
    if _module_health_reachable(module, timeout=0.5):
        _append_module_log(module.id, "Health endpoint already responds, but the PID file is missing.")
        return {
            "ok": True,
            "message": "Module already responds, but the PID file is missing.",
            "status": module_status(module),
        }
    module = _ensure_startable_port(module)
    validate_or_raise(module)
    try:
        process = _spawn_worker(module)
    except Exception as exc:
        _append_module_log(module.id, f"Worker start failed: {exc}")
        update_module_runtime_state(module.id, last_start_error=str(exc), last_error=str(exc))
        return {
            "ok": False,
            "message": f"Worker could not be started: {exc}",
            "status": module_status(module),
            "log_tail": _read_module_log_tail(module.id),
        }
    module_pid_path(module_id).write_text(f"{process.pid}\n", encoding="utf-8")
    started, detail = _wait_for_worker_start(process, module)
    if not started:
        _append_module_log(module.id, detail)
        _cleanup_failed_start(module.id, process)
        update_module_runtime_state(module.id, last_start_error=detail, last_error=detail)
        return {
            "ok": False,
            "message": detail,
            "status": module_status(module),
            "log_tail": _read_module_log_tail(module.id),
        }
    _append_module_log(module.id, f"Worker is reachable on {module.host}:{module.port} (PID {process.pid})")
    state = load_module_runtime_state(module.id)
    update_module_runtime_state(
        module.id,
        last_started_at=_timestamp(),
        last_health_ok_at=_timestamp(),
        last_start_error="",
        last_error="",
        restart_count=int(state.get("restart_count", 0)),
    )
    return {"ok": True, "message": "Module started.", "status": module_status(module)}


def stop_module(module_id: str) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unknown module: {module_id}")
    pid = read_module_pid(module_id)
    if pid is None:
        return {"ok": True, "message": "Module was not running.", "status": module_status(module)}
    os.killpg(pid, signal.SIGTERM)
    for _ in range(15):
        time.sleep(0.2)
        if read_module_pid(module_id) is None:
            break
    if read_module_pid(module_id) is not None:
        os.killpg(pid, signal.SIGKILL)
    module_pid_path(module_id).unlink(missing_ok=True)
    update_module_runtime_state(module.id, last_stopped_at=_timestamp())
    return {"ok": True, "message": "Module stopped.", "status": module_status(module)}


def restart_module(module_id: str) -> dict[str, Any]:
    state = load_module_runtime_state(module_id)
    update_module_runtime_state(module_id, restart_count=int(state.get("restart_count", 0)) + 1)
    stop_module(module_id)
    return start_module(module_id)


def execute_module(
    module_id: str,
    action: str,
    payload: dict[str, Any],
    *,
    openstack_token: str = "",
    openstack_user: str = "",
) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unknown module: {module_id}")

    if module.transport == "local" and module.type in {"docs", "maildir"}:
        return worker_execute(module, action, payload)

    if module.type in {"mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"}:
        try:
            if _is_local_mcp_module(module) or module.remote_protocol == "mcp" or module.base_url.rstrip("/").endswith("/mcp"):
                if action == "health":
                    return {
                        "ok": True,
                        "data": health_check_module(
                            module_id,
                            openstack_token=openstack_token,
                            openstack_user=openstack_user,
                        ),
                    }
                if action in {"discover", "capabilities", "tools/list", "list_tools"}:
                    return {
                        "ok": True,
                        "data": discover_standard_mcp_module(
                            module,
                            openstack_token=openstack_token,
                            openstack_user=openstack_user,
                        ),
                    }
                tool_name = str(payload.get("tool") or action).strip()
                arguments = payload.get("arguments")
                if not isinstance(arguments, dict):
                    arguments = {key: value for key, value in payload.items() if key != "tool"}
                effective_openstack_token = openstack_token.strip()
                if module.type == "openstack_mcp" and not effective_openstack_token:
                    effective_openstack_token = os.getenv("OS_TOKEN", "").strip()
                if module.type == "openstack_mcp" and not effective_openstack_token and not openstack_user.strip():
                    raise ValueError(
                        "OpenStack credentials are missing for this user. "
                        "Save a project-scoped user token in the OpenStack dialog."
                    )
                result = _call_mcp_tool(
                    module,
                    tool_name,
                    arguments,
                    openstack_token=effective_openstack_token,
                    openstack_user=openstack_user,
                )
                _update_field_catalog_from_result(module, tool_name, result)
                update_module_runtime_state(module.id, last_execute_error="", last_error="")
                return {"ok": True, "data": result, "tool": tool_name}
            headers = _auth_headers(module)
            with httpx.Client(timeout=module.timeout_seconds) as client:
                response = client.post(
                    module.base_url.rstrip("/") + "/execute",
                    headers=headers,
                    json={"action": action, "payload": payload},
                )
                response.raise_for_status()
                update_module_runtime_state(module.id, last_execute_error="", last_error="")
                return response.json()
        except Exception as exc:
            update_module_runtime_state(module.id, last_execute_error=str(exc), last_error=str(exc))
            raise

    if module.transport != "local":
        raise ValueError(f"Unsupported transport model for {module.id}")
    with httpx.Client(timeout=module.timeout_seconds) as client:
        response = client.post(
            f"{module_url(module)}/execute",
            headers=_local_worker_headers(),
            json={"action": action, "payload": payload},
        )
        response.raise_for_status()
        return response.json()


def worker_health(module: ModuleConfig) -> dict[str, Any]:
    return {
        "module_id": module.id,
        "name": module.display_name(),
        "type": module.type,
        "path": module.path,
        "source_count": len(module_sources(module)),
        "transport": module.transport,
        "port": module.port,
        "ready": True,
        "index_path": str(module_index_path(module.id)) if module.type in {"docs", "maildir"} else "",
    }


def _query_cache_key(module: ModuleConfig, action: str, payload: dict[str, Any], index_built_at: str) -> str:
    return json.dumps(
        {
            "module_id": module.id,
            "action": action,
            "payload": payload,
            "index_built_at": index_built_at,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _query_cache_disk_path(module: ModuleConfig, action: str, payload: dict[str, Any], index_built_at: str) -> Path:
    key = _query_cache_key(module, action, payload, index_built_at)
    digest = sha1(key.encode("utf-8")).hexdigest()
    return module_query_cache_dir(module.id) / f"{digest}.json"


def _query_cache_get(module: ModuleConfig, action: str, payload: dict[str, Any], index_built_at: str) -> dict[str, Any] | None:
    key = _query_cache_key(module, action, payload, index_built_at)
    cached = _QUERY_CACHE.get(key)
    if cached is None:
        disk_path = _query_cache_disk_path(module, action, payload, index_built_at)
        if disk_path.exists():
            try:
                raw = json.loads(disk_path.read_text(encoding="utf-8"))
            except Exception:
                raw = None
            if isinstance(raw, dict):
                cached_at = float(raw.get("cached_at", 0.0))
                if time.time() - cached_at <= PERSISTENT_QUERY_CACHE_TTL_SECONDS:
                    result = raw.get("result")
                    if isinstance(result, dict):
                        _QUERY_CACHE.set(key, result)
                        state = load_module_runtime_state(module.id)
                        update_module_runtime_state(
                            module.id,
                            query_cache_hits=int(state.get("query_cache_hits", 0)) + 1,
                            query_cache_disk_hits=int(state.get("query_cache_disk_hits", 0)) + 1,
                        )
                        return json.loads(json.dumps(result, ensure_ascii=False))
                else:
                    disk_path.unlink(missing_ok=True)
        state = load_module_runtime_state(module.id)
        update_module_runtime_state(module.id, query_cache_misses=int(state.get("query_cache_misses", 0)) + 1)
        return None
    state = load_module_runtime_state(module.id)
    update_module_runtime_state(module.id, query_cache_hits=int(state.get("query_cache_hits", 0)) + 1)
    return json.loads(json.dumps(cached, ensure_ascii=False))


def _query_cache_set(module: ModuleConfig, action: str, payload: dict[str, Any], index_built_at: str, result: dict[str, Any]) -> None:
    key = _query_cache_key(module, action, payload, index_built_at)
    serialized = json.loads(json.dumps(result, ensure_ascii=False))
    _QUERY_CACHE.set(key, serialized)
    disk_path = _query_cache_disk_path(module, action, payload, index_built_at)
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    disk_path.write_text(json.dumps({"cached_at": time.time(), "result": serialized}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state = load_module_runtime_state(module.id)
    update_module_runtime_state(module.id, query_cache_writes=int(state.get("query_cache_writes", 0)) + 1)


def _clear_module_query_cache(module_id: str) -> None:
    prefix = f'"module_id":"{module_id}"'
    _QUERY_CACHE.delete_matching(lambda key: prefix in key)
    shutil.rmtree(module_query_cache_dir(module_id), ignore_errors=True)


def _module_query_cache_count(module_id: str) -> int:
    prefix = f'"module_id":"{module_id}"'
    memory_entries = _QUERY_CACHE.count_matching(lambda key: prefix in key)
    disk_entries = 0
    cache_dir = module_query_cache_dir(module_id)
    if cache_dir.exists():
        try:
            disk_entries = sum(1 for path in cache_dir.iterdir() if path.is_file())
        except OSError:
            disk_entries = 0
    return max(memory_entries, disk_entries)


def warm_module_runtime_caches() -> dict[str, Any]:
    warmed = 0
    checked = 0
    for module in load_modules():
        if not module.enabled:
            continue
        checked += 1
        try:
            if module.type in {"docs", "maildir"}:
                _index_meta_payload(module.id)
            _module_health(module, timeout=1.5)
            warmed += 1
        except Exception:
            continue
    return {"checked": checked, "warmed": warmed, "timestamp": _timestamp()}


def _run_reindex_job(module: ModuleConfig, kind: IndexKind, roots: list[tuple[str, str, Path]], index_path: Path, index_timeout: float | None, job_id: str) -> None:
    started_at = time.monotonic()
    update_module_runtime_state(
        module.id,
        last_index_started_at=_timestamp(),
        last_index_error="",
        index_job_active=True,
        index_job_id=job_id,
        index_job_status="running",
    )
    try:
        index, _rebuilt = ensure_index(kind, roots, index_path, force_rebuild=True, timeout_seconds=index_timeout)
        _clear_module_query_cache(module.id)
        update_module_runtime_state(
            module.id,
            last_index_completed_at=_timestamp(),
            last_index_duration_seconds=round(time.monotonic() - started_at, 3),
            last_index_document_count=index.document_count,
            last_index_inventory_count=index.inventory_count,
            last_index_error="",
            index_job_active=False,
            index_job_status="completed",
            index_job_id=job_id,
        )
    except Exception as exc:
        update_module_runtime_state(
            module.id,
            last_index_error=str(exc),
            last_error=str(exc),
            index_job_active=False,
            index_job_status="failed",
            index_job_id=job_id,
        )
    finally:
        with _REINDEX_LOCK:
            _REINDEX_THREADS.pop(module.id, None)


def worker_execute(module: ModuleConfig, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    roots = [
        (source.id, source.display_name(), resolve_module_source_path(source))
        for source in module_sources(module)
    ]
    top_k = int(payload.get("top_k", module.top_k))
    index_timeout = module.timeout_seconds if module.timeout_seconds > 0 else None
    if module.type == "docs":
        index_path = module_index_path(module.id)
        if action == "health":
            return {"ok": True, "data": worker_health(module)}
        if action == "stats":
            index, rebuilt = ensure_index("docs", roots, index_path, timeout_seconds=index_timeout)
            return {
                "ok": True,
                "data": {
                    "rebuilt": rebuilt,
                    "built_at": index.built_at,
                    "document_count": index.document_count,
                    "inventory_count": index.inventory_count,
                    "index_path": str(index_path),
                    "roots": index.roots,
                },
            }
        if action == "reindex":
            started_at = time.monotonic()
            update_module_runtime_state(module.id, last_index_started_at=_timestamp(), last_index_error="", index_job_active=False, index_job_status="")
            index, _rebuilt = ensure_index("docs", roots, index_path, force_rebuild=True, timeout_seconds=index_timeout)
            _clear_module_query_cache(module.id)
            update_module_runtime_state(
                module.id,
                last_index_completed_at=_timestamp(),
                last_index_duration_seconds=round(time.monotonic() - started_at, 3),
                last_index_document_count=index.document_count,
                last_index_inventory_count=index.inventory_count,
                last_index_error="",
            )
            return {
                "ok": True,
                "data": {
                    "rebuilt": True,
                    "built_at": index.built_at,
                    "document_count": index.document_count,
                    "inventory_count": index.inventory_count,
                    "index_path": str(index_path),
                    "roots": index.roots,
                },
            }
        if action == "reindex_async":
            with _REINDEX_LOCK:
                existing = _REINDEX_THREADS.get(module.id)
                if existing is not None and existing.is_alive():
                    state = load_module_runtime_state(module.id)
                    return {"ok": True, "data": {"job_running": True, "job_id": state.get("index_job_id", ""), "status": state.get("index_job_status", "running")}}
                job_id = str(uuid4())
                thread = threading.Thread(
                    target=_run_reindex_job,
                    args=(module, "docs", roots, index_path, index_timeout, job_id),
                    daemon=True,
                    name=f"reindex-{module.id}",
                )
                _REINDEX_THREADS[module.id] = thread
                thread.start()
            return {"ok": True, "data": {"job_running": True, "job_id": job_id, "status": "started"}}
        if action == "reindex_status":
            state = load_module_runtime_state(module.id)
            return {
                "ok": True,
                "data": {
                    "job_running": bool(state.get("index_job_active")),
                    "job_id": state.get("index_job_id", ""),
                    "status": state.get("index_job_status", ""),
                    "last_index_started_at": state.get("last_index_started_at", ""),
                    "last_index_completed_at": state.get("last_index_completed_at", ""),
                    "last_index_error": state.get("last_index_error", ""),
                },
            }
        if action == "search":
            query = str(payload.get("query", "")).strip()
            started_at = time.monotonic()
            index, rebuilt = ensure_index("docs", roots, index_path, timeout_seconds=index_timeout)
            cached = _query_cache_get(module, action, {"query": query, "top_k": top_k}, index.built_at)
            if cached is not None:
                cached["data"]["cache_hit"] = True
                cached["data"]["rebuilt"] = rebuilt
                update_module_runtime_state(module.id, last_query_duration_ms=round((time.monotonic() - started_at) * 1000.0, 2))
                return cached
            hits = search_index(index, query, top_k)
            result = {
                "ok": True,
                "data": {
                    "query": query,
                    "hits": [asdict(hit) for hit in hits],
                    "documents": index.document_count,
                    "rebuilt": rebuilt,
                    "cache_hit": False,
                    "index_built_at": index.built_at,
                    "roots": index.roots,
                },
            }
            _query_cache_set(module, action, {"query": query, "top_k": top_k}, index.built_at, result)
            update_module_runtime_state(module.id, last_query_duration_ms=round((time.monotonic() - started_at) * 1000.0, 2))
            return result
        raise ValueError(f"Unknown docs action: {action}")
    if module.type == "maildir":
        index_path = module_index_path(module.id)
        if action == "health":
            return {"ok": True, "data": worker_health(module)}
        if action == "stats":
            index, rebuilt = ensure_index("maildir", roots, index_path, timeout_seconds=index_timeout)
            return {
                "ok": True,
                "data": {
                    "rebuilt": rebuilt,
                    "built_at": index.built_at,
                    "document_count": index.document_count,
                    "inventory_count": index.inventory_count,
                    "index_path": str(index_path),
                    "roots": index.roots,
                },
            }
        if action == "reindex":
            started_at = time.monotonic()
            update_module_runtime_state(module.id, last_index_started_at=_timestamp(), last_index_error="", index_job_active=False, index_job_status="")
            index, _rebuilt = ensure_index("maildir", roots, index_path, force_rebuild=True, timeout_seconds=index_timeout)
            _clear_module_query_cache(module.id)
            update_module_runtime_state(
                module.id,
                last_index_completed_at=_timestamp(),
                last_index_duration_seconds=round(time.monotonic() - started_at, 3),
                last_index_document_count=index.document_count,
                last_index_inventory_count=index.inventory_count,
                last_index_error="",
            )
            return {
                "ok": True,
                "data": {
                    "rebuilt": True,
                    "built_at": index.built_at,
                    "document_count": index.document_count,
                    "inventory_count": index.inventory_count,
                    "index_path": str(index_path),
                    "roots": index.roots,
                },
            }
        if action == "reindex_async":
            with _REINDEX_LOCK:
                existing = _REINDEX_THREADS.get(module.id)
                if existing is not None and existing.is_alive():
                    state = load_module_runtime_state(module.id)
                    return {"ok": True, "data": {"job_running": True, "job_id": state.get("index_job_id", ""), "status": state.get("index_job_status", "running")}}
                job_id = str(uuid4())
                thread = threading.Thread(
                    target=_run_reindex_job,
                    args=(module, "maildir", roots, index_path, index_timeout, job_id),
                    daemon=True,
                    name=f"reindex-{module.id}",
                )
                _REINDEX_THREADS[module.id] = thread
                thread.start()
            return {"ok": True, "data": {"job_running": True, "job_id": job_id, "status": "started"}}
        if action == "reindex_status":
            state = load_module_runtime_state(module.id)
            return {
                "ok": True,
                "data": {
                    "job_running": bool(state.get("index_job_active")),
                    "job_id": state.get("index_job_id", ""),
                    "status": state.get("index_job_status", ""),
                    "last_index_started_at": state.get("last_index_started_at", ""),
                    "last_index_completed_at": state.get("last_index_completed_at", ""),
                    "last_index_error": state.get("last_index_error", ""),
                },
            }
        if action == "search":
            query = str(payload.get("query", "")).strip()
            started_at = time.monotonic()
            index, rebuilt = ensure_index("maildir", roots, index_path, timeout_seconds=index_timeout)
            cached = _query_cache_get(module, action, {"query": query, "top_k": top_k}, index.built_at)
            if cached is not None:
                cached["data"]["cache_hit"] = True
                cached["data"]["rebuilt"] = rebuilt
                update_module_runtime_state(module.id, last_query_duration_ms=round((time.monotonic() - started_at) * 1000.0, 2))
                return cached
            hits = search_index(index, query, top_k)
            result = {
                "ok": True,
                "data": {
                    "query": query,
                    "hits": [asdict(hit) for hit in hits],
                    "messages": index.document_count,
                    "rebuilt": rebuilt,
                    "cache_hit": False,
                    "index_built_at": index.built_at,
                    "roots": index.roots,
                },
            }
            _query_cache_set(module, action, {"query": query, "top_k": top_k}, index.built_at, result)
            update_module_runtime_state(module.id, last_query_duration_ms=round((time.monotonic() - started_at) * 1000.0, 2))
            return result
        raise ValueError(f"Unknown maildir action: {action}")
    raise ValueError(f"Unsupported worker type: {module.type}")


def parse_json_payload(raw: str) -> dict[str, Any]:
    raw_text = raw.strip()
    if not raw_text:
        return {}
    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError("Payload must be a JSON object.")
    return parsed
