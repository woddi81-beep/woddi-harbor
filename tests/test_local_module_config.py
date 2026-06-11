from pathlib import Path

from app import config


def test_module_writes_use_ignored_local_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    assert config.modules_config_path(for_write=True) == tmp_path / "modules.local.json"


def test_module_reads_fall_back_to_versioned_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    assert config.modules_config_path() == tmp_path / "modules.json"
    (tmp_path / "modules.local.json").write_text('{"modules": []}\n', encoding="utf-8")
    assert config.modules_config_path() == tmp_path / "modules.local.json"
