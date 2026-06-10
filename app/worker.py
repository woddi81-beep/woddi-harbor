from __future__ import annotations

from typing import Any

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

from .config import find_module
from .modules import worker_execute, worker_health
from .worker_security import install_worker_auth


class ExecuteRequest(BaseModel):
    action: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


def create_worker_app(module_id: str) -> FastAPI:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Modul nicht gefunden: {module_id}")

    api = FastAPI(title=f"Harbor Worker {module_id}")

    @api.get("/health")
    def health() -> dict:
        return worker_health(module)

    @api.post("/execute", name="direct-execute")
    def execute(body: ExecuteRequest) -> dict:
        return worker_execute(module, body.action.strip(), body.payload)

    return install_worker_auth(api)


def run_worker(module_id: str) -> None:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Modul nicht gefunden: {module_id}")
    api = create_worker_app(module_id)
    uvicorn.run(api, host=module.host, port=module.port, log_level="warning")


def main() -> None:
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m app.worker MODULE_ID")
    run_worker(sys.argv[1])


if __name__ == "__main__":
    main()
