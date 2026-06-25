from __future__ import annotations

import unittest
from unittest.mock import patch

from app.config import ModuleConfig, ServiceProfile
from app.operations import run_service_profile_action, schedule_runtime_restart, service_overview, update_checkout


class OperationsTests(unittest.TestCase):
    def test_disabled_update_does_not_probe_git(self) -> None:
        with patch("app.operations.version_status", side_effect=AssertionError("disabled update must be inert")):
            result = update_checkout(enabled=False)

        self.assertEqual(result["reason"], "disabled")
        self.assertTrue(result["skipped"])

    def test_service_overview_uses_shallow_module_status(self) -> None:
        profile = ServiceProfile(id="module:openstack", kind="module", module_id="openstack")
        module = ModuleConfig(id="openstack", type="openstack_mcp", transport="local")
        with (
            patch("app.operations.version_status", return_value={"version": "1.2.3"}),
            patch("app.operations.list_service_profiles", return_value=[profile]),
            patch("app.operations.find_service_profile", return_value=profile),
            patch("app.operations.find_module", return_value=module),
            patch("app.operations.module_status", return_value={"running": True, "validation_errors": []}) as status,
            patch("app.operations.health_check_service", side_effect=AssertionError("overview must stay shallow")),
        ):
            result = service_overview()

        self.assertTrue(result["services"][0]["running"])
        self.assertTrue(result["services"][0]["ok"])
        status.assert_called_once_with(module)

    def test_explicit_check_uses_deep_health_check(self) -> None:
        profile = ServiceProfile(id="module:openstack", kind="module", module_id="openstack")
        with (
            patch("app.operations.find_service_profile", return_value=profile),
            patch("app.operations.health_check_service", return_value={"ok": True, "deep": True}) as health,
        ):
            result = run_service_profile_action("module:openstack", "check")

        self.assertTrue(result["deep"])
        health.assert_called_once_with("module:openstack")

    def test_scheduled_restart_prefers_systemd_run(self) -> None:
        completed = type("Completed", (), {"returncode": 0, "stdout": "queued", "stderr": ""})()
        with (
            patch("app.operations.shutil.which", return_value="/usr/bin/systemd-run"),
            patch("app.operations.find_service_profile", return_value=ServiceProfile(id="harbor", kind="harbor", systemd_mode="user")),
            patch("app.operations.subprocess.run", return_value=completed) as run,
            patch("app.operations.threading.Thread", side_effect=AssertionError("systemd-run should avoid thread fallback")),
        ):
            result = schedule_runtime_restart(delay_seconds=2.0)

        self.assertEqual(result["method"], "systemd-run")
        self.assertTrue(result["scheduled"])
        self.assertEqual(run.call_args.args[0][0], "systemd-run")


if __name__ == "__main__":
    unittest.main()
