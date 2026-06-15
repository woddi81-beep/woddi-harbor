from __future__ import annotations

import os
import stat
from typing import Any

from .config import CONFIG_DIR, INTERNAL_TOKEN_PATH, load_modules, load_settings, load_users
from .llm import llm_health
from .mcp.openstack import openstack_sdk_available
from .modules import discover_remote_module, health_check_module, validation_errors_by_module
from .sources import source_overview
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
    llm = llm_health(settings)
    add("llm", bool(llm["ok"]), str(llm.get("detail", "LLM-Status unbekannt")))
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
    integration_health: dict[str, str] = {}
    for module in modules:
        if not module.enabled or module.type not in {"mcp_http", "netbox_mcp", "openstack_mcp", "sap_docs_mcp"}:
            continue
        try:
            result = health_check_module(module.id)
            if not result.get("ok"):
                integration_health[module.id] = str(
                    result.get("validation_errors")
                    or result.get("remote")
                    or result.get("local")
                    or "nicht erreichbar"
                )
            elif module.type == "netbox_mcp":
                discovery = discover_remote_module(module)
                if not discovery.get("ok"):
                    integration_health[module.id] = str(
                        discovery.get("attempts") or "NetBox Upstream Discovery fehlgeschlagen"
                    )
        except Exception as exc:
            integration_health[module.id] = str(exc)
    add(
        "integrations",
        not integration_health,
        f"Nicht betriebsbereit: {integration_health}" if integration_health else "Alle aktivierten Integrationen betriebsbereit",
    )
    openstack_modules = [module.id for module in modules if module.type == "openstack_mcp" and module.enabled]
    sdk_available = openstack_sdk_available()
    add(
        "openstack_sdk",
        not openstack_modules or sdk_available,
        f"OpenStack-Module {openstack_modules}; openstacksdk {'installiert' if sdk_available else 'fehlt'}",
    )
    sources = source_overview()
    unhealthy_sources = [
        source["id"]
        for source in sources
        if source["enabled"] and (not source["exists"] or not (source["quality"] or {}).get("healthy", False))
    ]
    add(
        "sources",
        not unhealthy_sources,
        f"{len(sources)} Quellen, ungesund: {unhealthy_sources}",
    )
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
