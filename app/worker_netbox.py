from __future__ import annotations

import os
import sys

import uvicorn

from .config import find_module
from .mcp.netbox import create_app


def run_worker(module_id: str, port: int) -> None:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Modul nicht gefunden: {module_id}")
    netbox_url = os.getenv("NETBOX_URL", "").strip()
    netbox_token = os.getenv("NETBOX_TOKEN", "").strip()
    if not netbox_url:
        raise ValueError("NETBOX_URL fehlt.")
    if not netbox_token:
        raise ValueError("NETBOX_TOKEN fehlt.")
    api = create_app(netbox_url=netbox_url, netbox_token=netbox_token)
    uvicorn.run(api, host=module.host, port=port, log_level="warning")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python -m app.worker_netbox MODULE_ID PORT")
    run_worker(sys.argv[1], int(sys.argv[2]))


if __name__ == "__main__":
    main()
