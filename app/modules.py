from __future__ import annotations

import fcntl
import json
import os
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
from typing import Any
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


def _local_mcp_server_name(module: ModuleConfig) -> str:
    if module.type == "netbox_mcp":
        return "netbox-mcp-server"
    if module.type == "openstack_mcp":
        return "openstack-mcp-server"
    if module.type == "sap_docs_mcp":
        return "sap-docs-mcp-server"
    return ""


def _local_mcp_auth_configured(module: ModuleConfig, openstack_token: str = "") -> bool:
    if module.type == "netbox_mcp":
        return False
    if module.type == "openstack_mcp":
        settings = _openstack_settings(module, openstack_token)
        return bool(settings.get("OS_AUTH_URL") and settings.get("OS_TOKEN"))
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
    if openstack_token.strip():
        headers[OPENSTACK_TOKEN_HEADER] = openstack_token.strip()
        headers[OPENSTACK_USER_HEADER] = openstack_user.strip() or "cli"
    return headers


def _redact_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = key.lower()
            if any(marker in normalized for marker in ("password", "secret", "token", "api_key", "credential")):
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
    raise RuntimeError("Kein Python-Interpreter gefunden. Erwarte sys.executable oder python3 im PATH.")


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
        _append_module_log(module.id, f"Neuen Port reserviert: {module.port}")
        return module
    if _module_health_reachable(module, timeout=0.5):
        return module
    if _port_bindable(module.host, module.port):
        return module
    previous_port = module.port
    module.port = reserve_port()
    upsert_module(module)
    _append_module_log(module.id, f"Port {previous_port} war belegt. Neuer Port: {module.port}")
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
    _append_module_log(module.id, f"Starte Worker fuer Modul {module.id} auf {module.host}:{module.port} mit {python_executable}")
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
            return False, f"Worker-Prozess wurde vorzeitig beendet (Exit-Code {returncode})."
        time.sleep(0.2)
    return False, f"Health-Check fuer {module.host}:{module.port} hat nicht innerhalb von {timeout_seconds:.1f}s geantwortet."


