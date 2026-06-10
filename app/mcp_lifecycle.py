from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from .config import DATA_DIR, LOG_DIR, PID_DIR
from .state import (
    change_mcp_instance_version,
    find_mcp_instance,
    find_mcp_package,
    list_mcp_instances,
    list_mcp_packages,
    previous_mcp_instance_version,
    record_audit,
    set_mcp_instance_state,
    upsert_mcp_instance,
    upsert_mcp_package,
)

MCP_PACKAGE_DIR = DATA_DIR / "mcp" / "packages"
ALLOWED_DRIVERS = {"http", "process", "systemd", "container"}


def _validate_identifier(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized or not all(character.isalnum() or character in "._-" for character in normalized):
        raise ValueError(f"{label} enthaelt ungueltige Zeichen.")
    return normalized


def validate_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    package_id = _validate_identifier(str(payload.get("id", "")), "Package-ID")
    version = _validate_identifier(str(payload.get("version", "")), "Version")
    driver = str(payload.get("driver", "")).strip()
    if driver not in ALLOWED_DRIVERS:
        raise ValueError(f"Driver muss einer von {sorted(ALLOWED_DRIVERS)} sein.")
    command = payload.get("command", [])
    if driver == "process" and (not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command)):
        raise ValueError("Process-Packages brauchen ein nicht-leeres command-Array.")
    if driver == "process" and Path(str(command[0])).is_absolute():
        raise ValueError("Process-Packages muessen ein relatives Executable im Paket verwenden.")
    if driver == "http" and not str(payload.get("endpoint", "")).strip():
        raise ValueError("HTTP-Packages brauchen einen endpoint.")
    if driver == "systemd" and not str(payload.get("unit_name", "")).strip():
        raise ValueError("systemd-Packages brauchen unit_name.")
    if driver == "container" and not str(payload.get("image", "")).strip():
        raise ValueError("Container-Packages brauchen image.")
    tools = payload.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("tools muss eine Liste sein.")
    return {
        **payload,
        "id": package_id,
        "version": version,
        "driver": driver,
        "tools": [str(item) for item in tools if str(item).strip()],
    }


def install_package(source: str, *, actor: str = "system") -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve()
    manifest_path = source_path / "mcp-package.json" if source_path.is_dir() else source_path
    if not manifest_path.is_file():
        raise ValueError("mcp-package.json wurde nicht gefunden.")
    manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    target = MCP_PACKAGE_DIR / manifest["id"] / manifest["version"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if source_path.is_dir():
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_path, target)
    else:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(manifest_path, target / "mcp-package.json")
    manifest["install_path"] = str(target)
    upsert_mcp_package(manifest["id"], manifest["version"], manifest)
    record_audit("mcp.package.install", f"{manifest['id']}@{manifest['version']}", actor=actor)
    return manifest


def create_instance(
    instance_id: str,
    package_id: str,
    version: str,
    config: dict[str, Any] | None = None,
    *,
    actor: str = "system",
) -> dict[str, Any]:
    normalized_id = _validate_identifier(instance_id, "Instanz-ID")
    manifest = find_mcp_package(package_id, version)
    if manifest is None:
        raise ValueError(f"MCP-Paket nicht installiert: {package_id}@{version}")
    upsert_mcp_instance(normalized_id, package_id, version, manifest["driver"], config or {})
    record_audit("mcp.instance.create", normalized_id, actor=actor)
    return instance_status(normalized_id)


def _pid_path(instance_id: str) -> Path:
    return PID_DIR / f"mcp-{instance_id}.pid"


def _log_path(instance_id: str) -> Path:
    return LOG_DIR / f"mcp-{instance_id}.log"


def _process_alive(instance_id: str) -> bool:
    path = _pid_path(instance_id)
    if not path.exists():
        return False
    try:
        pid = int(path.read_text(encoding="utf-8"))
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return False


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = key.lower()
            if any(marker in normalized for marker in ("password", "secret", "token", "api_key", "credential")):
                result[key] = "***" if item else ""
            else:
                result[key] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _systemd_command(instance: dict[str, Any], manifest: dict[str, Any], action: str) -> subprocess.CompletedProcess[str]:
    scope = str(instance["config"].get("scope") or manifest.get("scope") or "system")
    command = ["systemctl"]
    if scope == "user":
        command.append("--user")
    elif scope != "system":
        raise ValueError("systemd scope muss user oder system sein.")
    command.extend([action, str(manifest["unit_name"])])
    return subprocess.run(command, check=False, text=True, capture_output=True)


def _container_name(instance_id: str) -> str:
    return f"harbor-mcp-{instance_id}"


def _container_running(instance_id: str) -> bool:
    if shutil.which("podman") is None:
        return False
    completed = subprocess.run(
        ["podman", "inspect", "-f", "{{.State.Running}}", _container_name(instance_id)],
        check=False,
        text=True,
        capture_output=True,
    )
    return completed.returncode == 0 and completed.stdout.strip().lower() == "true"


