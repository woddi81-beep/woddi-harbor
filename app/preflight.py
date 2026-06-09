from __future__ import annotations

import os
import stat
from typing import Any

from .config import CONFIG_DIR, INTERNAL_TOKEN_PATH, load_modules, load_settings, load_users
from .modules import validation_errors_by_module
from .state import DATABASE_PATH, initialize_database


def production_check() -> dict[str, Any]:
    settings = load_settings()
    users = load_users()
    modules = load_modules()
    initialize_database()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, severity: str = "error") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "severity": severity})

    add("users", bool(users), f"{len(users)} Benutzer konfiguriert")
    add("admin", any(user.enabled and user.role == "admin" for user in users), "Mindestens ein aktiver Admin")
    add(
        "llm",
        bool(settings.llm.base_url and settings.llm.model),
        f"{settings.llm.model or 'kein Modell'} via {settings.llm.base_url or 'keine URL'}",
    )
    add(
        "llm_secret",
        not bool(settings.llm.api_key),
        "LLM-Key wird per ENV referenziert" if not settings.llm.api_key else "Inline LLM-Key gefunden",
    )
    add("api_workers", settings.api_workers >= 2, f"{settings.api_workers} API-Worker", severity="warning")
    add("database", DATABASE_PATH.exists(), str(DATABASE_PATH))
    database_mode = stat.S_IMODE(DATABASE_PATH.stat().st_mode) if DATABASE_PATH.exists() else 0
    add("database_permissions", database_mode & 0o077 == 0, oct(database_mode))
    token_mode = stat.S_IMODE(INTERNAL_TOKEN_PATH.stat().st_mode) if INTERNAL_TOKEN_PATH.exists() else 0
    add("worker_token_permissions", token_mode & 0o077 == 0, oct(token_mode))
    validation = validation_errors_by_module(modules)
    invalid = {module_id: errors for module_id, errors in validation.items() if errors}
    add("modules", not invalid, f"{len(modules)} Module, {len(invalid)} ungueltig: {invalid}")
    add(
        "public_bind",
        settings.host in {"127.0.0.1", "::1", "localhost"},
        f"Bind-Adresse {settings.host}; extern nur hinter TLS-Reverse-Proxy",
        severity="warning",
    )
    config_world_writable = [
        str(path)
        for path in CONFIG_DIR.glob("*")
        if path.is_file() and stat.S_IMODE(path.stat().st_mode) & 0o002
    ]
    add("config_permissions", not config_world_writable, f"world-writable: {config_world_writable}")
    errors = [check for check in checks if not check["ok"] and check["severity"] == "error"]
    warnings = [check for check in checks if not check["ok"] and check["severity"] == "warning"]
    return {
        "ok": not errors,
        "checks": checks,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "cpu_count": os.cpu_count() or 0,
    }
