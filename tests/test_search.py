from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.search import ensure_index, load_index_meta


class SearchCacheTests(unittest.TestCase):
    def test_html_is_indexed_as_visible_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "docs"
            root.mkdir()
            (root / "guide.html").write_text(
                "<html><head><style>.x{}</style></head><body><h1>Harbor Guide</h1><script>ignored()</script><p>Visible content</p></body></html>",
                encoding="utf-8",
            )
            index_path = Path(tmpdir) / "index.json"

            index, rebuilt = ensure_index("docs", [("docs-source", "Docs", root)], index_path)

            self.assertTrue(rebuilt)
            self.assertEqual(index.document_count, 1)
            self.assertIn("Harbor Guide", index.documents[0].text)
            self.assertIn("Visible content", index.documents[0].text)
            self.assertNotIn("ignored", index.documents[0].text)

    def test_ensure_index_reuses_memory_cache_within_staleness_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "docs"
            root.mkdir()
            (root / "readme.md").write_text("hello cache\n", encoding="utf-8")
            index_path = Path(tmpdir) / "index.json"
            roots = [("docs-source", "Docs", root)]

            index, rebuilt = ensure_index("docs", roots, index_path)
            self.assertTrue(rebuilt)
            self.assertEqual(index.document_count, 1)

            with patch("app.search.index_is_stale", side_effect=AssertionError("staleness check should be skipped inside TTL")):
                cached_index, rebuilt_again = ensure_index("docs", roots, index_path)

            self.assertFalse(rebuilt_again)
            self.assertEqual(cached_index.document_count, 1)

    def test_load_index_meta_reads_sidecar_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "docs"
            root.mkdir()
            (root / "readme.md").write_text("hello meta\n", encoding="utf-8")
            index_path = Path(tmpdir) / "index.json"
            roots = [("docs-source", "Docs", root)]

            index, rebuilt = ensure_index("docs", roots, index_path)
            self.assertTrue(rebuilt)

            meta = load_index_meta(index_path)
            self.assertIsNotNone(meta)
            self.assertEqual(meta.built_at, index.built_at)
            self.assertEqual(meta.document_count, 1)


if __name__ == "__main__":
    unittest.main()
