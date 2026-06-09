from __future__ import annotations

import unittest

from app.mcp_lifecycle import validate_manifest


class McpLifecycleTests(unittest.TestCase):
    def test_validate_process_manifest(self) -> None:
        manifest = validate_manifest(
            {
                "id": "example-mcp",
                "version": "1.0.0",
                "driver": "process",
                "command": ["bin/server"],
                "tools": ["search"],
            }
        )
        self.assertEqual(manifest["id"], "example-mcp")
        self.assertEqual(manifest["tools"], ["search"])

    def test_process_manifest_requires_command_array(self) -> None:
        with self.assertRaises(ValueError):
            validate_manifest({"id": "broken", "version": "1", "driver": "process"})

    def test_http_manifest_requires_endpoint(self) -> None:
        with self.assertRaises(ValueError):
            validate_manifest({"id": "remote", "version": "1", "driver": "http"})

    def test_systemd_manifest_requires_unit(self) -> None:
        with self.assertRaises(ValueError):
            validate_manifest({"id": "service", "version": "1", "driver": "systemd"})

    def test_container_manifest_requires_image(self) -> None:
        with self.assertRaises(ValueError):
            validate_manifest({"id": "container", "version": "1", "driver": "container"})
