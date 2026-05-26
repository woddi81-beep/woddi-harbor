from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


ModuleType = Literal["docs", "maildir", "mcp_http"]
ServiceKind = Literal["harbor", "module"]
UserRole = Literal["admin", "operator", "viewer"]

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
LOG_DIR = DATA_DIR / "logs"
RUNTIME_DIR = DATA_DIR / "runtime"
PID_DIR = RUNTIME_DIR / "pids"


@dataclass
class LlmSettings:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    api_key_env: str = ""
    timeout_seconds: float = 60.0
    max_tokens: int = 1200


@dataclass
class HarborSettings:
    name: str = "Harbor"
    host: str = "127.0.0.1"
    port: int = 9680
    system_prompt_path: str = "config/system_prompt.txt"
    onboarding_complete: bool = False
    llm: LlmSettings = field(default_factory=LlmSettings)


@dataclass
class ModuleSource:
    id: str
    path: str
    label: str = ""
    enabled: bool = True

    def display_name(self) -> str:
        return self.label or self.id


@dataclass
class ModuleConfig:
    id: str
    type: ModuleType
    enabled: bool = True
    name: str = ""
    transport: str = "local"
    path: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    host: str = "127.0.0.1"
    port: int = 0
    timeout_seconds: float = 30.0
    top_k: int = 5
    notes: str = ""
    sources: list[ModuleSource] = field(default_factory=list)

    def display_name(self) -> str:
        return self.name or self.id

    def local_sources(self) -> list[ModuleSource]:
        if self.sources:
            return self.sources
        if self.path.strip():
            return [ModuleSource(id=f"{self.id}-source-1", path=self.path)]
        return []


@dataclass
class ServiceProfile:
    id: str
    kind: ServiceKind
    module_id: str = ""
    enabled: bool = True
    autostart: bool = False
    systemd_mode: str = "none"
    unit_name: str = ""

    def resolved_unit_name(self) -> str:
        if self.unit_name:
            return self.unit_name
        if self.kind == "harbor":
            return "woddi-harbor"
        return f"woddi-harbor-{self.module_id}"


@dataclass
class HarborUser:
    username: str
    password_hash: str
    role: UserRole = "viewer"
    enabled: bool = True


