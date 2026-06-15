from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.mcp_lifecycle import reconcile_desired_instances, validate_manifest


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

    def test_reconcile_starts_desired_process_once(self) -> None:
        with TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "mcp-reconcile.lock"
            with (
                patch("app.mcp_lifecycle.RECONCILE_LOCK_PATH", lock_path),
                patch(
                    "app.mcp_lifecycle.list_mcp_instances",
                    return_value=[{"id": "ops", "desired_state": "running"}],
                ),
                patch("app.mcp_lifecycle.instance_status", return_value={"running": False}),
                patch("app.mcp_lifecycle.start_instance", return_value={"running": True}) as start,
            ):
                result = reconcile_desired_instances()

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["action"], "started")
        start.assert_called_once_with("ops", actor="startup")

    def test_reconcile_leaves_matching_state_unchanged(self) -> None:
        with TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "mcp-reconcile.lock"
            with (
                patch("app.mcp_lifecycle.RECONCILE_LOCK_PATH", lock_path),
                patch(
                    "app.mcp_lifecycle.list_mcp_instances",
                    return_value=[{"id": "ops", "desired_state": "running"}],
                ),
                patch("app.mcp_lifecycle.instance_status", return_value={"running": True}),
                patch("app.mcp_lifecycle.start_instance") as start,
            ):
                result = reconcile_desired_instances()

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["action"], "unchanged")
        start.assert_not_called()
