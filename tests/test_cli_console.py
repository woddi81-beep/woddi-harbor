from typer.testing import CliRunner

from app.cli import app, auto_update_checkout
from app.config import HarborSettings
from app.version import __version__

runner = CliRunner()


def test_version_command_reports_installed_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"woddi-harbor {__version__}"


def test_version_option_reports_installed_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"woddi-harbor {__version__}"


def test_version_command_supports_machine_readable_output() -> None:
    result = runner.invoke(app, ["version", "--short"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_help_exposes_single_console_entrypoint() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "console" in result.stdout
    assert "console-ui" not in result.stdout
    assert " tui " not in result.stdout


def test_console_simple_uses_fallback(monkeypatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr("app.cli._open_console", lambda *, simple=False: called.append(simple))

    result = runner.invoke(app, ["console", "--simple"])

    assert result.exit_code == 0
    assert called == [True]


def test_no_command_is_explicit_without_terminal(monkeypatch) -> None:
    monkeypatch.setattr("app.cli.sys.stdin.isatty", lambda: False)

    result = runner.invoke(app, [])

    assert result.exit_code == 2
    assert "benötigt ein Terminal" in result.stdout


def test_server_set_persists_external_bind(monkeypatch) -> None:
    settings = HarborSettings()
    saved: list[HarborSettings] = []
    monkeypatch.setattr("app.cli.load_settings", lambda: settings)
    monkeypatch.setattr("app.cli.save_settings", saved.append)

    result = runner.invoke(app, ["server", "set", "--host", "0.0.0.0", "--port", "9680"])

    assert result.exit_code == 0
    assert saved == [settings]
    assert settings.host == "0.0.0.0"
    assert settings.port == 9680
    assert settings.listen_configured is True
    assert '"external": true' in result.stdout


def test_startup_git_update_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("HARBOR_AUTO_UPDATE", "0")

    result = auto_update_checkout()

    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["reason"] == "disabled"
