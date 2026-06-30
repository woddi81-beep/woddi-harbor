from __future__ import annotations

import os
import socket
import time
from typing import Any

from .backup import create_backup
from .modules import execute_module
from .sources import sync_source
from .state import claim_next_job, create_job, update_job


def submit_job(kind: str, target: str, payload: dict[str, Any] | None = None) -> str:
    return create_job(kind, target, payload or {})


def execute_job(job: dict[str, Any]) -> dict[str, Any]:
    kind = job["kind"]
    if kind == "module.reindex":
        return execute_module(job["target"], "reindex", job["payload"])
    if kind == "source.sync":
        return sync_source(job["target"])
    if kind == "backup.create":
        path = create_backup(str(job["payload"].get("label", "scheduled")))
        return {"ok": True, "path": str(path)}
    raise ValueError(f"Unknown job type: {kind}")


def run_job_worker(*, once: bool = False, poll_seconds: float = 1.0) -> None:
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    while True:
        job = claim_next_job(worker_id)
        if job is None:
            if once:
                return
            time.sleep(max(0.1, poll_seconds))
            continue
        try:
            update_job(job["id"], "completed", result=execute_job(job))
        except Exception as exc:
            update_job(job["id"], "failed", error=str(exc))
        if once:
            return
