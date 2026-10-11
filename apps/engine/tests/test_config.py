"""Configuración de sesión y del modo --dev (`.env.local`)."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from faro_engine.core.config import (
    DEFAULT_DEV_PORT,
    DevConfigError,
    Settings,
    default_env_file,
    load_dev_config,
    parse_env_file,
    repo_root,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env.local"
    path.write_text(text, encoding="utf-8")
    return path


def test_settings_repr_hides_token() -> None:
    token = secrets.token_urlsafe(32)
    settings = Settings(token=token.encode("ascii"), port=1234, version="0.1.0")
    assert token not in repr(settings)
    assert settings.expected_host == "127.0.0.1:1234"


def test_default_env_file_is_repo_root() -> None:
    assert (repo_root() / "apps" / "engine" / "pyproject.toml").is_file()
    assert default_env_file() == repo_root() / ".env.local"


def test_parse_env_file() -> None:
    text = "\n# c\nA=1\nexport B = 'dos'\nC=\"tres\"\nsin_igual\n"
    assert parse_env_file(text) == {"A": "1", "B": "dos", "C": "tres"}


def test_load_dev_config_defaults_port(tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    config = load_dev_config(_write(tmp_path, f"FARO_ENGINE_DEV_TOKEN={token}\n"))
    assert config.token == token.encode("ascii")
    assert config.port == DEFAULT_DEV_PORT
    assert token not in repr(config)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "token_missing"),
        ("FARO_ENGINE_DEV_TOKEN=\n", "token_missing"),
        ("FARO_ENGINE_DEV_TOKEN=<pega-aqui-el-token-generado>\n", "token_invalid"),
        ("FARO_ENGINE_DEV_TOKEN=ñ" + "a" * 42 + "\n", "token_invalid"),
        ("FARO_ENGINE_DEV_TOKEN={token}\nFARO_ENGINE_DEV_PORT=abc\n", "port_invalid"),
        ("FARO_ENGINE_DEV_TOKEN={token}\nFARO_ENGINE_DEV_PORT=80\n", "port_invalid"),
        ("FARO_ENGINE_DEV_TOKEN={token}\nFARO_ENGINE_DEV_PORT=70000\n", "port_invalid"),
        ("FARO_ENGINE_DEV_TOKEN={token}\nFARO_ENGINE_DEV_DB_KEY=corta\n", "db_key_invalid"),
        (
            "FARO_ENGINE_DEV_TOKEN={token}\nFARO_ENGINE_DEV_DB_KEY=ññññññññññññññññññññññññññññññññññññññññññññññññññññññññññññññññ\n",
            "db_key_invalid",
        ),
        ("FARO_ENGINE_DEV_TOKEN={token}\nFARO_ENGINE_DEV_DB_KEY={key}\n", "profile_id_invalid"),
        (
            "FARO_ENGINE_DEV_TOKEN={token}\nFARO_ENGINE_DEV_DB_KEY={key}\nFARO_ENGINE_DEV_PROFILE_ID=../x\n",
            "profile_id_invalid",
        ),
    ],
)
def test_load_dev_config_errors(tmp_path: Path, text: str, reason: str) -> None:
    token = secrets.token_urlsafe(32)
    with pytest.raises(DevConfigError) as info:
        load_dev_config(
            _write(tmp_path, text.replace("{token}", token).replace("{key}", "ab" * 32))
        )
    assert info.value.reason == reason
    assert token not in str(info.value)


def test_load_dev_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DevConfigError) as info:
        load_dev_config(tmp_path / "missing")
    assert info.value.reason == "env_file_missing"


def test_load_dev_config_with_dev_database(tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    profile = "01920000-0000-7000-8000-000000000001"
    config = load_dev_config(
        _write(
            tmp_path,
            f"FARO_ENGINE_DEV_TOKEN={token}\nFARO_ENGINE_DEV_DB_KEY={'cd' * 32}\n"
            f"FARO_ENGINE_DEV_PROFILE_ID={profile}\n",
        )
    )
    assert config.db_key == bytearray("cd" * 32, "ascii")
    assert config.profile_id == profile
    assert "cd" * 32 not in repr(config)


def test_load_dev_config_without_dev_database(tmp_path: Path) -> None:
    config = load_dev_config(
        _write(tmp_path, f"FARO_ENGINE_DEV_TOKEN={secrets.token_urlsafe(32)}\n")
    )
    assert config.db_key is None
    assert config.profile_id is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [("FARO_ALLOW_LOCAL_SITES=1\n", True), ("FARO_ALLOW_LOCAL_SITES=0\n", False), ("", False)],
)
def test_load_dev_config_allow_local_sites(tmp_path: Path, line: str, expected: bool) -> None:
    config = load_dev_config(
        _write(tmp_path, f"FARO_ENGINE_DEV_TOKEN={secrets.token_urlsafe(32)}\n{line}")
    )
    assert config.allow_local_sites is expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("FARO_FAKE_LLM=1\n", True),
        ("FARO_FAKE_LLM= 1 \n", True),
        ("FARO_FAKE_LLM=0\n", False),
        ("FARO_FAKE_LLM=true\n", False),
        ("", False),
    ],
)
def test_load_dev_config_fake_llm(tmp_path: Path, line: str, expected: bool) -> None:
    """Spec F1b §4.1: en `--dev`, solo el valor exacto `1` activa la IA simulada."""
    config = load_dev_config(
        _write(tmp_path, f"FARO_ENGINE_DEV_TOKEN={secrets.token_urlsafe(32)}\n{line}")
    )
    assert config.fake_llm is expected
