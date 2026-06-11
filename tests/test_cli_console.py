from typer.testing import CliRunner

from app.cli import app

runner = CliRunner()


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
