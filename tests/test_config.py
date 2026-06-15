from __future__ import annotations

import json

from app.config import HarborSettings, load_settings


def test_default_harbor_settings_bind_to_loopback() -> None:
    assert HarborSettings().host == "127.0.0.1"


def test_load_settings_preserves_legacy_loopback_bind(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    harbor_path = config_dir / "harbor.json"
    harbor_path.write_text(
        json.dumps(
            {
                "name": "Harbor",
                "host": "127.0.0.1",
                "port": 9680,
                "llm": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("app.config.LOG_DIR", tmp_path / "data" / "logs")
    monkeypatch.setattr("app.config.RUNTIME_DIR", tmp_path / "data" / "runtime")
    monkeypatch.setattr("app.config.PID_DIR", tmp_path / "data" / "runtime" / "pids")
    monkeypatch.setattr("app.config.SECRETS_DIR", tmp_path / "data" / "secrets")
    monkeypatch.setattr("app.config.INTERNAL_TOKEN_PATH", tmp_path / "data" / "secrets" / "worker.token")
    monkeypatch.setattr("app.config.INTERNAL_ENV_PATH", tmp_path / "data" / "secrets" / "worker.env")

    settings = load_settings()

    assert settings.host == "127.0.0.1"
    persisted = json.loads(harbor_path.read_text(encoding="utf-8"))
    assert persisted["host"] == "127.0.0.1"
    assert persisted["listen_configured"] is True


def test_load_settings_preserves_explicit_loopback_bind(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    harbor_path = config_dir / "harbor.json"
    harbor_path.write_text(
        json.dumps(
            {
                "name": "Harbor",
                "host": "127.0.0.1",
                "port": 9680,
                "listen_configured": True,
                "llm": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("app.config.LOG_DIR", tmp_path / "data" / "logs")
    monkeypatch.setattr("app.config.RUNTIME_DIR", tmp_path / "data" / "runtime")
    monkeypatch.setattr("app.config.PID_DIR", tmp_path / "data" / "runtime" / "pids")
    monkeypatch.setattr("app.config.SECRETS_DIR", tmp_path / "data" / "secrets")
    monkeypatch.setattr("app.config.INTERNAL_TOKEN_PATH", tmp_path / "data" / "secrets" / "worker.token")
    monkeypatch.setattr("app.config.INTERNAL_ENV_PATH", tmp_path / "data" / "secrets" / "worker.env")

    assert load_settings().host == "127.0.0.1"
