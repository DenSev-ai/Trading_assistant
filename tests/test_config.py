"""Тесты config.py: загрузка settings.yaml, валидация, чек-лист .env."""
from __future__ import annotations

from pathlib import Path

import pytest

from trading_assistant.config import (
    ConfigError,
    EnvSettings,
    env_checklist,
    load_keywords,
    load_settings,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "settings.yaml"


def test_load_settings_real_config() -> None:
    settings = load_settings(CONFIG)
    assert settings.timezone == "Europe/Moscow"
    assert settings.data_dir == "data"
    assert len(settings.llm.pair) == 2
    assert settings.llm.pair[0].id == "anthropic/claude-sonnet-5.5"
    assert settings.llm.pair[1].id == "x-ai/grok-4.7"
    assert settings.signal.min_move_pct == 15
    assert settings.evaluation.sample_n == 100


def test_load_settings_extra_field_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        (CONFIG.read_text(encoding="utf-8")) + "\nextra_field: 1\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_settings(bad)


def test_load_settings_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.yaml")


def test_cycle_interval_negative_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        CONFIG.read_text(encoding="utf-8").replace(
            "cycle_interval_sec: 180", "cycle_interval_sec: -5"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_settings(bad)


def test_min_move_pct_zero_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        CONFIG.read_text(encoding="utf-8").replace("min_move_pct: 15", "min_move_pct: 0"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_settings(bad)


def test_magnitude_tolerance_one_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        CONFIG.read_text(encoding="utf-8").replace(
            "magnitude_tolerance: 0.20", "magnitude_tolerance: 1"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_settings(bad)


def test_pair_same_prefix_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    text = CONFIG.read_text(encoding="utf-8")
    text = text.replace('id: "anthropic/claude-sonnet-5.5"', 'id: "openai/a"')
    text = text.replace('id: "x-ai/grok-4.7"', 'id: "openai/b"')
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(bad)


def test_env_checklist_missing_required() -> None:
    env = EnvSettings(OPENROUTER_API_KEY="")
    lines, ok = env_checklist(env)
    assert not ok
    assert "[НЕТ] OPENROUTER_API_KEY" in lines
    joined = "\n".join(lines)
    assert "sk-" not in joined


def test_env_checklist_optional_empty() -> None:
    env = EnvSettings(
        OPENROUTER_API_KEY="sk-test123",
        TELEGRAM_BOT_TOKEN="",
        TELEGRAM_CHAT_IDS="",
        SMTP_HOST="",
        SMTP_USER="",
        SMTP_PASSWORD="",
        REPORT_EMAIL_TO="",
    )
    lines, ok = env_checklist(env)
    assert ok
    assert "[OK] OPENROUTER_API_KEY" in lines
    assert "[ОПЦИОНАЛЬНО] TELEGRAM_BOT_TOKEN" in lines
    assert "[ОПЦИОНАЛЬНО] SMTP_HOST" in lines
    assert "[ОПЦИОНАЛЬНО] REPORT_EMAIL_TO" in lines
    joined = "\n".join(lines)
    assert "sk-test123" not in joined


def test_env_checklist_all_present() -> None:
    env = EnvSettings(
        OPENROUTER_API_KEY="sk-1",
        TELEGRAM_BOT_TOKEN="bot",
        TELEGRAM_CHAT_IDS="123",
        SMTP_HOST="smtp.example.com",
        SMTP_USER="user",
        SMTP_PASSWORD="pass",
        REPORT_EMAIL_TO="a@b.c",
    )
    lines, ok = env_checklist(env)
    assert ok
    for name in (
        "OPENROUTER_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_IDS",
        "SMTP_HOST",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "REPORT_EMAIL_TO",
    ):
        assert f"[OK] {name}" in lines


def test_load_keywords(tmp_path: Path) -> None:
    p = tmp_path / "keywords.yaml"
    p.write_text("include: [центробанк, ставк]\nexclude: [футбол, гороскоп]\n", encoding="utf-8")
    kw = load_keywords(p)
    assert kw.include == ["центробанк", "ставк"]
    assert kw.exclude == ["футбол", "гороскоп"]


def test_settings_forbid_extra_at_all_levels(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    text = CONFIG.read_text(encoding="utf-8").replace(
        "min_confidence: 85", "min_confidence: 85\nunknown: 1"
    )
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(bad)
