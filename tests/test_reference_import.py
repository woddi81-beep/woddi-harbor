from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.sources import configure_document_sources


class ReferenceImportTests(unittest.TestCase):
    def test_configure_document_sources_rejects_missing_directories(self) -> None:
        with self.assertRaisesRegex(ValueError, "Dokumentverzeichnis nicht gefunden"):
            configure_document_sources("/missing/operations", "/missing/customer")

    def test_configure_document_sources_uses_markdown_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operations = root / "operations"
            customer = root / "customer"
            operations.mkdir()
            customer.mkdir()
            with (
                patch("app.sources.save_sources") as save_sources,
                patch("app.sources.load_modules", return_value=[]),
                patch("app.sources.save_modules") as save_modules,
            ):
                result = configure_document_sources(str(operations), str(customer))

        configured = save_sources.call_args.args[0]
        self.assertEqual(configured[0].source_path, str(operations))
        self.assertEqual(configured[1].source_path, str(customer))
        self.assertEqual(configured[0].include_extensions, [".md", ".markdown"])
        self.assertEqual(len(save_modules.call_args.args[0]), 2)
        self.assertTrue(result["ok"])
