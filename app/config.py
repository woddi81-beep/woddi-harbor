from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

ModuleType = Literal["docs", "maildir", "mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"]
ServiceKind = Literal["harbor", "module"]
UserRole = Literal["admin", "operator", "viewer"]
MODULE_TYPES = frozenset({"docs", "maildir", "mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"})
SERVICE_KINDS = frozenset({"harbor", "module"})
USER_ROLES = frozenset({"admin", "operator", "viewer"})

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
LOG_DIR = DATA_DIR / "logs"
RUNTIME_DIR = DATA_DIR / "runtime"
PID_DIR = RUNTIME_DIR / "pids"
SECRETS_DIR = DATA_DIR / "secrets"
INTERNAL_TOKEN_PATH = SECRETS_DIR / "worker.token"
INTERNAL_ENV_PATH = SECRETS_DIR / "worker.env"


def parse_module_type(value: object) -> ModuleType:
    normalized = str(value).strip()
    if normalized not in MODULE_TYPES:
        raise ValueError(f"Ungueltiger Modultyp: {normalized}")
    return cast(ModuleType, normalized)


def parse_service_kind(value: object) -> ServiceKind:
    normalized = str(value).strip()
    if normalized not in SERVICE_KINDS:
        raise ValueError(f"Ungueltige Service-Art: {normalized}")
    return cast(ServiceKind, normalized)


def parse_user_role(value: object) -> UserRole:
    normalized = str(value).strip()
    if normalized not in USER_ROLES:
        raise ValueError(f"Ungueltige Rolle: {normalized}")
    return cast(UserRole, normalized)


@dataclass
class LlmSettings:
    provider: str = "auto"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    api_key_env: str = ""
    timeout_seconds: float = 120.0
    connect_timeout_seconds: float = 5.0
    retry_attempts: int = 3
    max_tokens: int = 1200


@dataclass
class HarborSettings:
    name: str = "Harbor"
    host: str = "127.0.0.1"
    port: int = 9680
    api_workers: int = 4
    system_prompt_path: str = "config/system_prompt.txt"
    onboarding_complete: bool = False
    listen_configured: bool = True
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
    provider: str = ""
    transport: str = "local"
    remote_protocol: str = "auto"
    path: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    host: str = "127.0.0.1"
    port: int = 0
    timeout_seconds: float = 30.0
    top_k: int = 5
    notes: str = ""
    tool_names: list[str] = field(default_factory=list)
    test_action: str = ""
    test_payload: dict[str, Any] = field(default_factory=dict)
    test_expect_contains: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
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
    allowed_modules: list[str] = field(default_factory=lambda: ["*"])
    allowed_tools: list[str] = field(default_factory=lambda: ["*"])


