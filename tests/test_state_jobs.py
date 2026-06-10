from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import state
from app.jobs import execute_job


class StateJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database_patch = patch.object(state, "DATABASE_PATH", Path(self.temporary.name) / "harbor.db")
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)

    def test_database_migrates_to_current_version(self) -> None:
        state.initialize_database()
        with state.read_connection() as connection:
            version = connection.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
        self.assertEqual(version, state.SCHEMA_VERSION)
        self.assertIn("worker_id", columns)
        self.assertIn("attempts", columns)

    def test_jobs_are_claimed_once(self) -> None:
        job_id = state.create_job("module.reindex", "docs", {})
        first = state.claim_next_job("worker-a")
        second = state.claim_next_job("worker-b")
        self.assertEqual(first["id"], job_id)
        self.assertEqual(first["worker_id"], "worker-a")
        self.assertIsNone(second)

    def test_unknown_job_kind_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unbekannter Job-Typ"):
            execute_job({"kind": "shell", "target": "", "payload": {}})