def instance_status(instance_id: str) -> dict[str, Any]:
    instance = find_mcp_instance(instance_id)
    if instance is None:
        raise ValueError(f"MCP-Instanz nicht gefunden: {instance_id}")
    manifest = find_mcp_package(instance["package_id"], instance["package_version"]) or {}
    driver = instance["driver"]
    health: dict[str, Any] = {}
    running = _process_alive(instance_id) if driver == "process" else False
    if driver == "systemd":
        completed = _systemd_command(instance, manifest, "is-active")
        running = completed.returncode == 0
        health = {"stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}
    elif driver == "container":
        running = _container_running(instance_id)
    endpoint = str(instance["config"].get("endpoint") or manifest.get("endpoint") or "").strip()
    if driver == "http" and endpoint:
        try:
            with httpx.Client(timeout=3.0) as client:
                response = client.get(endpoint.rstrip("/") + "/health")
            running = response.is_success
            health = {"status_code": response.status_code}
        except Exception as exc:
            health = {"error": str(exc)}
    return {
        **instance,
        "config": _redact(instance["config"]),
        "manifest": manifest,
        "running": running,
        "health": health,
        "log_path": str(_log_path(instance_id)),
    }


def start_instance(instance_id: str, *, actor: str = "system") -> dict[str, Any]:
    instance = find_mcp_instance(instance_id)
    if instance is None:
        raise ValueError(f"MCP-Instanz nicht gefunden: {instance_id}")
    manifest = find_mcp_package(instance["package_id"], instance["package_version"]) or {}
    driver = instance["driver"]
    if driver == "http":
        set_mcp_instance_state(instance_id, "running")
    elif driver == "process":
        if not _process_alive(instance_id):
            command = [str(item) for item in manifest.get("command", [])]
            install_path = Path(str(manifest["install_path"])).resolve()
            executable = Path(command[0])
            if not executable.is_absolute():
                candidate = (install_path / executable).resolve()
                if install_path not in candidate.parents and candidate != install_path:
                    raise ValueError("MCP-Command verlaesst das Installationsverzeichnis.")
                command[0] = str(candidate)
            env = os.environ.copy()
            env.update({str(key): str(value) for key, value in instance["config"].get("env", {}).items()})
            with _log_path(instance_id).open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command,
                    cwd=install_path,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            _pid_path(instance_id).write_text(str(process.pid), encoding="utf-8")
        set_mcp_instance_state(instance_id, "running")
    elif driver == "systemd":
        completed = _systemd_command(instance, manifest, "start")
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "systemd start fehlgeschlagen.")
        set_mcp_instance_state(instance_id, "running")
    elif driver == "container":
        if shutil.which("podman") is None:
            raise RuntimeError("podman ist nicht installiert.")
        if not _container_running(instance_id):
            subprocess.run(["podman", "rm", "-f", _container_name(instance_id)], check=False, capture_output=True)
            command = ["podman", "run", "--detach", "--name", _container_name(instance_id)]
            for key, value in instance["config"].get("env", {}).items():
                command.extend(["--env", f"{key}={value}"])
            for port in instance["config"].get("ports", []):
                command.extend(["--publish", str(port)])
            command.append(str(manifest["image"]))
            command.extend(str(item) for item in manifest.get("command", []))
            completed = subprocess.run(command, check=False, text=True, capture_output=True)
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip() or "Container-Start fehlgeschlagen.")
        set_mcp_instance_state(instance_id, "running")
    else:
        raise ValueError(f"Unbekannter Driver: {driver}")
    record_audit("mcp.instance.start", instance_id, actor=actor)
    return instance_status(instance_id)


def stop_instance(instance_id: str, *, actor: str = "system") -> dict[str, Any]:
    instance = find_mcp_instance(instance_id)
    if instance is None:
        raise ValueError(f"MCP-Instanz nicht gefunden: {instance_id}")
    if instance["driver"] == "process" and _process_alive(instance_id):
        pid = int(_pid_path(instance_id).read_text(encoding="utf-8"))
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _process_alive(instance_id):
            time.sleep(0.1)
        if _process_alive(instance_id):
            os.killpg(pid, signal.SIGKILL)
        _pid_path(instance_id).unlink(missing_ok=True)
    elif instance["driver"] == "systemd":
        manifest = find_mcp_package(instance["package_id"], instance["package_version"]) or {}
        completed = _systemd_command(instance, manifest, "stop")
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "systemd stop fehlgeschlagen.")
    elif instance["driver"] == "container" and shutil.which("podman"):
        completed = subprocess.run(
            ["podman", "stop", "--time", "10", _container_name(instance_id)],
            check=False,
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0 and "no such container" not in completed.stderr.lower():
            raise RuntimeError(completed.stderr.strip() or "Container-Stop fehlgeschlagen.")
    set_mcp_instance_state(instance_id, "stopped")
    record_audit("mcp.instance.stop", instance_id, actor=actor)
    return instance_status(instance_id)


def restart_instance(instance_id: str, *, actor: str = "system") -> dict[str, Any]:
    stop_instance(instance_id, actor=actor)
    return start_instance(instance_id, actor=actor)


def upgrade_instance(instance_id: str, version: str, *, actor: str = "system") -> dict[str, Any]:
    instance = find_mcp_instance(instance_id)
    if instance is None:
        raise ValueError(f"MCP-Instanz nicht gefunden: {instance_id}")
    was_running = instance_status(instance_id)["running"]
    if was_running:
        stop_instance(instance_id, actor=actor)
    change_mcp_instance_version(instance_id, version)
    record_audit("mcp.instance.upgrade", instance_id, actor=actor, detail={"version": version})
    return start_instance(instance_id, actor=actor) if was_running else instance_status(instance_id)


def rollback_instance(instance_id: str, *, actor: str = "system") -> dict[str, Any]:
    version = previous_mcp_instance_version(instance_id)
    if not version:
        raise ValueError("Keine vorherige MCP-Version fuer Rollback vorhanden.")
    return upgrade_instance(instance_id, version, actor=actor)


def lifecycle_overview() -> dict[str, Any]:
    return {
        "packages": list_mcp_packages(),
        "instances": [instance_status(item["id"]) for item in list_mcp_instances()],
    }
