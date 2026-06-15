from pathlib import Path

import tools.verify_installation
from tools.verify_installation import verify


def test_verify_reports_missing_checkout_modules(tmp_path: Path) -> None:
    errors = verify(tmp_path, source_only=True)

    assert any("app/backup.py" in error for error in errors)


def test_verify_reports_stale_installed_version(monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(tools.verify_installation.importlib.metadata, "version", lambda _name: "0.0.0")

    errors = verify(project_root)

    assert any("Installierte Version 0.0.0" in error for error in errors)
