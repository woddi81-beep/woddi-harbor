from unittest.mock import patch

from app.runtime import stop_all


def test_stop_all_stops_running_mcp_and_systemd() -> None:
    with (
        patch("app.runtime.list_mcp_instances", return_value=[{"id": "demo"}]),
        patch("app.runtime.instance_status", return_value={"running": True}),
        patch("app.runtime.stop_instance") as stop_instance,
        patch("app.runtime._stop_orphan_mcp_processes", return_value={"component": "orphan-mcp-processes", "ok": True}),
        patch("app.runtime._installed_units", return_value=["woddi-harbor.service"]),
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