def ensure_layout() -> None:
    for directory in (CONFIG_DIR, DATA_DIR, LOG_DIR, RUNTIME_DIR, PID_DIR, SECRETS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    SECRETS_DIR.chmod(0o700)

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
    if users_file.stat().st_mode & 0o777 != 0o600:
        users_file.chmod(0o600)
    internal_worker_token()


def _load_json(path: Path) -> dict[str, Any]:
    with _locked_path(path, exclusive=False):
        return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with _locked_path(path, exclusive=True):
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.chmod(0o600 if path.name == "users.json" else 0o640)
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
    try:
        from .state import snapshot_config

        snapshot_config(path.name, payload)
    except ImportError:
        pass


@contextmanager
def _locked_path(path: Path, *, exclusive: bool):
    lock_dir = RUNTIME_DIR / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    lock_path = lock_dir / f"{path.name}-{identity}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def internal_worker_token() -> str:
    configured = os.getenv("HARBOR_INTERNAL_WORKER_TOKEN", "").strip()
    if configured:
        return configured
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS_DIR.chmod(0o700)
    with _locked_path(INTERNAL_TOKEN_PATH, exclusive=True):
        if INTERNAL_TOKEN_PATH.exists():
            token = INTERNAL_TOKEN_PATH.read_text(encoding="utf-8").strip()
            if token:
                INTERNAL_TOKEN_PATH.chmod(0o600)
                return token
        token = secrets.token_urlsafe(48)
        INTERNAL_TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
        INTERNAL_TOKEN_PATH.chmod(0o600)
        return token


def internal_worker_env_file() -> Path:
    token = internal_worker_token()
    content = f"HARBOR_INTERNAL_WORKER_TOKEN={token}\n"
    if not INTERNAL_ENV_PATH.exists() or INTERNAL_ENV_PATH.read_text(encoding="utf-8") != content:
        INTERNAL_ENV_PATH.write_text(content, encoding="utf-8")
    INTERNAL_ENV_PATH.chmod(0o600)
    return INTERNAL_ENV_PATH


def resolve_path(raw_path: str, *, base_dir: Path | None = None) -> Path:
    path = Path(raw_path.strip()).expanduser()
    if path.is_absolute():
        return path.resolve()
    root = base_dir or BASE_DIR
    return (root / path).resolve()


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
        provider=str(llm_payload.get("provider", "auto")),
        base_url=str(llm_payload.get("base_url", "")),
        model=str(llm_payload.get("model", "")),
        api_key=str(llm_payload.get("api_key", "")),
        api_key_env=str(llm_payload.get("api_key_env", "")),
        timeout_seconds=float(llm_payload.get("timeout_seconds", 120.0)),
        connect_timeout_seconds=float(llm_payload.get("connect_timeout_seconds", 5.0)),
        retry_attempts=max(1, min(5, int(llm_payload.get("retry_attempts", 3)))),
        max_tokens=int(llm_payload.get("max_tokens", 1200)),
    )
    legacy_listen_config = "listen_configured" not in payload
    configured_host = str(payload.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    settings = HarborSettings(
        name=str(payload.get("name", "Harbor")),
        host=configured_host,
        port=int(payload.get("port", 9680)),
        api_workers=max(1, int(payload.get("api_workers", 4))),
        system_prompt_path=str(payload.get("system_prompt_path", "config/system_prompt.txt")),
        onboarding_complete=bool(payload.get("onboarding_complete", False)),
        listen_configured=True,
        llm=llm,
    )
    if legacy_listen_config and not local_path.exists():
        save_settings(settings)
    return settings


def save_settings(settings: HarborSettings) -> None:
    ensure_layout()
    _write_json(CONFIG_DIR / "harbor.json", asdict(settings))


def modules_config_path(*, for_write: bool = False) -> Path:
    local_path = CONFIG_DIR / "modules.local.json"
    if for_write or local_path.exists():
        return local_path
    return CONFIG_DIR / "modules.json"


def load_modules() -> list[ModuleConfig]:
    ensure_layout()
    payload = _load_json(modules_config_path())
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
                type=parse_module_type(raw["type"]),
                enabled=bool(raw.get("enabled", True)),
                name=str(raw.get("name", "")),
                provider=str(raw.get("provider", "")),
                transport=str(raw.get("transport", "local")),
                remote_protocol=str(raw.get("remote_protocol", "auto")),
                path=str(raw.get("path", "")),
                base_url=str(raw.get("base_url", "")),
                api_key=str(raw.get("api_key", "")),
                api_key_env=str(raw.get("api_key_env", "")),
                host=str(raw.get("host", "127.0.0.1")),
                port=int(raw.get("port", 0)),
                timeout_seconds=float(raw.get("timeout_seconds", 30.0)),
                top_k=int(raw.get("top_k", 5)),
                notes=str(raw.get("notes", "")),
                tool_names=[str(item) for item in raw.get("tool_names", []) if str(item).strip()],
                test_action=str(raw.get("test_action", "")),
                test_payload=dict(raw.get("test_payload", {})) if isinstance(raw.get("test_payload", {}), dict) else {},
                test_expect_contains=[str(item) for item in raw.get("test_expect_contains", []) if str(item).strip()],
                settings=dict(raw.get("settings", {})) if isinstance(raw.get("settings", {}), dict) else {},
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
    _write_json(modules_config_path(for_write=True), {"modules": serialized})
    sync_service_profiles()


def load_service_profiles() -> list[ServiceProfile]:
    ensure_layout()
    payload = _load_json(CONFIG_DIR / "services.json")
    profiles: list[ServiceProfile] = []
    for raw in payload.get("profiles", []):
        profiles.append(
            ServiceProfile(
                id=str(raw["id"]),
                kind=parse_service_kind(raw["kind"]),
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
    local_path = CONFIG_DIR / "users.local.json"
    payload = _load_json(local_path if local_path.exists() else CONFIG_DIR / "users.json")
    users: list[HarborUser] = []
    for raw in payload.get("users", []):
        users.append(
            HarborUser(
                username=str(raw["username"]),
                password_hash=str(raw["password_hash"]),
                role=parse_user_role(raw.get("role", "viewer")),
                enabled=bool(raw.get("enabled", True)),
                allowed_modules=[str(item) for item in raw.get("allowed_modules", ["*"])],
                allowed_tools=[str(item) for item in raw.get("allowed_tools", ["*"])],
            )
        )
    return users


def save_users(users: list[HarborUser]) -> None:
    ensure_layout()
    path = CONFIG_DIR / "users.local.json"
    _write_json(path, {"users": [asdict(user) for user in users]})
    path.chmod(0o600)


def find_user(username: str) -> HarborUser | None:
    normalized = username.strip()
    for user in load_users():
        if user.username == normalized:
            return user
    return None


def system_prompt(settings: HarborSettings | None = None) -> str:
    current = settings or load_settings()
    prompt_path = resolve_path(current.system_prompt_path)
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8", errors="replace").strip()
    return "Du bist Harbor."


def save_system_prompt(text: str, settings: HarborSettings | None = None) -> Path:
    current = settings or load_settings()
    prompt_path = resolve_path(current.system_prompt_path)
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


def module_named_secret_path(module_id: str, secret_name: str) -> Path:
    for value, label in ((module_id, "module_id"), (secret_name, "secret_name")):
        if not value or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in value):
            raise ValueError(f"Ungueltiger Wert fuer {label}.")
    return SECRETS_DIR / "modules" / module_id / f"{secret_name}.secret"


def load_module_named_secret(module_id: str, secret_name: str) -> str:
    path = module_named_secret_path(module_id, secret_name)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def save_module_named_secret(module_id: str, secret_name: str, value: str) -> Path:
    secret = value.strip()
    if not secret:
        raise ValueError("Secret darf nicht leer sein.")
    path = module_named_secret_path(module_id, secret_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def delete_module_named_secret(module_id: str, secret_name: str) -> None:
    module_named_secret_path(module_id, secret_name).unlink(missing_ok=True)


def user_named_secret_path(username: str, secret_name: str) -> Path:
    normalized_username = username.strip()
    if not normalized_username:
        raise ValueError("Benutzername darf nicht leer sein.")
    if not secret_name or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in secret_name
    ):
        raise ValueError("Ungueltiger Wert fuer secret_name.")
    user_id = hashlib.sha256(normalized_username.encode("utf-8")).hexdigest()
    return SECRETS_DIR / "users" / user_id / f"{secret_name}.secret"


def load_user_named_secret(username: str, secret_name: str) -> str:
    path = user_named_secret_path(username, secret_name)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def save_user_named_secret(username: str, secret_name: str, value: str) -> Path:
    secret = value.strip()
    if not secret:
        raise ValueError("Secret darf nicht leer sein.")
    path = user_named_secret_path(username, secret_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def delete_user_named_secret(username: str, secret_name: str) -> None:
    user_named_secret_path(username, secret_name).unlink(missing_ok=True)


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


def resolve_module_source_path(source: ModuleSource) -> Path:
    return resolve_path(source.path)
