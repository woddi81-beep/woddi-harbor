from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .state import create_job, update_job


MAX_JOB_WORKERS = max(2, min(16, (os.cpu_count() or 2) // 2))
_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_JOB_WORKERS, thread_name_prefix="harbor-job")


def submit_job(
    kind: str,
    target: str,
    payload: dict[str, Any],
    operation: Callable[[], dict[str, Any]],
) -> str:
    job_id = create_job(kind, target, payload)

    def run() -> None:
        update_job(job_id, "running")
        try:
            result = operation()
            update_job(job_id, "completed", result=result)
        except Exception as exc:
            update_job(job_id, "failed", error=str(exc))

    _EXECUTOR.submit(run)
    return job_id
