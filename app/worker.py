from __future__ import annotations

from fastapi import FastAPI, HTTPException
import uvicorn

from .config import find_module
from .modules import worker_execute, worker_health


def create_worker_app(module_id: str) -> FastAPI:
    module = find_module(module_id)
    if module is None:
        raise ValueError(f"Modul nicht gefunden: {module_id}")

    api = FastAPI(title=f"Harbor Worker {module_id}")

    @api.get("/health")
    def health() -> dict:
        return worker_health(module)

    @api.post("/execute")
    def execute(body: dict) -> dict:
        action = str(body.get("action", "")).strip()
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload muss ein JSON-Objekt sein.")
        return worker_execute(module, action, payload)

    return api


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
