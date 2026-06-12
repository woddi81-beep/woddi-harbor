from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

from .config import CONFIG_DIR, DATA_DIR, RUNTIME_DIR, ModuleConfig, ModuleSource, load_modules, resolve_path, save_modules
from .modules import execute_module

SourceKind = Literal["local", "git"]
SOURCES_CONFIG_PATH = CONFIG_DIR / "sources.json"
SOURCES_LOCAL_CONFIG_PATH = CONFIG_DIR / "sources.local.json"
SOURCES_RUNTIME_DIR = RUNTIME_DIR / "sources"
SOURCES_DATA_DIR = DATA_DIR / "sources"
SOURCE_LOCK_DIR = RUNTIME_DIR / "locks"


@dataclass
class ManagedSource:
    id: str
    kind: SourceKind
    enabled: bool = True
    module_id: str = ""
    source_path: str = ""
    repository: str = ""
    branch: str = "main"
    target_path: str = ""
    include_extensions: list[str] | None = None

    def target(self) -> Path:
        return resolve_path(self.target_path or str(SOURCES_DATA_DIR / self.id))


def ensure_sources_config() -> None:
    if SOURCES_CONFIG_PATH.exists():
        return
    SOURCES_CONFIG_PATH.write_text('{\n  "sources": []\n}\n', encoding="utf-8")


def sources_config_path(*, for_write: bool = False) -> Path:
    if for_write or SOURCES_LOCAL_CONFIG_PATH.exists():
        return SOURCES_LOCAL_CONFIG_PATH
    return SOURCES_CONFIG_PATH


def load_sources() -> list[ManagedSource]:
    ensure_sources_config()
    payload = json.loads(sources_config_path().read_text(encoding="utf-8"))
    sources: list[ManagedSource] = []
    for raw in payload.get("sources", []):
        sources.append(
            ManagedSource(
                id=str(raw["id"]),
                kind=cast(SourceKind, str(raw["kind"])),
                enabled=bool(raw.get("enabled", True)),
                module_id=str(raw.get("module_id", "")),
                source_path=str(raw.get("source_path", "")),
                repository=str(raw.get("repository", "")),
                branch=str(raw.get("branch", "main")),
                target_path=str(raw.get("target_path", "")),
                include_extensions=[str(item).lower() for item in raw.get("include_extensions", [])] or None,
            )
        )
    return sources


def save_sources(sources: list[ManagedSource]) -> Path:
    path = sources_config_path(for_write=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"sources": [asdict(source) for source in sources]}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)
    return path


def configure_document_sources(operations_path: str, customer_path: str) -> dict[str, Any]:
    operations = resolve_path(operations_path)
    customer = resolve_path(customer_path)
    missing = [str(path) for path in (operations, customer) if not path.is_dir()]
    if missing:
        raise ValueError(f"Dokumentverzeichnis nicht gefunden: {', '.join(missing)}")

    extensions = [".md", ".markdown"]
    sources = [
        ManagedSource(
            id="operation-docs",
            kind="local",
            module_id="10",
            source_path=str(operations),
            target_path="data/sources/documentation-operation",
            include_extensions=extensions,
        ),
        ManagedSource(
            id="customer-docs",
            kind="local",
            module_id="11",
            source_path=str(customer),
            target_path="data/sources/documentation-customer",
            include_extensions=extensions,
        ),
    ]
    source_config = save_sources(sources)

    modules_by_id = {module.id: module for module in load_modules()}
    definitions = (
        ("10", "Operations-Dokumentation", "data/sources/documentation-operation"),
        ("11", "Kunden-Dokumentation", "data/sources/documentation-customer"),
    )
    for module_id, name, path in definitions:
        module = modules_by_id.get(module_id)
        if module is None:
            module = ModuleConfig(id=module_id, type="docs")
            modules_by_id[module_id] = module
        module.enabled = True
        module.name = name
        module.transport = "local"
        module.path = path
        module.sources = [ModuleSource(id=f"{module_id}-source-1", path=path, label=name)]
    save_modules(sorted(modules_by_id.values(), key=lambda item: item.id))
    return {
        "ok": True,
        "source_config": str(source_config),
        "operations_path": str(operations),
        "customer_path": str(customer),
        "modules": ["10", "11"],
    }


def find_source(source_id: str) -> ManagedSource | None:
    return next((source for source in load_sources() if source.id == source_id), None)


@contextmanager
def _source_lock(source_id: str):
    SOURCE_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = SOURCE_LOCK_DIR / f"source-{source_id}.lock"
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_source(source: ManagedSource) -> None:
    if not source.id or not all(character.isalnum() or character in "._-" for character in source.id):
        raise ValueError("Quellen-ID enthaelt ungueltige Zeichen.")
    if source.kind not in {"local", "git"}:
        raise ValueError("Quellentyp muss local oder git sein.")
    if source.kind == "local" and not source.source_path:
        raise ValueError("Lokale Quelle braucht source_path.")
    if source.kind == "git" and not source.repository:
        raise ValueError("Git-Quelle braucht repository.")


