#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def hardware_inventory() -> dict[str, int | str | bool]:
    completed = subprocess.run(["lscpu", "-J"], check=True, text=True, capture_output=True)
    fields = {
        str(item["field"]).rstrip(":"): str(item.get("data", ""))
        for item in json.loads(completed.stdout).get("lscpu", [])
    }
    page_size = os.sysconf("SC_PAGE_SIZE")
    page_count = os.sysconf("SC_PHYS_PAGES")
    return {
        "architecture": fields.get("Architecture", ""),
        "model": fields.get("Model name", ""),
        "logical_cpus": int(fields.get("CPU(s)", "0")),
        "sockets": int(fields.get("Socket(s)", "0")),
        "numa_nodes": int(fields.get("NUMA node(s)", "0")),
        "memory_bytes": int(page_size * page_count),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the Harbor production host capacity.")
    parser.add_argument("--min-memory-gib", type=int, default=128)
    parser.add_argument("--min-sockets", type=int, default=4)
    args = parser.parse_args()

    inventory = hardware_inventory()
    required_memory = args.min_memory_gib * 1024**3
    checks = {
        "memory": int(inventory["memory_bytes"]) >= required_memory,
        "sockets": int(inventory["sockets"]) >= args.min_sockets,
    }
    payload = {
        "ok": all(checks.values()),
        "requirements": {
            "memory_gib": args.min_memory_gib,
            "sockets": args.min_sockets,
        },
        "checks": checks,
        "inventory": {
            **inventory,
            "memory_gib": round(int(inventory["memory_bytes"]) / 1024**3, 2),
        },
    }
    print(json.dumps(payload, indent=2))
    if not payload["ok"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
