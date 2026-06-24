from __future__ import annotations

import signal
import sys
from typing import Any

import uvicorn
from fastapi import FastAPI

from .config import find_module, load_user_named_secret
from .mcp.openstack import create_app
from .worker_security import install_worker_auth


def _openstack_credentials(module_id: str) -> dict[str, str]:
    """Read shared OpenStack connection settings without shared cloud credentials."""
    import os

    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Modul nicht gefunden: {module_id}")

    settings = module.settings or {}
    resolved = {
        "OS_AUTH_URL": str(settings.get("auth_url") or os.getenv("OS_AUTH_URL", "")).strip(),
        "OS_REGION_NAME": str(settings.get("region_name") or os.getenv("OS_REGION_NAME", "")).strip(),
        "OS_INTERFACE": str(settings.get("interface") or os.getenv("OS_INTERFACE", "")).strip(),
        "OS_TIMEOUT": str(settings.get("timeout") or os.getenv("OS_TIMEOUT", "30")).strip() or "30",
        "OS_AUTH_TYPE": "token",
        "OS_TOKEN": "",
    }

    if not resolved["OS_AUTH_URL"]:
        raise ValueError("OS_AUTH_URL fehlt. Bitte im Admin-UI konfigurieren.")

    return resolved


def _openstack_user_credentials(username: str) -> dict[str, str]:
    token = load_user_named_secret(username, "openstack_token")
    if token:
        return {"OS_AUTH_TYPE": "token", "OS_TOKEN": token}

    openstack_username = load_user_named_secret(username, "openstack_username")
    password = load_user_named_secret(username, "openstack_password")
    if not openstack_username or not password:
        return {}

    return {
        "OS_AUTH_TYPE": "password",
        "OS_USERNAME": openstack_username,
        "OS_PASSWORD": password,
        "OS_PROJECT_NAME": load_user_named_secret(username, "openstack_project_name"),
        "OS_USER_DOMAIN_NAME": load_user_named_secret(username, "openstack_user_domain") or "Default",
        "OS_PROJECT_DOMAIN_NAME": load_user_named_secret(username, "openstack_project_domain") or "Default",
    }


def create_worker_app(module_id: str) -> FastAPI:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Modul nicht gefunden: {module_id}")
    return install_worker_auth(create_app(_openstack_credentials(module_id), credential_provider=_openstack_user_credentials))


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
