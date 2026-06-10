from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.sources import source_quality


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
