from __future__ import annotations

import subprocess
import shlex
from pathlib import Path

import httpx

from .config import BASE_DIR, CONFIG_DIR, ModuleConfig, ServiceProfile, find_module, find_service_profile, internal_worker_env_file, load_settings, save_service_profiles, sync_service_profiles
from .modules import health_check_module, module_url, module_worker_command


SYSTEMD_DIR = BASE_DIR / "systemd"
HARBOR_TEMPLATE = SYSTEMD_DIR / "woddi-harbor.service.tpl"
MODULE_TEMPLATE = SYSTEMD_DIR / "woddi-harbor-module.service.tpl"


def _systemctl_scope(mode: str) -> list[str]:
    if mode == "user":
        return ["systemctl", "--user"]
    if mode == "system":
        return ["systemctl"]
    raise ValueError(f"Unbekannter systemd mode: {mode}")


def _unit_dir(mode: str) -> Path:
    if mode == "user":
        return Path.home() / ".config/systemd/user"
    if mode == "system":
        return Path("/etc/systemd/system")
    raise ValueError(f"Unbekannter systemd mode: {mode}")


def _render_template(template_path: Path, replacements: dict[str, str]) -> str:
    content = template_path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        content = content.replace(key, value)
    return content


def install_service(profile_id: str, mode: str) -> dict:
    profile = find_service_profile(profile_id)
    if profile is None:
        raise ValueError(f"Service-Profil nicht gefunden: {profile_id}")
    if mode not in {"user", "system"}:
        raise ValueError("mode muss 'user' oder 'system' sein.")

    unit_dir = _unit_dir(mode)
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_name = profile.resolved_unit_name() + ".service"
    target_path = unit_dir / unit_name

    if profile.kind == "harbor":
        from .config import load_settings

        settings = load_settings()
        rendered = _render_template(
            HARBOR_TEMPLATE,
            {
                "__HARBOR_WORKDIR__": str(BASE_DIR),
                "__HARBOR_HOST__": settings.host,
                "__HARBOR_PORT__": str(settings.port),
            },
        )
    else:
        module = find_module(profile.module_id)
        if module is None:
            raise ValueError(f"Modul fuer Profil nicht gefunden: {profile.module_id}")
        rendered = _render_template(
            MODULE_TEMPLATE,
            {
                "__HARBOR_WORKDIR__": str(BASE_DIR),
                "__HARBOR_MODULE_ID__": module.id,
                "__HARBOR_MODULE_NAME__": module.display_name(),
                "__HARBOR_MODULE_COMMAND__": shlex.join(module_worker_command(module)),
                "__HARBOR_WORKER_ENV_FILE__": str(internal_worker_env_file()),
            },
        )
    target_path.write_text(rendered, encoding="utf-8")
    subprocess.run(_systemctl_scope(mode) + ["daemon-reload"], check=True)

    profiles = sync_service_profiles()
    for existing in profiles:
        if existing.id == profile_id:
            existing.systemd_mode = mode
            existing.unit_name = profile.resolved_unit_name()
            break
    save_service_profiles(profiles)
    return {"ok": True, "unit_path": str(target_path), "unit_name": unit_name, "mode": mode}


def install_and_optionally_enable_service(profile_id: str, mode: str, *, enable: bool = False, start: bool = False) -> dict:
    result = install_service(profile_id, mode)
    actions: list[dict] = []
    if enable:
        actions.append(service_action(profile_id, "enable"))
    if start:
        actions.append(service_action(profile_id, "start"))
    return {**result, "actions": actions}


def service_action(profile_id: str, action: str) -> dict:
    profile = find_service_profile(profile_id)
    if profile is None:
        raise ValueError(f"Service-Profil nicht gefunden: {profile_id}")
    if profile.systemd_mode not in {"user", "system"}:
        raise ValueError("Fuer dieses Profil ist noch keine systemd-Unit installiert.")
    if action not in {"start", "stop", "restart", "enable", "disable", "status"}:
        raise ValueError(f"Unbekannte Service-Aktion: {action}")
    cmd = _systemctl_scope(profile.systemd_mode) + [action, profile.resolved_unit_name() + ".service"]
    completed = subprocess.run(cmd, check=False, text=True, capture_output=True)
    return {
        "ok": completed.returncode == 0,
        "action": action,
        "profile_id": profile_id,
        "mode": profile.systemd_mode,
        "unit_name": profile.resolved_unit_name() + ".service",
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def list_service_profiles() -> list[ServiceProfile]:
    return sync_service_profiles()


def health_check_service(profile_id: str) -> dict:
    profile = find_service_profile(profile_id)
    if profile is None:
        raise ValueError(f"Service-Profil nicht gefunden: {profile_id}")
    payload = {
        "ok": True,
        "profile_id": profile_id,
        "kind": profile.kind,
        "mode": profile.systemd_mode,
        "unit_name": profile.resolved_unit_name() + ".service",
    }
    if profile.systemd_mode in {"user", "system"}:
        status = service_action(profile_id, "status")
        payload["systemd"] = status
        payload["ok"] = payload["ok"] and bool(status.get("ok"))
    else:
        payload["systemd"] = {
            "ok": False,
            "message": "Keine systemd-Unit installiert.",
        }
    if profile.kind == "harbor":
        settings = load_settings()
        api_url = f"http://{settings.host}:{settings.port}/api/health"
        try:
            with httpx.Client(timeout=4.0) as client:
                response = client.get(api_url)
            payload["runtime"] = {
                "ok": response.is_success,
                "url": api_url,
                "status_code": response.status_code,
                "body": response.json() if response.is_success else response.text[:600],
            }
            payload["ok"] = payload["ok"] and response.is_success
        except Exception as exc:
            payload["runtime"] = {"ok": False, "url": api_url, "error": str(exc)}
            payload["ok"] = False
    elif profile.kind == "module":
        payload["runtime"] = health_check_module(profile.module_id)
        payload["ok"] = payload["ok"] and bool(payload["runtime"].get("ok"))
    return payload
