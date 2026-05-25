from __future__ import annotations

import subprocess
from pathlib import Path

from .config import BASE_DIR, CONFIG_DIR, ModuleConfig, ServiceProfile, find_module, find_service_profile, save_service_profiles, sync_service_profiles


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
