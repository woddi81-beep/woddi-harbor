#!/usr/bin/env python3
"""Enhanced error reporting utilities for Harbor MCP modules."""

from __future__ import annotations

import traceback
from typing import Any


def rich_error(exc: Exception, context: str = "") -> dict[str, Any]:
    """Build a detailed error dict for MCP error responses."""
    error: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc),
        "context": context,
    }

    # Add cause chain
    cause_chain = []
    current = exc.__cause__
    while current:
        cause_chain.append(f"{type(current).__name__}: {str(current)}")
        current = getattr(current, "__cause__", None)
    if cause_chain:
        error["cause_chain"] = cause_chain

    # Extract HTTP status/code if available
    import httpx
    if isinstance(exc, httpx.HTTPStatusError):
        error["http_status"] = exc.response.status_code
        error["http_reason"] = exc.response.reason_phrase
        try:
            error["response_body"] = exc.response.text[:500]
        except Exception:
            pass
    elif isinstance(exc, httpx.TimeoutException):
        error["is_timeout"] = True
    elif isinstance(exc, httpx.ConnectError):
        error["is_connect_error"] = True
        error["is_timeout"] = isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout))

    return error


def format_error_for_user(exc: Exception, context: str = "") -> str:
    """Format an exception as a human-readable user-facing error message."""
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return (
            f"⚠️ **Zeitüberschreitung** — Der Server hat nach {getattr(exc, 'seconds', '?')}s nicht geantwortet.\n"
            f"   -> Prüfe ob der Server erreichbar und nicht überlastet ist."
        )
    if isinstance(exc, httpx.ConnectError):
        return (
            f"⚠️ **Verbindung fehlgeschlagen** — Server nicht erreichbar.\n"
            f"   -> Prüfe die URL und Netzwerk-Konnektivität.\n"
            f"   -> Fehler: {exc}"
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        reason = exc.response.reason_phrase
        msg = f"⚠️ **HTTP {status} {reason}**"
        try:
            body = exc.response.json()
            if "error" in body:
                msg += f"\n   -> {body['error']}"
            elif "message" in body:
                msg += f"\n   -> {body['message']}"
        except Exception:
            pass
        return msg
    if isinstance(exc, ValueError):
        return f"⚠️ **Konfigurationsfehler**: {exc}"
    if context:
        return f"⚠️ **{context} fehlgeschlagen**: {exc}"
    return f"⚠️ {exc}"


def exception_info(exc: Exception) -> str:
    """Return a short one-line exception summary with type and message."""
    return f"{type(exc).__name__}: {exc}"


def traceback_summary(exc: Exception, limit: int = 5) -> str:
    """Return a limited traceback as a string."""
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    if limit and len(lines) > limit * 2:
        lines = lines[:limit] + ["  ... (traceback truncated) ..."] + lines[-limit:]
    return "".join(lines)
