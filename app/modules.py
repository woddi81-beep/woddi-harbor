from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import BASE_DIR, LOG_DIR, PID_DIR, RUNTIME_DIR, ModuleConfig, find_module, load_modules, module_secret
from .search import ensure_index, load_index, search_index


def module_url(module: ModuleConfig) -> str:
    return f"http://{module.host}:{module.port}"


def module_pid_path(module_id: str) -> Path:
    return PID_DIR / f"{module_id}.pid"


def module_log_path(module_id: str) -> Path:
    return LOG_DIR / f"{module_id}.log"


def module_index_path(module_id: str) -> Path:
    return RUNTIME_DIR / "indexes" / f"{module_id}.json"


def _auth_headers(module: ModuleConfig, *, force_auth: bool = False) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    secret = module_secret(module)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    elif force_auth:
        headers["Authorization"] = "Bearer "
    return headers


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def validate_module_config(module: ModuleConfig) -> list[str]:
    errors: list[str] = []
    if not module.id.strip():
        errors.append("Module ID fehlt.")
    if module.type not in {"docs", "maildir", "mcp_http"}:
        errors.append(f"Unbekannter Modultyp: {module.type}")
    if module.transport not in {"local", "remote"}:
        errors.append(f"Ungueltiger Transport: {module.transport}")
    if module.type in {"docs", "maildir"}:
        if module.transport != "local":
            errors.append(f"{module.type} muss lokal sein.")
        root = Path(module.path).expanduser()
        if not module.path.strip():
            errors.append("Lokaler Pfad fehlt.")
        elif not root.exists():
            errors.append(f"Pfad existiert nicht: {root}")
        elif not root.is_dir():
            errors.append(f"Pfad ist kein Verzeichnis: {root}")
        if module.port <= 0 or module.port > 65535:
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
    return errors


def validate_or_raise(module: ModuleConfig) -> None:
    errors = validate_module_config(module)
    if errors:
        raise ValueError(" ".join(errors))


def upsert_module(module: ModuleConfig) -> ModuleConfig:
    validate_or_raise(module)
    modules = load_modules()
    replaced = False
    for index, existing in enumerate(modules):
        if existing.id == module.id:
            modules[index] = module
            replaced = True
            break
    if not replaced:
        modules.append(module)
    from .config import save_modules

    save_modules(modules)
    return module


def remove_module(module_id: str) -> bool:
    modules = [module for module in load_modules() if module.id != module_id]
    if len(modules) == len(load_modules()):
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


def module_status(module: ModuleConfig) -> dict[str, Any]:
    pid = read_module_pid(module.id)
    running = pid is not None
    health: dict[str, Any] | None = None
    if running:
        try:
            with httpx.Client(timeout=2.5) as client:
                response = client.get(f"{module_url(module)}/health")
                response.raise_for_status()
                health = response.json()
        except Exception:
            health = None
    return {
        "id": module.id,
        "name": module.display_name(),
        "type": module.type,
        "enabled": module.enabled,
        "transport": module.transport,
        "running": running,
        "pid": pid,
        "path": module.path,
        "base_url": module.base_url,
        "host": module.host,
        "port": module.port,
        "health": health,
        "index_path": str(module_index_path(module.id)) if module.type in {"docs", "maildir"} else "",
    }


def health_check_module(module_id: str) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unbekanntes Modul: {module_id}")
    errors = validate_module_config(module)
    status = module_status(module)
    payload: dict[str, Any] = {
        "ok": not errors,
        "module_id": module_id,
        "validation_errors": errors,
        "status": status,
    }
    if module.type in {"docs", "maildir"}:
        index = load_index(module_index_path(module.id))
        payload["index"] = {
            "exists": index is not None,
            "built_at": index.built_at if index else "",
            "document_count": index.document_count if index else 0,
        }
    if module.type == "mcp_http" and not errors:
        discovery = discover_remote_module(module)
        payload["remote"] = discovery
        payload["ok"] = payload["ok"] and bool(discovery.get("ok"))
    return payload


def discover_remote_module(module: ModuleConfig) -> dict[str, Any]:
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
    return {
        "ok": bool(successful),
        "base_url": base_url,
        "auth_configured": secret_present,
        "successful_attempts": len(successful),
        "capabilities": dedup_capabilities,
        "actions": dedup_actions,
        "attempts": attempts,
    }