def _copy_local(source: ManagedSource, staging: Path) -> None:
    origin = resolve_path(source.source_path)
    if not origin.is_dir():
        raise ValueError(f"Quellverzeichnis nicht gefunden: {origin}")
    shutil.copytree(origin, staging, dirs_exist_ok=True, symlinks=False)
    _filter_staging(staging, source.include_extensions)


def _clone_git(source: ManagedSource, staging: Path) -> None:
    command = [
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        source.branch,
        "--single-branch",
        source.repository,
        str(staging),
    ]
    completed = subprocess.run(command, check=False, text=True, capture_output=True, timeout=300)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Git-Import fehlgeschlagen.")
    shutil.rmtree(staging / ".git", ignore_errors=True)
    _filter_staging(staging, source.include_extensions)


def _filter_staging(staging: Path, include_extensions: list[str] | None) -> None:
    if not include_extensions:
        return
    extensions = {item.lower() if item.startswith(".") else f".{item.lower()}" for item in include_extensions}
    for item in sorted(staging.rglob("*"), reverse=True):
        if item.is_symlink() or (item.is_file() and item.suffix.lower() not in extensions):
            item.unlink()
        elif item.is_dir() and not any(item.iterdir()):
            item.rmdir()


def source_quality(path: Path, include_extensions: list[str] | None = None) -> dict[str, Any]:
    extensions = {item if item.startswith(".") else f".{item}" for item in include_extensions or []}
    files = [
        item
        for item in path.rglob("*")
        if item.is_file() and not item.is_symlink() and (not extensions or item.suffix.lower() in extensions)
    ]
    total_bytes = 0
    empty_files = 0
    hashes: dict[str, int] = {}
    for item in files:
        content = item.read_bytes()
        total_bytes += len(content)
        if not content.strip():
            empty_files += 1
        digest = hashlib.sha256(content).hexdigest()
        hashes[digest] = hashes.get(digest, 0) + 1
    duplicate_files = sum(count - 1 for count in hashes.values() if count > 1)
    return {
        "files": len(files),
        "bytes": total_bytes,
        "empty_files": empty_files,
        "duplicate_files": duplicate_files,
        "healthy": bool(files and total_bytes >= 100),
    }


def _write_manifest(source: ManagedSource, quality: dict[str, Any], *, changed: bool) -> Path:
    SOURCES_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    path = SOURCES_RUNTIME_DIR / f"{source.id}.json"
    payload = {
        "source": asdict(source),
        "target": str(source.target()),
        "quality": quality,
        "changed": changed,
        "synced_at": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()


def sync_source(source_id: str, *, reindex: bool = True) -> dict[str, Any]:
    source = find_source(source_id)
    if source is None:
        raise ValueError(f"Quelle nicht gefunden: {source_id}")
    _validate_source(source)
    target = source.target()
    with _source_lock(source.id):
        if source.kind == "local" and resolve_path(source.source_path) == target:
            quality = source_quality(target, source.include_extensions)
            manifest = _write_manifest(source, quality, changed=False)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=f"harbor-source-{source.id}-", dir=target.parent) as temporary:
                staging = Path(temporary) / "content"
                if source.kind == "local":
                    _copy_local(source, staging)
                else:
                    _clone_git(source, staging)
                quality = source_quality(staging, source.include_extensions)
                if not quality["healthy"]:
                    raise ValueError(f"Quelle {source.id} besteht die Mindestqualitaet nicht: {quality}")
                changed = not target.exists() or _tree_digest(staging) != _tree_digest(target)
                if changed:
                    previous = target.with_name(f".{target.name}.previous")
                    shutil.rmtree(previous, ignore_errors=True)
                    if target.exists():
                        os.replace(target, previous)
                    os.replace(staging, target)
                    shutil.rmtree(previous, ignore_errors=True)
                manifest = _write_manifest(source, quality, changed=changed)
        reindex_result: dict[str, Any] | None = None
        if reindex and source.module_id:
            reindex_result = execute_module(source.module_id, "reindex", {})
        return {
            "ok": True,
            "source_id": source.id,
            "target": str(target),
            "quality": quality,
            "manifest": str(manifest),
            "reindex": reindex_result,
        }


def source_status(source: ManagedSource) -> dict[str, Any]:
    target = source.target()
    manifest_path = SOURCES_RUNTIME_DIR / f"{source.id}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    return {
        "id": source.id,
        "kind": source.kind,
        "enabled": source.enabled,
        "module_id": source.module_id,
        "target": str(target),
        "exists": target.is_dir(),
        "quality": source_quality(target, source.include_extensions) if target.is_dir() else None,
        "last_sync": manifest,
    }


def source_overview() -> list[dict[str, Any]]:
    return [source_status(source) for source in load_sources()]
