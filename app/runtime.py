from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import (
    BASE_DIR,
    DATA_DIR,
    LOG_DIR,
    PID_DIR,
    load_modules,
    load_settings,
    save_service_profiles,
    sync_service_profiles,
)
from .mcp_lifecycle import (
    instance_status,
    list_mcp_instances,
    stop_instance,
)

MANAGED_USER_UNITS = (
    "woddi-harbor.service",
    "woddi-harbor-jobs.service",
    "woddi-harbor-backup.service",
    "woddi-harbor-backup.timer",
    "woddi-harbor-llm-tunnel.service",
    "woddi-harbor-tls.service",
)
PROMETHEUS_CONTAINER = "woddi-harbor-prometheus"
HARBOR_PID_PATH = PID_DIR / "harbor.pid"
LOCAL_WORKER_TYPES = {"netbox_mcp", "openstack_mcp", "sap_docs_mcp"}


def _run(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    return {
        "command": command,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_harbor_pid() -> int | None:
    try:
        pid = int(HARBOR_PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if _pid_alive(pid):
        return pid
    HARBOR_PID_PATH.unlink(missing_ok=True)
    return None


def _harbor_healthy(timeout_seconds: float = 1.0) -> bool:
    port = load_settings().port
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout_seconds) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _wait_for_harbor(expected_running: bool, timeout_seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _harbor_healthy() is expected_running:
            return True
        time.sleep(0.25)
    return False


def start_all() -> dict[str, Any]:
    from .modules import start_module

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    user_units = _installed_units()
    system_units = _profile_units("system")
    profiles = sync_service_profiles()
    harbor_managed_by_systemd = "woddi-harbor.service" in user_units or any(
        profile.kind == "harbor" and profile.systemd_mode == "system"
        for profile in profiles
    )
    systemd_module_ids = {
        profile.module_id
        for profile in profiles
        if profile.kind == "module" and profile.systemd_mode in {"user", "system"}
    }

    if user_units:
        results.append(
            {
                "component": "systemd-user",
                **_run(["systemctl", "--user", "start", *user_units]),
            }
        )
    if system_units:
        results.append(
            {
                "component": "systemd-system",
                **_run(["systemctl", "start", *system_units]),
            }
        )

    if harbor_managed_by_systemd:
        healthy = _wait_for_harbor(True)
        results.append(
            {
                "component": "harbor",
                "ok": healthy,
                "status": "systemd-running" if healthy else "systemd-start-failed",
                "unit": next(
                    (
                        profile.resolved_unit_name() + ".service"
                        for profile in profiles
                        if profile.kind == "harbor" and profile.systemd_mode in {"user", "system"}
                    ),
                    "woddi-harbor.service",
                ),
            }
        )
    elif _harbor_healthy():
        results.append({"component": "harbor", "ok": True, "status": "already-running", "pid": _read_harbor_pid()})
    else:
        log_path = LOG_DIR / "harbor.log"
        with log_path.open("a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                [sys.executable, "-m", "app.cli", "serve"],
                cwd=BASE_DIR,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        healthy = _wait_for_harbor(True)
        results.append(
            {
                "component": "harbor",
                "ok": healthy,
                "status": "started" if healthy else "start-failed",
                "pid": process.pid,
                "log": str(log_path),
            }
        )

    if results[-1]["ok"]:
        for module in load_modules():
            if not module.enabled or module.transport != "local" or module.type not in LOCAL_WORKER_TYPES:
                continue
            if module.id in systemd_module_ids:
                continue
            try:
                module_result = start_module(module.id)
                results.append(
                    {
                        "component": f"module:{module.id}",
                        "ok": bool(module_result.get("ok")),
                        "result": module_result,
                    }
                )
            except Exception as exc:
                results.append({"component": f"module:{module.id}", "ok": False, "error": str(exc)})

    return {"ok": all(item["ok"] for item in results), "results": results}


def _stop_local_modules() -> list[dict[str, Any]]:
    from .modules import stop_module

    results: list[dict[str, Any]] = []
    systemd_module_ids = {
        profile.module_id
        for profile in sync_service_profiles()
        if profile.kind == "module" and profile.systemd_mode in {"user", "system"}
    }
    for module in load_modules():
        if module.transport != "local":
            continue
        if module.id in systemd_module_ids:
            continue
        try:
            module_result = stop_module(module.id)
            results.append(
                {
                    "component": f"module:{module.id}",
                    "ok": bool(module_result.get("ok")),
                    "result": module_result,
                }
            )
        except Exception as exc:
            results.append({"component": f"module:{module.id}", "ok": False, "error": str(exc)})
    return results


def _stop_manual_harbor() -> dict[str, Any]:
    pid = _read_harbor_pid()
    if pid is None:
        return {"component": "harbor", "ok": True, "status": "not-running"}
    if pid == os.getpid():
        return {"component": "harbor", "ok": False, "status": "current-process", "pid": pid}

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        HARBOR_PID_PATH.unlink(missing_ok=True)
        return {"component": "harbor", "ok": True, "status": "not-running", "pid": pid}

    deadline = time.monotonic() + 10.0
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    HARBOR_PID_PATH.unlink(missing_ok=True)
    stopped = _wait_for_harbor(False, timeout_seconds=5.0)
    return {"component": "harbor", "ok": stopped, "status": "stopped" if stopped else "stop-failed", "pid": pid}


def _installed_module_units() -> list[str]:
    unit_dir = Path.home() / ".config/systemd/user"
    return sorted(path.name for path in unit_dir.glob("woddi-harbor-*.service") if path.name not in MANAGED_USER_UNITS)


def _installed_units() -> list[str]:
    unit_dir = Path.home() / ".config/systemd/user"
    managed = [unit for unit in MANAGED_USER_UNITS if (unit_dir / unit).exists() or (unit_dir / unit).is_symlink()]
    return [*managed, *_installed_module_units()]


def _profile_units(mode: str) -> list[str]:
    return [
        profile.resolved_unit_name() + ".service"
        for profile in sync_service_profiles()
        if profile.systemd_mode == mode
    ]


def _orphan_mcp_processes() -> list[int]:
    package_root = str((DATA_DIR / "mcp" / "packages").resolve())
    matches: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if package_root in command:
            matches.append(pid)
    return matches


def _stop_orphan_mcp_processes() -> dict[str, Any]:
    pids = _orphan_mcp_processes()
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and any(Path(f"/proc/{pid}").exists() for pid in pids):
        time.sleep(0.1)
    remaining = [pid for pid in pids if Path(f"/proc/{pid}").exists()]
    for pid in remaining:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            os.kill(pid, signal.SIGKILL)
    kill_deadline = time.monotonic() + 2.0
    while time.monotonic() < kill_deadline and any(Path(f"/proc/{pid}").exists() for pid in remaining):
        time.sleep(0.05)
    survivors = [pid for pid in remaining if Path(f"/proc/{pid}").exists()]
    return {
        "component": "orphan-mcp-processes",
        "ok": not survivors,
        "pids": pids,
        "forced": remaining,
        "survivors": survivors,
    }


def stop_all() -> dict[str, Any]:
    results = _stop_local_modules()
    for instance in list_mcp_instances():
        try:
            if instance_status(instance["id"])["running"]:
                stop_instance(instance["id"], actor="runtime.stop-all")
            results.append({"component": f"mcp:{instance['id']}", "ok": True})
        except Exception as exc:
            results.append({"component": f"mcp:{instance['id']}", "ok": False, "error": str(exc)})
    results.append(_stop_orphan_mcp_processes())

    user_units = _installed_units()
    if user_units:
        results.append(
            {
                "component": "systemd-user",
                **_run(["systemctl", "--user", "stop", *user_units]),
            }
        )
    system_units = _profile_units("system")
    if system_units:
        results.append(
            {
                "component": "systemd-system",
                **_run(["systemctl", "stop", *system_units]),
            }
        )
    if shutil.which("docker"):
        inspected = _run(["docker", "container", "inspect", PROMETHEUS_CONTAINER])
        if inspected["ok"]:
            results.append({"component": "prometheus", **_run(["docker", "stop", PROMETHEUS_CONTAINER])})
    results.append(_stop_manual_harbor())
    return {"ok": all(item["ok"] for item in results), "results": results}


def restart_all() -> dict[str, Any]:
    stopped = stop_all()
    started = start_all()
    return {"ok": stopped["ok"] and started["ok"], "stop": stopped, "start": started}


def uninstall_runtime() -> dict[str, Any]:
    stopped = stop_all()
    unit_dir = Path.home() / ".config/systemd/user"
    units = _installed_units()
    actions = [_run(["systemctl", "--user", "disable", *units])] if units else []
    removed_units: list[str] = []
    for unit in units:
        path = unit_dir / unit
        if path.exists() or path.is_symlink():
            path.unlink()
            removed_units.append(str(path))
    actions.append(_run(["systemctl", "--user", "daemon-reload"]))
    actions.append(_run(["systemctl", "--user", "reset-failed"]))

    if shutil.which("docker"):
        inspected = _run(["docker", "container", "inspect", PROMETHEUS_CONTAINER])
        if inspected["ok"]:
            actions.append(_run(["docker", "rm", "-f", PROMETHEUS_CONTAINER]))

    profiles = sync_service_profiles()
    for profile in profiles:
        profile.systemd_mode = "none"
        profile.unit_name = ""
        profile.autostart = False
    save_service_profiles(profiles)
    return {
        "ok": stopped["ok"] and all(action["ok"] for action in actions),
        "stopped": stopped,
        "actions": actions,
        "removed_units": removed_units,
        "data_preserved": True,
    }
