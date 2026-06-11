from pathlib import Path

from app import config


def test_ensure_layout_does_not_rechmod_secure_users_file(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    users_file = config_dir / "users.json"
    config_dir.mkdir()
    users_file.write_text('{"users": []}\n', encoding="utf-8")
    users_file.chmod(0o600)

    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "LOG_DIR", data_dir / "logs")
    monkeypatch.setattr(config, "RUNTIME_DIR", data_dir / "runtime")
    monkeypatch.setattr(config, "PID_DIR", data_dir / "runtime" / "pids")
    monkeypatch.setattr(config, "SECRETS_DIR", data_dir / "secrets")
    monkeypatch.setattr(config, "internal_worker_token", lambda: "token")

    def fail_chmod(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if self == users_file:
            raise AssertionError("secure users file must not be chmod'ed again")
        return original_chmod(self, mode, follow_symlinks=follow_symlinks)

    original_chmod = Path.chmod
    monkeypatch.setattr(Path, "chmod", fail_chmod)

    config.ensure_layout()


def test_config_lock_is_stored_in_runtime_directory(monkeypatch, tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    config_file = tmp_path / "readonly-config" / "modules.json"
    config_file.parent.mkdir()
    config_file.write_text('{"modules": []}\n', encoding="utf-8")
    config_file.parent.chmod(0o555)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime_dir)

    try:
        with config._locked_path(config_file, exclusive=False):
            pass
    finally:
        config_file.parent.chmod(0o755)

    locks = list((runtime_dir / "locks").glob("modules.json-*.lock"))
    assert len(locks) == 1
    assert not config_file.with_suffix(".json.lock").exists()
