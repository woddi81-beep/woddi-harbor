from __future__ import annotations

import signal
import sys
from typing import Any

import uvicorn
from fastapi import FastAPI

from .config import find_module
from .mcp.openstack import create_app
from .worker_security import install_worker_auth


def _openstack_credentials(module_id: str) -> dict[str, str]:
    """Read OpenStack credentials from module.settings (set by admin UI)."""
    from app.config import find_module
    import os

    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Modul nicht gefunden: {module_id}")

    settings = module.settings or {}
    fields = {
        "OS_AUTH_URL": "",
        "OS_REGION_NAME": "",
        "OS_INTERFACE": "",
        "OS_TIMEOUT": "30",
        "OS_TOKEN": "",
        "OS_USERNAME": "",
        "OS_PASSWORD": "",
        "OS_PROJECT_NAME": "",
        "OS_USER_DOMAIN_NAME": "Default",
        "OS_PROJECT_DOMAIN_NAME": "Default",
    }

    # Env vars as fallback
    resolved = {key: os.getenv(key, "").strip() for key in fields}

    # Module settings override env vars
    resolved["OS_AUTH_URL"] = settings.get("auth_url") or os.getenv("OS_AUTH_URL", "").strip()
    resolved["OS_REGION_NAME"] = settings.get("region_name") or os.getenv("OS_REGION_NAME", "").strip()
    resolved["OS_TOKEN"] = settings.get("token") or os.getenv("OS_TOKEN", "").strip()
    resolved["OS_USERNAME"] = settings.get("username") or os.getenv("OS_USERNAME", "").strip()
    resolved["OS_PASSWORD"] = settings.get("password") or os.getenv("OS_PASSWORD", "").strip()
    resolved["OS_PROJECT_NAME"] = settings.get("project_name") or os.getenv("OS_PROJECT_NAME", "").strip()
    resolved["OS_USER_DOMAIN_NAME"] = settings.get("user_domain_name") or os.getenv("OS_USER_DOMAIN_NAME", "Default").strip()
    resolved["OS_PROJECT_DOMAIN_NAME"] = settings.get("project_domain_name") or os.getenv("OS_PROJECT_DOMAIN_NAME", "Default").strip()

    if not resolved["OS_AUTH_URL"]:
        raise ValueError("OS_AUTH_URL fehlt. Bitte im Admin-UI konfigurieren.")

    return resolved


def create_worker_app(module_id: str) -> FastAPI:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Modul nicht gefunden: {module_id}")
    return install_worker_auth(create_app(_openstack_credentials(module_id)))


def _install_signal_handlers(server: uvicorn.Server) -> dict[int, Any]:
    previous_handlers: dict[int, Any] = {}

    def _request_shutdown(signum: int, _frame: object) -> None:
        server.should_exit = True
        if signum == signal.SIGINT:
            server.force_exit = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _request_shutdown)
    return previous_handlers


def _restore_signal_handlers(previous_handlers: dict[int, Any]) -> None:
    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)


def run_worker(module_id: str, port: int) -> None:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Modul nicht gefunden: {module_id}")
    api = create_worker_app(module_id)
    config = uvicorn.Config(api, host=module.host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    previous_handlers = _install_signal_handlers(server)
    try:
        server.run()
    finally:
        _restore_signal_handlers(previous_handlers)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python -m app.worker_openstack MODULE_ID PORT")
    run_worker(sys.argv[1], int(sys.argv[2]))


if __name__ == "__main__":
    main()
