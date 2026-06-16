from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import RUNTIME_DIR

DATABASE_PATH = RUNTIME_DIR / "harbor.db"
_INIT_LOCK = threading.Lock()
SCHEMA_VERSION = 2


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    return connection


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        if connection.in_transaction:
            connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


@contextmanager
def read_connection() -> Iterator[sqlite3.Connection]:
    connection = _connect()
    try:
        yield connection
    finally:
        connection.close()


def initialize_database() -> Path:
    with _INIT_LOCK:
        connection = _connect()
        try:
            connection.executescript(
                """
                                CREATE TABLE IF NOT EXISTS stellen (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    department TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'offen',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_stellen_status ON stellen(status);
CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL,
                    applied_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config_documents (
                    name TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    occurred_at REAL NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_occurred_at
                    ON audit_events(occurred_at DESC);
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_messages_session
                    ON chat_messages(session_id, created_at);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS mcp_packages (
                    id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    installed_at REAL NOT NULL,
                    PRIMARY KEY(id, version)
                );
                CREATE TABLE IF NOT EXISTS mcp_instances (
                    id TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL,
                    package_version TEXT NOT NULL,
                    driver TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    desired_state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(package_id, package_version)
                        REFERENCES mcp_packages(id, version)
                );
                CREATE TABLE IF NOT EXISTS mcp_deployment_history (
                    id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    package_id TEXT NOT NULL,
                    from_version TEXT NOT NULL,
                    to_version TEXT NOT NULL,
                    changed_at REAL NOT NULL
                );
                """
            )
            row = connection.execute("SELECT MAX(version) AS version FROM schema_meta").fetchone()
            if row is None or row["version"] is None:
                connection.execute(
                    "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                    (1, time.time()),
                )
                current_version = 1
            else:
                current_version = int(row["version"])
            if current_version < 2:
                columns = {item["name"] for item in connection.execute("PRAGMA table_info(jobs)").fetchall()}
                if "attempts" not in columns:
                    connection.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
                if "worker_id" not in columns:
                    connection.execute("ALTER TABLE jobs ADD COLUMN worker_id TEXT NOT NULL DEFAULT ''")
                connection.execute(
                    "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                    (2, time.time()),
                )
        finally:
            connection.close()
    DATABASE_PATH.chmod(0o600)
    return DATABASE_PATH


def snapshot_config(name: str, payload: dict[str, Any]) -> None:
    initialize_database()
    now = time.time()
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with transaction() as connection:
        connection.execute(
            """
            INSERT INTO config_documents(name, payload_json, revision, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(name) DO UPDATE SET
                payload_json=excluded.payload_json,
                revision=config_documents.revision + 1,
                updated_at=excluded.updated_at
            """,
            (name, serialized, now),
        )


def record_audit(
    action: str,
    target: str,
    *,
    actor: str = "system",
    outcome: str = "success",
    detail: dict[str, Any] | None = None,
) -> str:
    initialize_database()
    event_id = str(uuid4())
    with transaction() as connection:
        connection.execute(
            """
            INSERT INTO audit_events(id, occurred_at, actor, action, target, outcome, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                time.time(),
                actor,
                action,
                target,
                outcome,
                json.dumps(detail or {}, ensure_ascii=False),
            ),
        )
    return event_id


def list_audit_events(limit: int = 100) -> list[dict[str, Any]]:
    initialize_database()
    with read_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM audit_events ORDER BY occurred_at DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
    return [
        {
            **dict(row),
            "detail": json.loads(row["detail_json"]),
        }
        for row in rows
    ]


def create_chat_session(owner: str, title: str = "") -> str:
    initialize_database()
    session_id = str(uuid4())
    now = time.time()
    with transaction() as connection:
        connection.execute(
            "INSERT INTO chat_sessions(id, owner, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, owner, title[:200], now, now),
        )
    return session_id


def append_chat_message(
    session_id: str,
    role: str,
    content: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> str:
    initialize_database()
    message_id = str(uuid4())
    now = time.time()
    with transaction() as connection:
        connection.execute(
            """
            INSERT INTO chat_messages(id, session_id, role, content, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, session_id, role, content, json.dumps(metadata or {}, ensure_ascii=False), now),
        )
        connection.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (now, session_id))
    return message_id


