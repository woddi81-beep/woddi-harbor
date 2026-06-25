from unittest.mock import patch

from app.config import ModuleConfig, ServiceProfile
from app.runtime import _stop_local_modules, start_all, stop_all


def test_stop_all_stops_running_mcp_and_systemd() -> None:
    with (
        patch("app.runtime.list_mcp_instances", return_value=[{"id": "demo"}]),
        patch("app.runtime.instance_status", return_value={"running": True}),
        patch("app.runtime.stop_instance") as stop_instance,
        patch("app.runtime._stop_orphan_mcp_processes", return_value={"component": "orphan-mcp-processes", "ok": True}),
        patch("app.runtime._stop_local_modules", return_value=[]),
        patch("app.runtime._stop_manual_harbor", return_value={"component": "harbor", "ok": True}),
        patch("app.runtime._installed_units", return_value=["woddi-harbor.service"]),
        patch("app.runtime._profile_units", return_value=[]),
        patch("app.runtime.shutil.which", return_value=None),
        patch("app.runtime.subprocess.run") as run,
    ):
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""
        result = stop_all()

    assert result["ok"]
    stop_instance.assert_called_once_with("demo", actor="runtime.stop-all")
    assert run.call_args.args[0][:3] == ["systemctl", "--user", "stop"]


def test_start_all_uses_installed_harbor_systemd_unit() -> None:
    module = ModuleConfig(id="openstack", type="openstack_mcp", transport="local")
    profiles = [
        ServiceProfile(id="harbor", kind="harbor", systemd_mode="user", unit_name="woddi-harbor"),
        ServiceProfile(id="module:openstack", kind="module", module_id="openstack", systemd_mode="user"),
    ]
    with (
        patch("app.runtime._installed_units", return_value=["woddi-harbor.service", "woddi-harbor-openstack.service"]),
        patch("app.runtime.sync_service_profiles", return_value=profiles),
        patch("app.runtime._run", return_value={"ok": True, "returncode": 0, "stdout": "", "stderr": ""}) as run,
        patch("app.runtime._wait_for_harbor", return_value=True) as wait_for_harbor,
        patch("app.runtime.load_modules", return_value=[module]),
        patch("app.modules.start_module") as start_module,
    ):
        result = start_all()

    assert result["ok"]
    run.assert_called_once()
    assert run.call_args.args[0][:3] == ["systemctl", "--user", "start"]
    wait_for_harbor.assert_called_once_with(True)
    start_module.assert_not_called()


def test_start_all_uses_system_mode_profiles() -> None:
    profiles = [ServiceProfile(id="harbor", kind="harbor", systemd_mode="system", unit_name="woddi-harbor")]
    with (
        patch("app.runtime._installed_units", return_value=[]),
        patch("app.runtime.sync_service_profiles", return_value=profiles),
        patch("app.runtime._run", return_value={"ok": True, "returncode": 0, "stdout": "", "stderr": ""}) as run,
        patch("app.runtime._wait_for_harbor", return_value=True),
        patch("app.runtime.load_modules", return_value=[]),
    ):
        result = start_all()

    assert result["ok"]
    assert run.call_args.args[0] == ["systemctl", "start", "woddi-harbor.service"]


def test_stop_local_modules_skips_systemd_managed_modules() -> None:
    module = ModuleConfig(id="openstack", type="openstack_mcp", transport="local")
    profiles = [ServiceProfile(id="module:openstack", kind="module", module_id="openstack", systemd_mode="user")]
    with (
        patch("app.runtime.sync_service_profiles", return_value=profiles),
        patch("app.runtime.load_modules", return_value=[module]),
        patch("app.modules.stop_module") as stop_module,
    ):
        result = _stop_local_modules()

    assert result == []
    stop_module.assert_not_called()