def start_module(module_id: str) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unbekanntes Modul: {module_id}")
    if module.transport != "local":
        return {"ok": True, "message": "Remote-Modul hat keinen lokalen Prozess.", "status": module_status(module)}
    if read_module_pid(module_id) is not None:
        return {"ok": True, "message": "Modul laeuft bereits.", "status": module_status(module)}
    if not module.port:
        module.port = reserve_port()
        upsert_module(module)

    log_path = module_log_path(module_id)
    with log_path.open("a", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.cli",
                "worker",
                module.id,
            ],
            cwd=str(BASE_DIR),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    module_pid_path(module_id).write_text(f"{process.pid}\n", encoding="utf-8")
    for _ in range(20):
        time.sleep(0.2)
        try:
            with httpx.Client(timeout=1.0) as client:
                response = client.get(f"{module_url(module)}/health")
            if response.status_code == 200:
                break
        except Exception:
            continue
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
    return {"ok": True, "message": "Modul gestoppt.", "status": module_status(module)}


def restart_module(module_id: str) -> dict[str, Any]:
    stop_module(module_id)
    return start_module(module_id)


def execute_module(module_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Unbekanntes Modul: {module_id}")

    if module.type == "mcp_http":
        headers = _auth_headers(module)
        with httpx.Client(timeout=module.timeout_seconds) as client:
            response = client.post(
                module.base_url.rstrip("/") + "/execute",
                headers=headers,
                json={"action": action, "payload": payload},
            )
            response.raise_for_status()
            return response.json()

    if module.transport != "local":
        raise ValueError(f"Nicht unterstuetztes Transportmodell fuer {module.id}")
    with httpx.Client(timeout=module.timeout_seconds) as client:
        response = client.post(
            f"{module_url(module)}/execute",
            json={"action": action, "payload": payload},
        )
        response.raise_for_status()
        return response.json()


def worker_health(module: ModuleConfig) -> dict[str, Any]:
    index = load_index(module_index_path(module.id)) if module.type in {"docs", "maildir"} else None
    return {
        "module_id": module.id,
        "name": module.display_name(),
        "type": module.type,
        "path": module.path,
        "transport": module.transport,
        "port": module.port,
        "index_path": str(module_index_path(module.id)) if module.type in {"docs", "maildir"} else "",
        "index_built_at": index.built_at if index else "",
        "index_document_count": index.document_count if index else 0,
    }


def worker_execute(module: ModuleConfig, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(module.path).expanduser()
    top_k = int(payload.get("top_k", module.top_k))
    if module.type == "docs":
        index_path = module_index_path(module.id)
        if action == "health":
            return {"ok": True, "data": worker_health(module)}
        if action == "stats":
            index, rebuilt = ensure_index("docs", root, index_path)
            return {
                "ok": True,
                "data": {
                    "rebuilt": rebuilt,
                    "built_at": index.built_at,
                    "document_count": index.document_count,
                    "inventory_count": index.inventory_count,
                    "index_path": str(index_path),
                },
            }
        if action == "reindex":
            index, _rebuilt = ensure_index("docs", root, index_path, force_rebuild=True)
            return {
                "ok": True,
                "data": {
                    "rebuilt": True,
                    "built_at": index.built_at,
                    "document_count": index.document_count,
                    "inventory_count": index.inventory_count,
                    "index_path": str(index_path),
                },
            }
        if action == "search":
            query = str(payload.get("query", "")).strip()
            index, rebuilt = ensure_index("docs", root, index_path)
            hits = search_index(index, query, top_k)
            return {
                "ok": True,
                "data": {
                    "query": query,
                    "hits": [asdict(hit) for hit in hits],
                    "documents": index.document_count,
                    "rebuilt": rebuilt,
                    "index_built_at": index.built_at,
                },
            }
        raise ValueError(f"Aktion fuer docs nicht bekannt: {action}")
    if module.type == "maildir":
        index_path = module_index_path(module.id)
        if action == "health":
            return {"ok": True, "data": worker_health(module)}
        if action == "stats":
            index, rebuilt = ensure_index("maildir", root, index_path)
            return {
                "ok": True,
                "data": {
                    "rebuilt": rebuilt,
                    "built_at": index.built_at,
                    "document_count": index.document_count,
                    "inventory_count": index.inventory_count,
                    "index_path": str(index_path),
                },
            }
        if action == "reindex":
            index, _rebuilt = ensure_index("maildir", root, index_path, force_rebuild=True)
            return {
                "ok": True,
                "data": {
                    "rebuilt": True,
                    "built_at": index.built_at,
                    "document_count": index.document_count,
                    "inventory_count": index.inventory_count,
                    "index_path": str(index_path),
                },
            }
        if action == "search":
            query = str(payload.get("query", "")).strip()
            index, rebuilt = ensure_index("maildir", root, index_path)
            hits = search_index(index, query, top_k)
            return {
                "ok": True,
                "data": {
                    "query": query,
                    "hits": [asdict(hit) for hit in hits],
                    "messages": index.document_count,
                    "rebuilt": rebuilt,
                    "index_built_at": index.built_at,
                },
            }
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
