from pathlib import Path

from tools.verify_installation import verify


def test_verify_reports_missing_checkout_modules(tmp_path: Path) -> None:
    errors = verify(tmp_path, source_only=True)

    assert any("app/backup.py" in error for error in errors)
