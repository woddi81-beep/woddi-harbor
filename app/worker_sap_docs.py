from __future__ import annotations

import signal
import sys
from typing import Any

import uvicorn
from fastapi import FastAPI

from .config import find_module
from .mcp.sap_docs import create_sap_docs_app
from .worker_security import install_worker_auth


def create_worker_app(module_id: str) -> FastAPI:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Modul nicht gefunden: {module_id}")
    docs_url = str(module.settings.get("docs_url", "")).strip() or str(module.settings.get("base_url", "")).strip()
    if docs_url:
        return install_worker_auth(create_sap_docs_app(base_url=docs_url))
    return install_worker_auth(create_sap_docs_app())


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
        raise SystemExit("Usage: python -m app.worker_sap_docs MODULE_ID PORT")
    run_worker(sys.argv[1], int(sys.argv[2]))


if __name__ == "__main__":
    main()
