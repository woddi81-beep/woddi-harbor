from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import ModuleConfig
from app.sources import ManagedSource, _copy_local, source_quality, sync_source


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

    def test_local_copy_keeps_document_text_and_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin"
            target = root / "target"
            origin.mkdir()
            (origin / "guide.md").write_text("# Guide\n", encoding="utf-8")
            (origin / "guide.html").write_text("<h1>Guide</h1>", encoding="utf-8")
            (origin / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (origin / "script.sh").write_text("exit 0\n", encoding="utf-8")
            _copy_local(
                ManagedSource(
                    id="docs",
                    kind="local",
                    source_path=str(origin),
                    include_extensions=[".md", ".html", ".png"],
                ),
                target,
            )

            self.assertTrue((target / "guide.md").is_file())
            self.assertTrue((target / "guide.html").is_file())
            self.assertTrue((target / "diagram.png").is_file())
            self.assertFalse((target / "script.sh").exists())

    def test_images_alone_do_not_pass_source_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n" * 20)

            quality = source_quality(root, [".png"])

        self.assertEqual(quality["asset_files"], 1)
        self.assertEqual(quality["text_files"], 0)
        self.assertFalse(quality["healthy"])

    def test_sync_reindexes_local_docs_without_worker_http(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin"
            target = root / "target"
            origin.mkdir()
            (origin / "guide.md").write_text("searchable documentation " * 10, encoding="utf-8")
            source = ManagedSource(
                id="operation-docs",
                kind="local",
                module_id="10",
                source_path=str(origin),
                target_path=str(target),
                include_extensions=[".md"],
            )
            module = ModuleConfig(id="10", type="docs", transport="local")
            with (
                patch("app.sources.find_source", return_value=source),
                patch("app.sources.find_module", return_value=module),
                patch("app.sources.worker_execute", return_value={"ok": True}) as direct_reindex,
                patch("app.sources.execute_module", side_effect=AssertionError("HTTP transport must not be used")),
                patch("app.sources.SOURCE_LOCK_DIR", root / "locks"),
                patch("app.sources.SOURCES_RUNTIME_DIR", root / "runtime"),
            ):
                result = sync_source("operation-docs")

        direct_reindex.assert_called_once_with(module, "reindex", {})
        self.assertEqual(result["reindex_mode"], "direct")
        self.assertTrue(result["ok"])
