from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def install_worker_auth(app: FastAPI) -> FastAPI:
    expected = os.getenv("HARBOR_INTERNAL_WORKER_TOKEN", "").strip()
    if not expected:
        raise RuntimeError("HARBOR_INTERNAL_WORKER_TOKEN is missing.")

    @app.middleware("http")
    async def require_internal_token(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        authorization = request.headers.get("Authorization", "")
        provided = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        if not provided or not hmac.compare_digest(provided, expected):
            return JSONResponse(status_code=401, content={"detail": "Internal worker authentication required."})
        return await call_next(request)

    return app
