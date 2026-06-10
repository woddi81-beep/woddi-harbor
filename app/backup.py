from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

from .config import CONFIG_DIR, DATA_DIR
from .state import DATABASE_PATH, initialize_database, record_audit

BACKUP_DIR = DATA_DIR / "backups"


def list_backups() -> list[dict[str, object]]:
    if not BACKUP_DIR.exists():
        return []
    return [
        {
            "name": path.name,
            "path": str(path),
            "bytes": path.stat().st_size,
            "modified_at": path.stat().st_mtime,
        }
        for path in sorted(BACKUP_DIR.glob("harbor-*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True)
    ]


def create_backup(label: str = "manual") -> Path:
    initialize_database()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    target = BACKUP_DIR / f"harbor-{timestamp}-{label}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="harbor-backup-") as temporary:
        staging = Path(temporary)
        shutil.copytree(CONFIG_DIR, staging / "config")
        database_copy = staging / "harbor.db"
        with sqlite3.connect(DATABASE_PATH) as source, sqlite3.connect(database_copy) as destination:
            source.backup(destination)
        manifest = {
            "format": 1,
            "created_at": time.time(),
            "database": "harbor.db",
            "config": "config",
        }
        (staging / "backup.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        with tarfile.open(target, "w:gz") as archive:
            archive.add(staging / "backup.json", arcname="backup.json")
            archive.add(staging / "harbor.db", arcname="harbor.db")
            archive.add(staging / "config", arcname="config")
    target.chmod(0o600)
    record_audit("backup.create", target.name, actor="cli")
    return target


def restore_backup(source: str) -> Path:
    archive_path = Path(source).expanduser().resolve()
    if not archive_path.is_file():
        raise ValueError("Backup-Datei nicht gefunden.")
    safety_backup = create_backup("pre-restore")
    with tempfile.TemporaryDirectory(prefix="harbor-restore-") as temporary:
        staging = Path(temporary)
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                destination = (staging / member.name).resolve()
                if staging.resolve() not in destination.parents and destination != staging.resolve():
                    raise ValueError("Backup enthaelt unzulaessige Pfade.")
            archive.extractall(staging, filter="data")
        manifest = json.loads((staging / "backup.json").read_text(encoding="utf-8"))
        if manifest.get("format") != 1:
            raise ValueError("Nicht unterstuetztes Backup-Format.")
        shutil.copytree(staging / "config", CONFIG_DIR, dirs_exist_ok=True)
        shutil.copy2(staging / "harbor.db", DATABASE_PATH)
        DATABASE_PATH.chmod(0o600)
    return safety_backup
