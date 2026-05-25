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

import httpx

from .config import BASE_DIR, LOG_DIR, PID_DIR, ModuleConfig, find_module, load_modules, module_secret
from .search import collect_mail_documents, collect_text_documents, score_documents


def module_url(module: ModuleConfig) -> str:
    return f"http://{module.host}:{module.port}"


def module_pid_path(module_id: str) -> Path:
    return PID_DIR / f"{module_id}.pid"


def module_log_path(module_id: str) -> Path:
    return LOG_DIR / f"{module_id}.log"


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def upsert_module(module: ModuleConfig) -> ModuleConfig:
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
        headers = {"Content-Type": "application/json"}
        secret = module_secret(module)
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
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
    return {
        "module_id": module.id,
        "name": module.display_name(),
        "type": module.type,
        "path": module.path,
        "transport": module.transport,
        "port": module.port,
    }


def worker_execute(module: ModuleConfig, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(module.path).expanduser()
    top_k = int(payload.get("top_k", module.top_k))
    if module.type == "docs":
        if action == "health":
            return {"ok": True, "data": worker_health(module)}
        if action == "search":
            query = str(payload.get("query", "")).strip()
            docs = collect_text_documents(root)
            hits = score_documents(docs, query, top_k)
            return {"ok": True, "data": {"query": query, "hits": [asdict(hit) for hit in hits], "documents": len(docs)}}
        raise ValueError(f"Aktion fuer docs nicht bekannt: {action}")
    if module.type == "maildir":
        if action == "health":
            return {"ok": True, "data": worker_health(module)}
        if action == "search":
            query = str(payload.get("query", "")).strip()
            docs = collect_mail_documents(root)
            hits = score_documents(docs, query, top_k)
            return {"ok": True, "data": {"query": query, "hits": [asdict(hit) for hit in hits], "messages": len(docs)}}
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
