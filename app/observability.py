from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any


_LOCK = threading.Lock()
_REQUESTS: dict[tuple[str, str, int], int] = defaultdict(int)
_DURATION_SUM: dict[tuple[str, str], float] = defaultdict(float)
_IN_FLIGHT = 0


def request_started() -> None:
    global _IN_FLIGHT
    with _LOCK:
        _IN_FLIGHT += 1


def request_finished(method: str, path: str, status_code: int, duration: float) -> None:
    global _IN_FLIGHT
    route = _normalize_path(path)
    with _LOCK:
        _IN_FLIGHT = max(0, _IN_FLIGHT - 1)
        _REQUESTS[(method, route, status_code)] += 1
        _DURATION_SUM[(method, route)] += duration


def _normalize_path(path: str) -> str:
    parts = path.strip("/").split("/")
    normalized = ["{id}" if len(part) > 24 or part.isdigit() else part for part in parts]
    return "/" + "/".join(normalized) if normalized and normalized[0] else "/"


def prometheus_metrics() -> str:
    lines = [
        "# HELP harbor_http_requests_total HTTP requests handled.",
        "# TYPE harbor_http_requests_total counter",
    ]
    with _LOCK:
        for (method, route, status), count in sorted(_REQUESTS.items()):
            lines.append(
                f'harbor_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}'
            )
        lines.extend(
            [
                "# HELP harbor_http_request_duration_seconds_sum Total request duration.",
                "# TYPE harbor_http_request_duration_seconds_sum counter",
            ]
        )
        for (method, route), duration in sorted(_DURATION_SUM.items()):
            lines.append(
                f'harbor_http_request_duration_seconds_sum{{method="{method}",route="{route}"}} {duration:.6f}'
            )
        lines.extend(
            [
                "# HELP harbor_http_requests_in_flight Requests currently executing.",
                "# TYPE harbor_http_requests_in_flight gauge",
                f"harbor_http_requests_in_flight {_IN_FLIGHT}",
            ]
        )
    return "\n".join(lines) + "\n"
