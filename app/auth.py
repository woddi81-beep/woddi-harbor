from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, status

from .config import HarborUser, UserRole, find_user, load_users

ROLE_LEVEL: dict[UserRole, int] = {
    "viewer": 1,
    "operator": 2,
    "admin": 3,
}
_AUTH_FAILURES: dict[str, deque[float]] = defaultdict(deque)
_AUTH_LOCK = threading.Lock()
AUTH_WINDOW_SECONDS = 300.0
AUTH_MAX_FAILURES = 8


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = 240_000) -> str:
    if not password:
        raise ValueError("Password must not be empty.")
    current_salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), current_salt, iterations)
    return "pbkdf2_sha256${iterations}${salt}${digest}".format(
        iterations=iterations,
        salt=base64.b64encode(current_salt).decode("ascii"),
        digest=base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = password_hash.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    salt = base64.b64decode(raw_salt.encode("ascii"))
    expected = base64.b64decode(raw_digest.encode("ascii"))
    computed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(raw_iterations))
    return hmac.compare_digest(computed, expected)


def any_users_exist() -> bool:
    return any(user.enabled for user in load_users())


def authenticate_basic_header(header_value: str | None) -> HarborUser:
    if not header_value or not header_value.startswith("Basic "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Basic"},
        )
    try:
        decoded = base64.b64decode(header_value.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication data.") from exc
    user = find_user(username)
    if user is None or not user.enabled or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login failed.")
    return user


def current_user(request: Request) -> HarborUser:
    if not any_users_exist():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Harbor is locked until an initial admin is created via CLI.",
        )
    client_key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _AUTH_LOCK:
        failures = _AUTH_FAILURES[client_key]
        while failures and now - failures[0] > AUTH_WINDOW_SECONDS:
            failures.popleft()
        if len(failures) >= AUTH_MAX_FAILURES:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts.",
            )
    try:
        user = authenticate_basic_header(request.headers.get("Authorization"))
    except HTTPException:
        with _AUTH_LOCK:
            _AUTH_FAILURES[client_key].append(now)
        raise
    with _AUTH_LOCK:
        _AUTH_FAILURES.pop(client_key, None)
    return user


def require_role(min_role: UserRole):
    def dependency(request: Request) -> HarborUser:
        user = current_user(request)
        if ROLE_LEVEL[user.role] < ROLE_LEVEL[min_role]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role.")
        return user

    return Depends(dependency)


def require_metrics_access(request: Request) -> HarborUser | None:
    configured_token = os.getenv("HARBOR_METRICS_TOKEN", "").strip()
    authorization = request.headers.get("Authorization", "")
    if configured_token and authorization.startswith("Bearer "):
        supplied_token = authorization.removeprefix("Bearer ").strip()
        if hmac.compare_digest(configured_token, supplied_token):
            return None
    user = current_user(request)
    if ROLE_LEVEL[user.role] < ROLE_LEVEL["admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role.")
    return user
