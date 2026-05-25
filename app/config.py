from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


ModuleType = Literal["docs", "maildir", "mcp_http"]

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
    llm: LlmSettings = field(default_factory=LlmSettings)


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

    def display_name(self) -> str:
        return self.name or self.id


def ensure_layout() -> None:
    for directory in (CONFIG_DIR, DATA_DIR, LOG_DIR, RUNTIME_DIR, PID_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    harbor_file = CONFIG_DIR / "harbor.json"
    modules_file = CONFIG_DIR / "modules.json"
    prompt_file = CONFIG_DIR / "system_prompt.txt"

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


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_settings() -> HarborSettings:
    ensure_layout()
    payload = _load_json(CONFIG_DIR / "harbor.json")
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
            )
        )
    return modules


def save_modules(modules: list[ModuleConfig]) -> None:
    ensure_layout()
    _write_json(CONFIG_DIR / "modules.json", {"modules": [asdict(module) for module in modules]})


def system_prompt(settings: HarborSettings | None = None) -> str:
    current = settings or load_settings()
    prompt_path = BASE_DIR / current.system_prompt_path
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8", errors="replace").strip()
    return "Du bist Harbor."


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
