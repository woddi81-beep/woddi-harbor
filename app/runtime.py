from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import DATA_DIR, save_service_profiles, sync_service_profiles
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


def _run(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    return {
        "command": command,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _installed_module_units() -> list[str]:
    unit_dir = Path.home() / ".config/systemd/user"
    return sorted(path.name for path in unit_dir.glob("woddi-harbor-*.service") if path.name not in MANAGED_USER_UNITS)


def _installed_units() -> list[str]:
    unit_dir = Path.home() / ".config/systemd/user"
    managed = [unit for unit in MANAGED_USER_UNITS if (unit_dir / unit).exists() or (unit_dir / unit).is_symlink()]
    return [*managed, *_installed_module_units()]


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
    results: list[dict[str, Any]] = []
    for instance in list_mcp_instances():
        try:
            if instance_status(instance["id"])["running"]:
                stop_instance(instance["id"], actor="runtime.stop-all")
            results.append({"component": f"mcp:{instance['id']}", "ok": True})
        except Exception as exc:
            results.append({"component": f"mcp:{instance['id']}", "ok": False, "error": str(exc)})
    results.append(_stop_orphan_mcp_processes())

    units = _installed_units()
    if units:
        results.append(
            {
                "component": "systemd-user",
                **_run(["systemctl", "--user", "stop", *units]),
            }
        )
    if shutil.which("docker"):
        inspected = _run(["docker", "container", "inspect", PROMETHEUS_CONTAINER])
        if inspected["ok"]:
            results.append({"component": "prometheus", **_run(["docker", "stop", PROMETHEUS_CONTAINER])})
    return {"ok": all(item["ok"] for item in results), "results": results}


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
