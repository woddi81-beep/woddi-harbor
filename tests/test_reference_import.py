from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.import_reference_docs import SourceFile, _write_corpus


class ReferenceImportTests(unittest.TestCase):
    def test_import_uses_explicit_files_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("# Real documentation\n\nOperational content.\n", encoding="utf-8")
            target = root / "target"
            manifest = _write_corpus(
                target,
                [SourceFile(source, "document.md", "operations")],
                corpus="test",
            )

            self.assertEqual(manifest["file_count"], 1)
            self.assertTrue((target / "document.md").is_file())
            self.assertTrue((target / "_SOURCE_MANIFEST.json").is_file())
