from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.sources import ManagedSource, _copy_local, source_quality


class SourceQualityTests(unittest.TestCase):
    def test_quality_reports_content_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.md").write_text("document content " * 10, encoding="utf-8")
            (root / "b.md").write_text("document content " * 10, encoding="utf-8")
            quality = source_quality(root, [".md"])
        self.assertEqual(quality["files"], 2)
        self.assertEqual(quality["duplicate_files"], 1)
        self.assertTrue(quality["healthy"])

    def test_local_copy_keeps_only_configured_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin"
            target = root / "target"
            origin.mkdir()
            (origin / "guide.md").write_text("# Guide\n", encoding="utf-8")
            (origin / "script.sh").write_text("exit 0\n", encoding="utf-8")
            _copy_local(
                ManagedSource(
                    id="docs",
                    kind="local",
                    source_path=str(origin),
                    include_extensions=[".md"],
                ),
                target,
            )

            self.assertTrue((target / "guide.md").is_file())
            self.assertFalse((target / "script.sh").exists())
