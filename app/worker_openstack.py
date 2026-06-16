from __future__ import annotations

import signal
import sys
from typing import Any

import uvicorn
from fastapi import FastAPI

from .config import find_module
from .mcp.openstack import create_app
from .worker_security import install_worker_auth


def _openstack_credentials() -> dict[str, str]:
    from app.state import load_user_named_secret, load_module_named_secret
    fields = {
        "OS_AUTH_URL": "",
        "OS_REGION_NAME": "",
        "OS_INTERFACE": "",
        "OS_TIMEOUT": "30",
        "OS_AUTH_TYPE": "token",
        "OS_TOKEN": "",
        "OS_USERNAME": "",
        "OS_PASSWORD": "",
        "OS_PROJECT_NAME": "",
        "OS_USER_DOMAIN_NAME": "Default",
        "OS_PROJECT_DOMAIN_NAME": "Default",
    }
    import os

    resolved = {key: os.getenv(key, "").strip() for key in fields}
    if not resolved["OS_AUTH_URL"]:
        raise ValueError("OS_AUTH_URL fehlt.")

    # Load per-user credentials (password auth takes priority over token)
    try:
        resolved["OS_USERNAME"] = load_user_named_secret(None, "openstack_username") or resolved.get("OS_USERNAME", "")
        resolved["OS_PASSWORD"] = load_user_named_secret(None, "openstack_password") or resolved.get("OS_PASSWORD", "")
        resolved["OS_PROJECT_NAME"] = load_user_named_secret(None, "openstack_project_name") or resolved.get("OS_PROJECT_NAME", "")
        resolved["OS_USER_DOMAIN_NAME"] = load_user_named_secret(None, "openstack_user_domain") or resolved.get("OS_USER_DOMAIN_NAME", "Default")
        resolved["OS_PROJECT_DOMAIN_NAME"] = load_user_named_secret(None, "openstack_project_domain") or resolved.get("OS_PROJECT_DOMAIN_NAME", "Default")
        resolved["OS_TOKEN"] = load_user_named_secret(None, "openstack_token") or resolved.get("OS_TOKEN", "")
    except Exception:
        pass

    return resolved


def create_worker_app(module_id: str) -> FastAPI:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Modul nicht gefunden: {module_id}")
    return install_worker_auth(create_app(_openstack_credentials()))


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