def validate_module_config(module: ModuleConfig) -> list[str]:
    errors: list[str] = []
    if not module.id.strip():
        errors.append("Module ID fehlt.")
    if module.type not in {"docs", "maildir", "mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"}:
        errors.append(f"Unbekannter Modultyp: {module.type}")
    if module.transport not in {"local", "remote"}:
        errors.append(f"Ungueltiger Transport: {module.transport}")
    if module.remote_protocol not in {"auto", "harbor_execute", "mcp"}:
        errors.append(f"Ungueltiges Remote-Protokoll: {module.remote_protocol}")
    if module.api_key.strip():
        errors.append("Inline API-Key ist nicht erlaubt; nutze api_key_env.")
    inline_secret_paths = _inline_secret_paths(module.settings)
    if inline_secret_paths:
        errors.append(
            "Inline-Secrets sind nicht erlaubt; nutze ENV-Referenzen: "
            + ", ".join(sorted(inline_secret_paths))
        )
    if module.type in {"docs", "maildir"}:
        if module.transport != "local":
            errors.append(f"{module.type} muss lokal sein.")
        sources = module_sources(module, enabled_only=False)
        if not sources:
            errors.append("Mindestens eine lokale Quelle fehlt.")
        seen_source_ids: set[str] = set()
        for source in sources:
            if not source.id.strip():
                errors.append("Quellen-ID fehlt.")
                continue
            if source.id in seen_source_ids:
                errors.append(f"Doppelte Quellen-ID: {source.id}")
            seen_source_ids.add(source.id)
            root = resolve_module_source_path(source)
            if not source.path.strip():
                errors.append(f"Pfad fehlt fuer Quelle {source.id}.")
            elif not root.exists():
                errors.append(f"Pfad existiert nicht: {root}")
            elif not root.is_dir():
                errors.append(f"Pfad ist kein Verzeichnis: {root}")
        if module.port < 0 or module.port > 65535:
            errors.append(f"Port ungueltig: {module.port}")
        if module.top_k <= 0:
            errors.append("top_k muss groesser als 0 sein.")
    if module.type == "mcp_http":
        if module.transport != "remote":
            errors.append("mcp_http muss remote sein.")
        if not module.base_url.strip():
            errors.append("Base URL fehlt.")
        else:
            parsed = urlparse(module.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"Base URL ungueltig: {module.base_url}")
    if module.type == "netbox_mcp":
        if module.transport != "local":
            errors.append("netbox_mcp muss lokal sein.")
        netbox_url = _netbox_url(module)
        if not netbox_url:
            errors.append("NetBox URL fehlt.")
        else:
            parsed = urlparse(netbox_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"NetBox URL ungueltig: {netbox_url}")
        if module.port < 0 or module.port > 65535:
            errors.append(f"Port ungueltig: {module.port}")
    if module.type == "openstack_mcp":
        if module.transport != "local":
            errors.append("openstack_mcp muss lokal sein.")
        openstack = _openstack_settings(module)
        if not openstack["OS_AUTH_URL"]:
            errors.append("OpenStack Auth URL fehlt.")
        else:
            parsed = urlparse(openstack["OS_AUTH_URL"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"OpenStack Auth URL ungueltig: {openstack['OS_AUTH_URL']}")
        if module.port < 0 or module.port > 65535:
            errors.append(f"Port ungueltig: {module.port}")
    if module.type == "sap_docs_mcp":
        if module.transport != "local":
            errors.append("sap_docs_mcp muss lokal sein.")
        docs_url = _sap_docs_url(module)
        if not docs_url:
            errors.append("SAP Docs URL fehlt.")
        else:
            parsed = urlparse(docs_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"SAP Docs URL ungueltig: {docs_url}")
        if module.port < 0 or module.port > 65535:
            errors.append(f"Port ungueltig: {module.port}")
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
        detail = f"Port-Konflikt: {host}:{port} wird mehrfach genutzt ({', '.join(sorted(module_ids))})."
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


def health_check_module(module_id: str) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unbekanntes Modul: {module_id}")
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
        payload["local"] = status.get("health")
        discovery = discover_standard_mcp_module(module)
        payload["remote"] = discovery
        payload["ok"] = payload["ok"] and bool(status.get("running")) and bool(discovery.get("ok"))
    return payload


def module_diagnostics(module_id: str, *, log_lines: int = 40) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unbekanntes Modul: {module_id}")
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
        payload["health"] = health_check_module(module_id)
        payload["ok"] = payload["ok"] and bool(payload["health"].get("ok"))
    except Exception as exc:
        payload["ok"] = False
        payload["health"] = {"ok": False, "error": str(exc)}
        payload["errors"].append(f"Health: {exc}")
    if module.type in {"mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"}:
        try:
            remote = discover_remote_module(module)
            payload["remote"] = remote
            payload["ok"] = payload["ok"] and bool(remote.get("ok"))
        except Exception as exc:
            payload["ok"] = False
            payload["remote"] = {"ok": False, "error": str(exc)}
            payload["errors"].append(f"Discovery: {exc}")
    if not payload["ok"] and module.transport == "local":
        payload["hint"] = f"Lokalen Worker pruefen oder starten: ./harbor.sh module start {module.id}"
    return payload


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
        raise ValueError(f"Unbekanntes Modul: {module_id}")
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
        message = "Modultest erfolgreich." if connected and meaningful_output else "Verbindung ok, aber Ausgabe ist nicht aussagekraeftig genug."
        if not connected:
            message = "Verbindungstest fehlgeschlagen."
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
        detail = payload["error"]
        raise ValueError(f"MCP-Fehler {method}: {detail}")
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
            last_discovery_error="Discovery ohne erfolgreiche Antwort.",
            last_error="Discovery ohne erfolgreiche Antwort.",
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
                            "NetBox Upstream nicht erreichbar: "
                            + str(discovery.get("error") or "Discovery nicht verfuegbar")
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
                    and _local_mcp_auth_configured(module, openstack_token)
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
                and _local_mcp_auth_configured(module, openstack_token)
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
        raise ValueError(f"Unbekanntes Modul: {module_id}")
    if module.transport != "local":
        return {"ok": True, "message": "Remote-Modul hat keinen lokalen Prozess.", "status": module_status(module)}
    update_module_runtime_state(module.id, last_start_attempt_at=_timestamp(), last_start_error="")
    if read_module_pid(module_id) is not None:
        return {"ok": True, "message": "Modul laeuft bereits.", "status": module_status(module)}
    if _module_health_reachable(module, timeout=0.5):
        _append_module_log(module.id, "Health-Endpoint antwortet bereits, aber die PID-Datei fehlt.")
        return {
            "ok": True,
            "message": "Modul antwortet bereits, aber die PID-Datei fehlt.",
            "status": module_status(module),
        }
    module = _ensure_startable_port(module)
    validate_or_raise(module)
    try:
        process = _spawn_worker(module)
    except Exception as exc:
        _append_module_log(module.id, f"Worker-Start fehlgeschlagen: {exc}")
        update_module_runtime_state(module.id, last_start_error=str(exc), last_error=str(exc))
        return {
            "ok": False,
            "message": f"Worker konnte nicht gestartet werden: {exc}",
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
    _append_module_log(module.id, f"Worker ist erreichbar auf {module.host}:{module.port} (PID {process.pid})")
    state = load_module_runtime_state(module.id)
    update_module_runtime_state(
        module.id,
        last_started_at=_timestamp(),
        last_health_ok_at=_timestamp(),
        last_start_error="",
        last_error="",
        restart_count=int(state.get("restart_count", 0)),
    )
    return {"ok": True, "message": "Modul gestartet.", "status": module_status(module)}


def stop_module(module_id: str) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unbekanntes Modul: {module_id}")
    pid = read_module_pid(module_id)
    if pid is None:
        return {"ok": True, "message": "Modul lief nicht.", "status": module_status(module)}
    os.killpg(pid, signal.SIGTERM)
    for _ in range(15):
        time.sleep(0.2)
        if read_module_pid(module_id) is None:
            break
    if read_module_pid(module_id) is not None:
        os.killpg(pid, signal.SIGKILL)
    module_pid_path(module_id).unlink(missing_ok=True)
    update_module_runtime_state(module.id, last_stopped_at=_timestamp())
    return {"ok": True, "message": "Modul gestoppt.", "status": module_status(module)}


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
        raise ValueError(f"Unbekanntes Modul: {module_id}")

    if module.transport == "local" and module.type in {"docs", "maildir"}:
        return worker_execute(module, action, payload)

    if module.type in {"mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"}:
        try:
            if _is_local_mcp_module(module) or module.remote_protocol == "mcp" or module.base_url.rstrip("/").endswith("/mcp"):
                if action == "health":
                    return {"ok": True, "data": health_check_module(module_id)}
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
                if module.type == "openstack_mcp" and not effective_openstack_token:
                    raise ValueError(
                        "OpenStack User-Token fehlt fuer diesen Benutzer. "
                        "Token in der Harbor-Weboberflaeche erneuern."
                    )
                result = _call_mcp_tool(
                    module,
                    tool_name,
                    arguments,
                    openstack_token=effective_openstack_token,
                    openstack_user=openstack_user,
                )
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
        raise ValueError(f"Nicht unterstuetztes Transportmodell fuer {module.id}")
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
        raise ValueError(f"Aktion fuer docs nicht bekannt: {action}")
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
        raise ValueError(f"Aktion fuer maildir nicht bekannt: {action}")
    raise ValueError(f"Worker-Typ nicht unterstuetzt: {module.type}")


def parse_json_payload(raw: str) -> dict[str, Any]:
    raw_text = raw.strip()
    if not raw_text:
        return {}
    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError("Payload muss ein JSON-Objekt sein.")
    return parsed
