from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import RUNTIME_DIR

FIELD_CACHE_DIR = RUNTIME_DIR / "field_cache"


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _cache_path(module_id: str) -> Path:
    safe_id = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in module_id)
    return FIELD_CACHE_DIR / f"{safe_id}.json"


def _normalize_field(entry: Any, *, source: str) -> dict[str, Any] | None:
    if isinstance(entry, str):
        path = entry.strip()
        return {"path": path, "source": source} if path else None
    if not isinstance(entry, dict):
        return None
    path = str(entry.get("path") or entry.get("name") or "").strip()
    if not path:
        return None
    normalized = {
        "path": path,
        "source": str(entry.get("source") or source),
    }
    for key in ("type", "required", "description", "enum"):
        if key in entry and entry[key] not in (None, ""):
            normalized[key] = entry[key]
    return normalized


def _merge_fields(*field_lists: list[Any]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for fields in field_lists:
        for raw in fields:
            source = "observed" if isinstance(raw, str) else str(raw.get("source", "observed")) if isinstance(raw, dict) else "observed"
            item = _normalize_field(raw, source=source)
            if not item:
                continue
            path = item["path"]
            existing = merged.get(path, {})
            existing.update({key: value for key, value in item.items() if value not in (None, "")})
            merged[path] = existing
    return [merged[path] for path in sorted(merged)]


def load_field_catalog(module_id: str) -> dict[str, Any]:
    path = _cache_path(module_id)
    if not path.exists():
        return {
            "ok": True,
            "module_id": module_id,
            "updated_at": "",
            "service": "",
            "resource_count": 0,
            "resources": {},
            "errors": [],
            "cache_path": str(path),
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "ok": False,
            "module_id": module_id,
            "updated_at": "",
            "service": "",
            "resource_count": 0,
            "resources": {},
            "errors": [f"Could not read field cache: {exc}"],
            "cache_path": str(path),
        }
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("ok", True)
    payload.setdefault("module_id", module_id)
    payload.setdefault("updated_at", "")
    payload.setdefault("service", "")
    payload.setdefault("resources", {})
    payload.setdefault("errors", [])
    payload["resource_count"] = len(payload.get("resources", {})) if isinstance(payload.get("resources"), dict) else 0
    payload["cache_path"] = str(path)
    return payload


def save_field_catalog(
    module_id: str,
    service: str,
    resources: dict[str, Any],
    *,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    FIELD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    normalized_resources: dict[str, Any] = {}
    for name, raw in sorted(resources.items()):
        if not isinstance(raw, dict):
            continue
        fields = _merge_fields(
            raw.get("fields") or [],
            raw.get("schema_fields") or [],
            raw.get("observed_fields") or [],
        )
        normalized_resources[name] = {
            "name": name,
            "service": service,
            "endpoint": raw.get("endpoint"),
            "tool": raw.get("tool"),
            "available": bool(raw.get("available", True)),
            "has_objects": bool(raw.get("has_objects", False)),
            "field_count": len(fields),
            "fields": fields,
            "filters": raw.get("filters", []),
            "sample_available": bool(raw.get("sample_available", False)),
            "error": raw.get("error") or None,
            "updated_at": _timestamp(),
        }
    payload = {
        "ok": not bool(errors),
        "module_id": module_id,
        "service": service,
        "updated_at": _timestamp(),
        "resource_count": len(normalized_resources),
        "resources": normalized_resources,
        "errors": errors or [],
    }
    path = _cache_path(module_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return load_field_catalog(module_id)


def update_catalog_from_tool_result(module_id: str, service: str, tool_name: str, result: dict[str, Any]) -> dict[str, Any] | None:
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    data = structured.get("data") if isinstance(structured, dict) else None
    if not isinstance(data, dict):
        return None

    existing = load_field_catalog(module_id)
    resources = dict(existing.get("resources", {})) if isinstance(existing.get("resources"), dict) else {}
    errors = list(existing.get("errors", [])) if isinstance(existing.get("errors"), list) else []

    if service == "openstack" and tool_name == "discover_resources":
        for item in data.get("resources", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("resource", "")).strip()
            if not name:
                continue
            resources[name] = {
                "tool": item.get("tool"),
                "available": item.get("available", False),
                "has_objects": item.get("has_objects", False),
                "fields": [{"path": field, "source": "observed"} for field in item.get("observed_fields", []) if isinstance(field, str)],
                "sample_available": bool(item.get("sample")),
                "error": item.get("error"),
            }
        return save_field_catalog(module_id, service, resources, errors=errors)

    if service == "netbox" and tool_name == "describe_object_type":
        name = str(data.get("object_type", "")).strip()
        if not name:
            return None
        resources[name] = {
            "endpoint": data.get("endpoint"),
            "available": not bool(data.get("sample_error")),
            "has_objects": bool(data.get("observed_field_count")),
            "schema_fields": [
                {**field, "source": "schema"}
                for field in data.get("schema_fields", [])
                if isinstance(field, dict)
            ],
            "observed_fields": [
                {**field, "source": "observed"}
                for field in data.get("observed_fields", [])
                if isinstance(field, dict)
            ],
            "filters": data.get("filter_parameters", []),
            "sample_available": bool(data.get("sample")),
            "error": data.get("sample_error"),
        }
        return save_field_catalog(module_id, service, resources, errors=errors)

    if service == "netbox" and tool_name == "discover_object_types":
        for item in data.get("object_types", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("object_type", "")).strip()
            if not name:
                continue
            resources.setdefault(
                name,
                {
                    "endpoint": item.get("endpoint"),
                    "available": True,
                    "fields": [],
                    "filters": [],
                    "sample_available": False,
                },
            )
        return save_field_catalog(module_id, service, resources, errors=errors)

    return None
