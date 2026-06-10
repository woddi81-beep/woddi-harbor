from __future__ import annotations

import threading
from collections import defaultdict

_LOCK = threading.Lock()
_REQUESTS: dict[tuple[str, str, int], int] = defaultdict(int)
_DURATION_SUM: dict[tuple[str, str], float] = defaultdict(float)
_DURATION_BUCKETS: dict[tuple[str, str, float], int] = defaultdict(int)
_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
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
        for boundary in _BUCKETS:
            if duration <= boundary:
                _DURATION_BUCKETS[(method, route, boundary)] += 1


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
                "# HELP harbor_http_request_duration_seconds HTTP request duration.",
                "# TYPE harbor_http_request_duration_seconds histogram",
            ]
        )
        route_counts: dict[tuple[str, str], int] = defaultdict(int)
        for (method, route, _status), count in _REQUESTS.items():
            route_counts[(method, route)] += count
        for (method, route, boundary), count in sorted(_DURATION_BUCKETS.items()):
            lines.append(
                f'harbor_http_request_duration_seconds_bucket{{method="{method}",route="{route}",le="{boundary}"}} {count}'
            )
        for (method, route), count in sorted(route_counts.items()):
            lines.append(
                f'harbor_http_request_duration_seconds_bucket{{method="{method}",route="{route}",le="+Inf"}} {count}'
            )
            lines.append(
                f'harbor_http_request_duration_seconds_count{{method="{method}",route="{route}"}} {count}'
            )
        lines.extend(
            [
                "# HELP harbor_http_requests_in_progress Requests currently executing.",
                "# TYPE harbor_http_requests_in_progress gauge",
                f"harbor_http_requests_in_progress {_IN_FLIGHT}",
            ]
        )
    return "\n".join(lines) + "\n"
