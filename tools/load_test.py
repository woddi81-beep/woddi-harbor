#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import statistics
import sys
import time
import urllib.request


def request(url: str, authorization: str, timeout: float) -> tuple[bool, float]:
    started = time.perf_counter()
    headers = {"Authorization": authorization} if authorization else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            response.read()
            return 200 <= response.status < 300, time.perf_counter() - started
    except Exception:
        return False, time.perf_counter() - started


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:9680/api/health")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--max-p95-ms", type=float, default=500.0)
    args = parser.parse_args()
    authorization = ""
    if args.username:
        token = base64.b64encode(f"{args.username}:{args.password}".encode()).decode()
        authorization = f"Basic {token}"
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        results = list(
            executor.map(
                lambda _: request(args.url, authorization, args.timeout),
                range(args.requests),
            )
        )
    elapsed = time.perf_counter() - started
    latencies = [duration for _ok, duration in results]
    successes = sum(1 for ok, _duration in results if ok)
    error_rate = (args.requests - successes) / args.requests
    p95_ms = percentile(latencies, 0.95) * 1000
    passed = error_rate <= args.max_error_rate and p95_ms <= args.max_p95_ms
    print(
        json.dumps(
            {
                "ok": passed,
                "requests": args.requests,
                "successes": successes,
                "errors": args.requests - successes,
                "elapsed_seconds": round(elapsed, 3),
                "requests_per_second": round(args.requests / elapsed, 2),
                "latency_ms": {
                    "mean": round(statistics.mean(latencies) * 1000, 2),
                    "p50": round(percentile(latencies, 0.50) * 1000, 2),
                    "p95": round(p95_ms, 2),
                    "p99": round(percentile(latencies, 0.99) * 1000, 2),
                },
            },
            indent=2,
        )
    )
    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