def load_chat_messages(session_id: str, owner: str, limit: int = 30) -> list[dict[str, str]]:
    initialize_database()
    with read_connection() as connection:
        session = connection.execute(
            "SELECT id FROM chat_sessions WHERE id=? AND owner=?",
            (session_id, owner),
        ).fetchone()
        if session is None:
            return []
        rows = connection.execute(
            """
            SELECT role, content FROM chat_messages
            WHERE session_id=? ORDER BY created_at DESC LIMIT ?
            """,
            (session_id, max(1, min(limit, 200))),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def list_chat_sessions(owner: str, limit: int = 50) -> list[dict[str, Any]]:
    initialize_database()
    with read_connection() as connection:
        rows = connection.execute(
            """
            SELECT s.id, s.title, s.created_at, s.updated_at, COUNT(m.id) AS message_count
            FROM chat_sessions s
            LEFT JOIN chat_messages m ON m.session_id=s.id
            WHERE s.owner=?
            GROUP BY s.id
            ORDER BY s.updated_at DESC
            LIMIT ?
            """,
            (owner, max(1, min(limit, 200))),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_chat_session(session_id: str, owner: str) -> bool:
    initialize_database()
    with transaction() as connection:
        cursor = connection.execute(
            "DELETE FROM chat_sessions WHERE id=? AND owner=?",
            (session_id, owner),
        )
    return cursor.rowcount > 0


def upsert_mcp_package(package_id: str, version: str, manifest: dict[str, Any]) -> None:
    initialize_database()
    with transaction() as connection:
        connection.execute(
            """
            INSERT INTO mcp_packages(id, version, manifest_json, installed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id, version) DO UPDATE SET
                manifest_json=excluded.manifest_json,
                installed_at=excluded.installed_at
            """,
            (package_id, version, json.dumps(manifest, ensure_ascii=False), time.time()),
        )


def list_mcp_packages() -> list[dict[str, Any]]:
    initialize_database()
    with read_connection() as connection:
        rows = connection.execute(
            "SELECT id, version, manifest_json, installed_at FROM mcp_packages ORDER BY id, version"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "version": row["version"],
            "manifest": json.loads(row["manifest_json"]),
            "installed_at": row["installed_at"],
        }
        for row in rows
    ]


def find_mcp_package(package_id: str, version: str) -> dict[str, Any] | None:
    initialize_database()
    with read_connection() as connection:
        row = connection.execute(
            "SELECT manifest_json FROM mcp_packages WHERE id=? AND version=?",
            (package_id, version),
        ).fetchone()
    return json.loads(row["manifest_json"]) if row else None


def upsert_mcp_instance(
    instance_id: str,
    package_id: str,
    package_version: str,
    driver: str,
    config: dict[str, Any],
    desired_state: str = "stopped",
) -> None:
    initialize_database()
    now = time.time()
    with transaction() as connection:
        connection.execute(
            """
            INSERT INTO mcp_instances(
                id, package_id, package_version, driver, config_json,
                desired_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                package_id=excluded.package_id,
                package_version=excluded.package_version,
                driver=excluded.driver,
                config_json=excluded.config_json,
                desired_state=excluded.desired_state,
                updated_at=excluded.updated_at
            """,
            (
                instance_id,
                package_id,
                package_version,
                driver,
                json.dumps(config, ensure_ascii=False),
                desired_state,
                now,
                now,
            ),
        )


def set_mcp_instance_state(instance_id: str, desired_state: str) -> None:
    initialize_database()
    with transaction() as connection:
        cursor = connection.execute(
            "UPDATE mcp_instances SET desired_state=?, updated_at=? WHERE id=?",
            (desired_state, time.time(), instance_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"MCP-Instanz nicht gefunden: {instance_id}")


def list_mcp_instances() -> list[dict[str, Any]]:
    initialize_database()
    with read_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM mcp_instances ORDER BY id"
        ).fetchall()
    return [
        {
            **dict(row),
            "config": json.loads(row["config_json"]),
        }
        for row in rows
    ]


def find_mcp_instance(instance_id: str) -> dict[str, Any] | None:
    return next((item for item in list_mcp_instances() if item["id"] == instance_id), None)


def change_mcp_instance_version(instance_id: str, version: str) -> None:
    initialize_database()
    with transaction() as connection:
        row = connection.execute(
            "SELECT package_id, package_version FROM mcp_instances WHERE id=?",
            (instance_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"MCP-Instanz nicht gefunden: {instance_id}")
        package = connection.execute(
            "SELECT 1 FROM mcp_packages WHERE id=? AND version=?",
            (row["package_id"], version),
        ).fetchone()
        if package is None:
            raise ValueError(f"MCP-Paketversion nicht installiert: {row['package_id']}@{version}")
        connection.execute(
            """
            INSERT INTO mcp_deployment_history(
                id, instance_id, package_id, from_version, to_version, changed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid4()), instance_id, row["package_id"], row["package_version"], version, time.time()),
        )
        connection.execute(
            "UPDATE mcp_instances SET package_version=?, updated_at=? WHERE id=?",
            (version, time.time(), instance_id),
        )


def previous_mcp_instance_version(instance_id: str) -> str | None:
    initialize_database()
    with read_connection() as connection:
        row = connection.execute(
            """
            SELECT from_version FROM mcp_deployment_history
            WHERE instance_id=? ORDER BY changed_at DESC LIMIT 1
            """,
            (instance_id,),
        ).fetchone()
    return str(row["from_version"]) if row else None


def create_job(kind: str, target: str, payload: dict[str, Any]) -> str:
    initialize_database()
    job_id = str(uuid4())
    with transaction() as connection:
        connection.execute(
            """
            INSERT INTO jobs(id, kind, target, status, payload_json, result_json, error, created_at)
            VALUES (?, ?, ?, 'queued', ?, '{}', '', ?)
            """,
            (job_id, kind, target, json.dumps(payload, ensure_ascii=False), time.time()),
        )
    return job_id


def update_job(
    job_id: str,
    status: str,
    *,
    result: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    initialize_database()
    now = time.time()
    started_at = now if status == "running" else None
    completed_at = now if status in {"completed", "failed"} else None
    with transaction() as connection:
        connection.execute(
            """
            UPDATE jobs SET status=?, result_json=?, error=?,
                started_at=COALESCE(started_at, ?),
                completed_at=COALESCE(?, completed_at)
            WHERE id=?
            """,
            (
                status,
                json.dumps(result or {}, ensure_ascii=False),
                error,
                started_at,
                completed_at,
                job_id,
            ),
        )


def claim_next_job(worker_id: str, *, stale_after_seconds: float = 900.0) -> dict[str, Any] | None:
    initialize_database()
    now = time.time()
    with transaction() as connection:
        connection.execute(
            """
            UPDATE jobs SET status='queued', worker_id='', started_at=NULL
            WHERE status='running' AND started_at < ?
            """,
            (now - stale_after_seconds,),
        )
        row = connection.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        updated = connection.execute(
            """
            UPDATE jobs SET status='running', started_at=?, worker_id=?, attempts=attempts + 1
            WHERE id=? AND status='queued'
            """,
            (now, worker_id, row["id"]),
        )
        if updated.rowcount != 1:
            return None
        claimed = connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
    return {
        **dict(claimed),
        "payload": json.loads(claimed["payload_json"]),
        "result": json.loads(claimed["result_json"]),
    }


def list_jobs(limit: int = 100) -> list[dict[str, Any]]:
    initialize_database()
    with read_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
    return [
        {
            **dict(row),
            "payload": json.loads(row["payload_json"]),
            "result": json.loads(row["result_json"]),
        }
        for row in rows
    ]

def list_stellen() -> list[dict[str, Any]]:
    initialize_database()
    with read_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM stellen ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_stellen(id: str) -> dict[str, Any] | None:
    initialize_database()
    with read_connection() as connection:
        row = connection.execute("SELECT * FROM stellen WHERE id=?", (id,)).fetchone()
    return dict(row) if row else None


def create_stellen(data: dict[str, Any]) -> str:
    initialize_database()
    import uuid
    now = time.time()
    id = data.get("id") or str(uuid.uuid4())[:8]
    connection = _connect()
    try:
        connection.execute(
            "INSERT INTO stellen (id, title, department, description, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, data["title"], data.get("department", ""), data.get("description", ""), data.get("status", "offen"), now, now)
        )
        connection.commit()
    finally:
        connection.close()
    return id


def update_stellen(id: str, data: dict[str, Any]) -> bool:
    initialize_database()
    now = time.time()
    connection = _connect()
    try:
        connection.execute(
            "UPDATE stellen SET title=?, department=?, description=?, status=?, updated_at=? WHERE id=?",
            (data["title"], data.get("department", ""), data.get("description", ""), data.get("status", "offen"), now, id)
        )
        connection.commit()
        return connection.total_changes > 0
    finally:
        connection.close()


def delete_stellen(id: str) -> bool:
    initialize_database()
    connection = _connect()
    try:
        connection.execute("DELETE FROM stellen WHERE id=?", (id,))
        connection.commit()
        return connection.total_changes > 0
    finally:
        connection.close()


def seed_stellen() -> None:
    initialize_database()
    with read_connection() as connection:
        existing = connection.execute("SELECT COUNT(*) FROM stellen").fetchone()
    if existing and existing[0] > 0:
        return
    now = time.time()
    items = [
        {"id": "stellen-1", "title": "System Engineer (m/w/d) – Linux & Infrastructure", "department": "IT-Infrastruktur", "description": "Betreuung und Weiterentwicklung unserer Linux-basierten Infrastruktur. Erfahrung mit Docker, Kubernetes und CI/CD wuenschenswert.", "status": "offen"},
        {"id": "stellen-2", "title": "Python Backend Developer (m/w/d)", "department": "Softwareentwicklung", "description": "Entwicklung von Microservices und APIs mit Python/FastAPI. Kenntnisse in PostgreSQL und asynchroner Programmierung von Vorteil.", "status": "offen"},
        {"id": "stellen-3", "title": "Technical Writer / Dokumentationsengineer (m/w/d)", "department": "Dokumentation", "description": "Erstellung und Pflege technischer Dokumentation fuer Infrastruktur und Betrieb. Erfahrung mit Markdown und Docs-as-Code von Vorteil.", "status": "besetzt"},
    ]
    connection = _connect()
    try:
        for item in items:
            connection.execute(
                "INSERT INTO stellen (id, title, department, description, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item["id"], item["title"], item["department"], item["description"], item["status"], now, now)
            )
        connection.commit()
    finally:
        connection.close()

