from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import BASE_DIR, LOG_DIR, find_module, find_service_profile
from .modules import module_status, restart_module, start_module, stop_module
from .runtime import restart_all, start_all, stop_all
from .services import health_check_service, list_service_profiles, service_action
from .version import __version__


def _run_capture(command: list[str], *, cwd: Path = BASE_DIR, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=False, text=True, capture_output=True, timeout=timeout)


def _git_output(command: list[str], *, timeout: float = 10.0) -> str:
    try:
        completed = _run_capture(command, timeout=timeout)
    except Exception:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def version_status() -> dict[str, Any]:
    root = BASE_DIR
    git_available = (root / ".git").exists() and shutil.which("git") is not None
    payload: dict[str, Any] = {
        "version": __version__,
        "git_available": git_available,
        "git_rev": "unknown",
        "branch": "",
        "upstream": "",
        "upstream_rev": "",
        "dirty": False,
        "ahead": 0,
        "behind": 0,
        "update_available": False,
        "update_supported": git_available,
    }
    if not git_available:
        payload["update_supported"] = False
        return payload

    payload["git_rev"] = _git_output(["git", "rev-parse", "--short", "HEAD"]) or "unknown"
    payload["branch"] = _git_output(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    payload["upstream"] = _git_output(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    payload["upstream_rev"] = _git_output(["git", "rev-parse", "--short", "@{u}"]) if payload["upstream"] else ""
    payload["dirty"] = bool(_git_output(["git", "status", "--porcelain"]))
    if payload["upstream"]:
        counts = _git_output(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"])
        if counts:
            ahead, behind = (counts.split() + ["0", "0"])[:2]
            payload["ahead"] = int(ahead)
            payload["behind"] = int(behind)
            payload["update_available"] = int(behind) > 0
    payload["update_supported"] = bool(payload["upstream"])
    return payload


def update_checkout(*, enabled: bool = True) -> dict[str, Any]:
    root = BASE_DIR
    if not enabled:
        return {"ok": True, "skipped": True, "changed": False, "reason": "disabled"}
    if not (root / ".git").exists():
        return {"ok": True, "skipped": True, "changed": False, "reason": "not-a-git-checkout", "version": version_status()}
    if shutil.which("git") is None:
        return {"ok": True, "skipped": True, "changed": False, "reason": "git-not-found", "version": version_status()}

    upstream = _run_capture(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout=10.0)
    if upstream.returncode != 0:
        return {"ok": True, "skipped": True, "changed": False, "reason": "no-upstream", "version": version_status()}

    status = _run_capture(["git", "status", "--porcelain"], timeout=10.0)
    if status.returncode != 0:
        return {
            "ok": False,
            "skipped": True,
            "changed": False,
            "reason": "status-failed",
            "stderr": status.stderr.strip(),
            "version": version_status(),
        }
    if status.stdout.strip():
        return {
            "ok": True,
            "skipped": True,
            "changed": False,
            "reason": "dirty-working-tree",
            "version": version_status(),
        }

    before = _run_capture(["git", "rev-parse", "HEAD"], timeout=10.0)
    pull = _run_capture(["git", "pull", "--ff-only"], timeout=300.0)
    if pull.returncode != 0:
        return {
            "ok": False,
            "skipped": False,
            "changed": False,
            "reason": "pull-failed",
            "stdout": pull.stdout[-1200:].strip(),
            "stderr": pull.stderr[-1200:].strip(),
            "version": version_status(),
        }

    after = _run_capture(["git", "rev-parse", "HEAD"], timeout=10.0)
    before_rev = before.stdout.strip()
    after_rev = after.stdout.strip()
    changed = before_rev != after_rev
    install: dict[str, Any] = {"ok": True, "skipped": not changed}
    if changed:
        completed = _run_capture([sys.executable, "-m", "pip", "install", "-e", str(root)], timeout=300.0)
        install = {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-1200:].strip(),
            "stderr": completed.stderr[-1200:].strip(),
        }
    return {
        "ok": bool(install.get("ok", True)),
        "skipped": False,
        "changed": changed,
        "before": before_rev,
        "after": after_rev,
        "pull": (pull.stdout or pull.stderr)[-1200:].strip(),
        "install": install,
        "restart_required": changed and bool(install.get("ok", True)),
        "version": version_status(),
    }


def _profile_running(health: dict[str, Any]) -> bool:
    if health.get("kind") == "harbor":
        runtime = health.get("runtime")
        return isinstance(runtime, dict) and bool(runtime.get("ok"))
    runtime = health.get("runtime")
    if isinstance(runtime, dict):
        status = runtime.get("status")
        if isinstance(status, dict):
            return bool(status.get("running"))
    module_status = health.get("status")
    if isinstance(module_status, dict):
        return bool(module_status.get("running"))
    return bool(health.get("ok"))


def service_overview(*, include_health: bool = True) -> dict[str, Any]:
    services: list[dict[str, Any]] = []
    for profile in list_service_profiles():
        entry = {
            **asdict(profile),
            "unit": profile.resolved_unit_name() + ".service",
            "display_name": "Harbor API" if profile.kind == "harbor" else profile.module_id,
            "can_restart": True,
        }
        if include_health:
            try:
                health = service_profile_status(profile.id)
                entry["health"] = health
                entry["running"] = _profile_running(health)
                entry["ok"] = bool(health.get("ok"))
            except Exception as exc:
                entry["health"] = {"ok": False, "error": str(exc)}
                entry["running"] = False
                entry["ok"] = False
        services.append(entry)
    return {"version": version_status(), "services": services}


def service_profile_status(profile_id: str) -> dict[str, Any]:
    profile = find_service_profile(profile_id)
    if profile is None:
        raise ValueError(f"Service profile not found: {profile_id}")
    if profile.kind == "harbor":
        return health_check_service(profile_id)

    payload: dict[str, Any] = {
        "ok": True,
        "profile_id": profile_id,
        "kind": profile.kind,
        "mode": profile.systemd_mode,
        "unit_name": profile.resolved_unit_name() + ".service",
    }
    if profile.systemd_mode in {"user", "system"}:
        payload["systemd"] = service_action(profile_id, "status")
    else:
        payload["systemd"] = {"ok": False, "message": "No systemd unit installed."}

    module = find_module(profile.module_id)
    if module is None:
        payload["runtime"] = {"ok": False, "error": f"Module not found: {profile.module_id}"}
        payload["ok"] = False
        return payload

    status = module_status(module)
    validation_errors = status.get("validation_errors") or []
    runtime_ok = bool(status.get("running")) and not validation_errors
    payload["runtime"] = {"ok": runtime_ok, "status": status}
    payload["ok"] = runtime_ok
    return payload


def run_service_profile_action(profile_id: str, action: str) -> dict[str, Any]:
    profile = find_service_profile(profile_id)
    if profile is None:
        raise ValueError(f"Service profile not found: {profile_id}")
    if action == "status":
        return service_profile_status(profile_id)
    if action == "check":
        return health_check_service(profile_id)
    if action not in {"start", "stop", "restart", "enable", "disable"}:
        raise ValueError(f"Unknown service action: {action}")

    if action in {"enable", "disable"}:
        return service_action(profile_id, action)
    if profile.systemd_mode in {"user", "system"}:
        return service_action(profile_id, action)
    if profile.kind == "module":
        if action == "start":
            return start_module(profile.module_id)
        if action == "stop":
            return stop_module(profile.module_id)
        return restart_module(profile.module_id)
    if action == "start":
        return start_all()
    if action == "stop":
        return stop_all()
    return restart_all()


def schedule_runtime_restart(delay_seconds: float = 1.0) -> dict[str, Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "harbor-restart.log"
    command = [sys.executable, "-m", "app.cli", "runtime", "restart-all"]

    if shutil.which("systemd-run"):
        harbor_profile = find_service_profile("harbor")
        scope = [] if harbor_profile and harbor_profile.systemd_mode == "system" else ["--user"]
        unit_name = f"woddi-harbor-restart-{int(time.time())}"
        completed = subprocess.run(
            [
                "systemd-run",
                *scope,
                "--collect",
                "--unit",
                unit_name,
                f"--on-active={max(1, int(delay_seconds))}s",
                f"--property=WorkingDirectory={BASE_DIR}",
                f"--property=StandardOutput=append:{log_path}",
                f"--property=StandardError=append:{log_path}",
                *command,
            ],
            check=False,
            text=True,
            capture_output=True,
            cwd=BASE_DIR,
            env=os.environ.copy(),
        )
        if completed.returncode == 0:
            return {
                "ok": True,
                "scheduled": True,
                "method": "systemd-run",
                "unit": unit_name,
                "delay_seconds": delay_seconds,
                "log": str(log_path),
                "stdout": completed.stdout.strip(),
            }

    def launch() -> None:
        time.sleep(delay_seconds)
        with log_path.open("a", encoding="utf-8") as log_handle:
            subprocess.Popen(
                command,
                cwd=BASE_DIR,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=os.environ.copy(),
            )

    thread = threading.Thread(target=launch, name="harbor-runtime-restart", daemon=True)
    thread.start()
    return {"ok": True, "scheduled": True, "method": "popen-thread", "delay_seconds": delay_seconds, "log": str(log_path)}