def ensure_layout() -> None:
    for directory in (CONFIG_DIR, DATA_DIR, LOG_DIR, RUNTIME_DIR, PID_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    harbor_file = CONFIG_DIR / "harbor.json"
    modules_file = CONFIG_DIR / "modules.json"
    prompt_file = CONFIG_DIR / "system_prompt.txt"
    services_file = CONFIG_DIR / "services.json"
    users_file = CONFIG_DIR / "users.json"

    if not harbor_file.exists():
        harbor_file.write_text(
            json.dumps(asdict(HarborSettings()), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if not modules_file.exists():
        modules_file.write_text('{\n  "modules": []\n}\n', encoding="utf-8")
    if not prompt_file.exists():
        prompt_file.write_text(
            "Du bist Harbor, ein praeziser lokaler AI-Assistent. "
            "Nutze bereitgestellten Modul-Kontext bevorzugt vor Vermutungen. "
            "Wenn Daten fehlen, sage das klar.\n",
            encoding="utf-8",
        )
    if not services_file.exists():
        services_file.write_text('{\n  "profiles": []\n}\n', encoding="utf-8")
    if not users_file.exists():
        users_file.write_text('{\n  "users": []\n}\n', encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings() -> HarborSettings:
    ensure_layout()
    payload = _load_json(CONFIG_DIR / "harbor.json")
    local_path = CONFIG_DIR / "harbor.local.json"
    if local_path.exists():
        payload = _merge_dict(payload, _load_json(local_path))
    llm_payload = payload.get("llm", {})
    llm = LlmSettings(
        base_url=str(llm_payload.get("base_url", "")),
        model=str(llm_payload.get("model", "")),
        api_key=str(llm_payload.get("api_key", "")),
        api_key_env=str(llm_payload.get("api_key_env", "")),
        timeout_seconds=float(llm_payload.get("timeout_seconds", 60.0)),
        max_tokens=int(llm_payload.get("max_tokens", 1200)),
    )
    return HarborSettings(
        name=str(payload.get("name", "Harbor")),
        host=str(payload.get("host", "127.0.0.1")),
        port=int(payload.get("port", 9680)),
        system_prompt_path=str(payload.get("system_prompt_path", "config/system_prompt.txt")),
        onboarding_complete=bool(payload.get("onboarding_complete", False)),
        llm=llm,
    )


def save_settings(settings: HarborSettings) -> None:
    ensure_layout()
    _write_json(CONFIG_DIR / "harbor.json", asdict(settings))


def load_modules() -> list[ModuleConfig]:
    ensure_layout()
    payload = _load_json(CONFIG_DIR / "modules.json")
    modules: list[ModuleConfig] = []
    for raw in payload.get("modules", []):
        raw_sources = raw.get("sources", [])
        sources: list[ModuleSource] = []
        if isinstance(raw_sources, list):
            for index, item in enumerate(raw_sources, start=1):
                if not isinstance(item, dict):
                    continue
                source_path = str(item.get("path", "")).strip()
                if not source_path:
                    continue
                source_id = str(item.get("id", "")).strip() or f"{raw['id']}-source-{index}"
                sources.append(
                    ModuleSource(
                        id=source_id,
                        path=source_path,
                        label=str(item.get("label", "")),
                        enabled=bool(item.get("enabled", True)),
                    )
                )
        legacy_path = str(raw.get("path", "")).strip()
        if not sources and legacy_path:
            sources.append(ModuleSource(id=f"{raw['id']}-source-1", path=legacy_path))
        modules.append(
            ModuleConfig(
                id=str(raw["id"]),
                type=str(raw["type"]),
                enabled=bool(raw.get("enabled", True)),
                name=str(raw.get("name", "")),
                transport=str(raw.get("transport", "local")),
                path=str(raw.get("path", "")),
                base_url=str(raw.get("base_url", "")),
                api_key=str(raw.get("api_key", "")),
                api_key_env=str(raw.get("api_key_env", "")),
                host=str(raw.get("host", "127.0.0.1")),
                port=int(raw.get("port", 0)),
                timeout_seconds=float(raw.get("timeout_seconds", 30.0)),
                top_k=int(raw.get("top_k", 5)),
                notes=str(raw.get("notes", "")),
                sources=sources,
            )
        )
    return modules


def save_modules(modules: list[ModuleConfig]) -> None:
    ensure_layout()
    serialized: list[dict[str, Any]] = []
    for module in modules:
        payload = asdict(module)
        payload["path"] = module.local_sources()[0].path if module.local_sources() else module.path
        serialized.append(payload)
    _write_json(CONFIG_DIR / "modules.json", {"modules": serialized})
    sync_service_profiles()


def load_service_profiles() -> list[ServiceProfile]:
    ensure_layout()
    payload = _load_json(CONFIG_DIR / "services.json")
    profiles: list[ServiceProfile] = []
    for raw in payload.get("profiles", []):
        profiles.append(
            ServiceProfile(
                id=str(raw["id"]),
                kind=str(raw["kind"]),
                module_id=str(raw.get("module_id", "")),
                enabled=bool(raw.get("enabled", True)),
                autostart=bool(raw.get("autostart", False)),
                systemd_mode=str(raw.get("systemd_mode", "none")),
                unit_name=str(raw.get("unit_name", "")),
            )
        )
    return profiles


def save_service_profiles(profiles: list[ServiceProfile]) -> None:
    ensure_layout()
    _write_json(CONFIG_DIR / "services.json", {"profiles": [asdict(profile) for profile in profiles]})


def sync_service_profiles() -> list[ServiceProfile]:
    profiles = {profile.id: profile for profile in load_service_profiles()}
    if "harbor" not in profiles:
        profiles["harbor"] = ServiceProfile(id="harbor", kind="harbor")
    module_ids = {module.id for module in load_modules() if module.transport == "local"}
    for module_id in module_ids:
        profile_id = f"module:{module_id}"
        if profile_id not in profiles:
            profiles[profile_id] = ServiceProfile(id=profile_id, kind="module", module_id=module_id)
    for profile_id in list(profiles):
        profile = profiles[profile_id]
        if profile.kind == "module" and profile.module_id not in module_ids:
            del profiles[profile_id]
    ordered = sorted(profiles.values(), key=lambda item: item.id)
    save_service_profiles(ordered)
    return ordered


def find_service_profile(profile_id: str) -> ServiceProfile | None:
    for profile in sync_service_profiles():
        if profile.id == profile_id:
            return profile
    return None


def load_users() -> list[HarborUser]:
    ensure_layout()
    payload = _load_json(CONFIG_DIR / "users.json")
    users: list[HarborUser] = []
    for raw in payload.get("users", []):
        users.append(
            HarborUser(
                username=str(raw["username"]),
                password_hash=str(raw["password_hash"]),
                role=str(raw.get("role", "viewer")),
                enabled=bool(raw.get("enabled", True)),
            )
        )
    return users


def save_users(users: list[HarborUser]) -> None:
    ensure_layout()
    _write_json(CONFIG_DIR / "users.json", {"users": [asdict(user) for user in users]})


def find_user(username: str) -> HarborUser | None:
    normalized = username.strip()
    for user in load_users():
        if user.username == normalized:
            return user
    return None


def system_prompt(settings: HarborSettings | None = None) -> str:
    current = settings or load_settings()
    prompt_path = BASE_DIR / current.system_prompt_path
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8", errors="replace").strip()
    return "Du bist Harbor."


def save_system_prompt(text: str, settings: HarborSettings | None = None) -> Path:
    current = settings or load_settings()
    prompt_path = BASE_DIR / current.system_prompt_path
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(text.strip() + "\n", encoding="utf-8")
    return prompt_path


def llm_api_key(settings: HarborSettings) -> str:
    if settings.llm.api_key:
        return settings.llm.api_key
    if settings.llm.api_key_env:
        return os.getenv(settings.llm.api_key_env, "").strip()
    return ""


def module_secret(module: ModuleConfig) -> str:
    if module.api_key:
        return module.api_key
    if module.api_key_env:
        return os.getenv(module.api_key_env, "").strip()
    return ""


def find_module(module_id: str) -> ModuleConfig | None:
    for module in load_modules():
        if module.id == module_id:
            return module
    return None


def module_sources(module: ModuleConfig, *, enabled_only: bool = True) -> list[ModuleSource]:
    sources = module.local_sources()
    if enabled_only:
        return [source for source in sources if source.enabled]
    return sources
