"""Тесты форматирования сообщений Telegram (CONTRACTS §7, STEP 5)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_assistant.config import TelegramSettings, load_settings
from trading_assistant.telegram.formatter import (
    DISCLAIMER,
    MAX_MESSAGE_CHARS,
    SignalView,
    format_alert,
    format_digest,
    split_blocks,
)

PAIR_KEY = "anthropic/claude-sonnet-5.5+x-ai/grok-4.7"
START = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)


def _cfg(**overrides: object) -> TelegramSettings:
    """Настройки telegram из config/settings.yaml (stat_min_n=30, stat_ok_n=50)."""
    data = load_settings(Path("config/settings.yaml")).telegram.model_dump()
    data.update(overrides)
    return TelegramSettings.model_validate(data)


def _signal(**overrides: object) -> SignalView:
    """Сигнал по умолчанию: SBER, рост на 18% за 90 дней."""
    data: dict[str, object] = {
        "prediction_id": 1,
        "ticker": "SBER",
        "name": "Сбербанк",
        "direction": "up",
        "confidence": 88,
        "horizon_days": 90,
        "expected_change_pct": 18.0,
        "reason": "рост прибыли",
        "sources_count": 3,
        "severity": 8,
        "url": "https://example.com/news",
        "title": "Новость",
        "created_at": START,
    }
    data.update(overrides)
    return SignalView(**data)  # type: ignore[arg-type]


def test_alert_up_with_statistics() -> None:
    """Алерт «рост» с точностью n=52 — обычная цифра без пометки о выборке."""
    text = format_alert(_signal(), (80.0, 52), _cfg())
    assert "рост не менее 18%" in text
    assert "за срок до 90 дн." in text
    assert "Сбылось прогнозов по SBER: 80% (n=52)." in text
    assert "по небольшой выборке" not in text
    assert text.endswith(DISCLAIMER)
    assert "Вероятность" not in text


def test_alert_down_keeps_decimal() -> None:
    """Падение 15.5% показывается без лишних нулей."""
    text = format_alert(_signal(direction="down", expected_change_pct=15.5), (None, 0), _cfg())
    assert "падение не менее 15.5%" in text


def test_alert_small_sample_hides_percent() -> None:
    """При n < stat_min_n цифра процента не показывается."""
    text = format_alert(_signal(), (80.0, 29), _cfg())
    assert "Статистики по SBER пока мало (n=29)" in text
    assert "80%" not in text


def test_alert_small_sample_mark() -> None:
    """При stat_min_n <= n < stat_ok_n добавляется пометка о небольшой выборке."""
    text = format_alert(_signal(), (83.0, 30), _cfg())
    assert "83% (n=30, по небольшой выборке)" in text


def test_alert_stat_ok_boundary() -> None:
    """n=49 — ещё небольшая выборка, n=50 — уже обычная цифра."""
    assert "по небольшой выборке" in format_alert(_signal(), (83.0, 49), _cfg())
    assert "по небольшой выборке" not in format_alert(_signal(), (83.0, 50), _cfg())


def test_alert_priority_mark() -> None:
    """Приоритетный алерт начинается с пометки."""
    text = format_alert(_signal(), (80.0, 52), _cfg(), priority=True)
    assert text.startswith("Приоритетный прогноз")


def test_alert_without_source() -> None:
    """Без ссылки на источник строка «Источник» не выводится."""
    text = format_alert(_signal(url=None, title=None), (80.0, 52), _cfg())
    assert "Источник" not in text


def test_alert_escapes_reason() -> None:
    """HTML в reason экранируется."""
    text = format_alert(_signal(reason="<b>x</b>"), (80.0, 52), _cfg())
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    assert "<b>x</b>" not in text


def test_alert_escapes_url_quotes() -> None:
    """URL попадает в атрибут href с экранированием кавычек."""
    text = format_alert(_signal(url="https://example.com/?a=1&b=\"2\""), (80.0, 52), _cfg())
    assert 'href="https://example.com/?a=1&amp;b=&quot;2&quot;"' in text


def test_digest_orders_by_severity_then_sources() -> None:
    """Блоки идут по severity вниз, затем по числу источников вниз."""
    sigs = [
        _signal(ticker="SBER", name="Сбербанк", severity=7, sources_count=2),
        _signal(ticker="LKOH", name="ЛУКОЙЛ", severity=7, sources_count=5, prediction_id=2),
        _signal(ticker="GAZP", name="Газпром", severity=9, sources_count=1, prediction_id=3),
    ]
    text = "\n\n".join(format_digest(sigs, {}, {}, _cfg()))
    assert text.index("<b>Газпром (GAZP)</b>") < text.index("<b>ЛУКОЙЛ (LKOH)</b>")
    assert text.index("<b>ЛУКОЙЛ (LKOH)</b>") < text.index("<b>Сбербанк (SBER)</b>")


def test_digest_one_block_per_ticker_with_count() -> None:
    """Два сигнала по тикеру дают один блок с числом прогнозов и один дисклеймер."""
    sigs = [
        _signal(created_at=START, expected_change_pct=16.0),
        _signal(prediction_id=2, created_at=START + timedelta(hours=1), expected_change_pct=20.0),
    ]
    text = "\n\n".join(format_digest(sigs, {}, {"SBER": 2}, _cfg()))
    assert text.count("<b>Сбербанк (SBER)</b>") == 1
    assert "Прогнозов по тикеру: 2" in text
    assert text.count(DISCLAIMER) == 1
    # Показывается самый свежий сигнал тикера.
    assert "20%" in text
    assert "16%" not in text


def test_digest_uses_latest_signal_only() -> None:
    """Из двух сигналов по тикеру в блоке остаётся последний по времени."""
    sigs = [
        _signal(created_at=START + timedelta(hours=2), reason="свежая новость"),
        _signal(prediction_id=2, created_at=START, reason="старая новость"),
    ]
    text = "\n\n".join(format_digest(sigs, {}, {"SBER": 2}, _cfg()))
    assert "свежая новость" in text
    assert "старая новость" not in text


def test_digest_without_signals_is_empty() -> None:
    """Нет сигналов — нет сообщений (дайджест не отправляется)."""
    assert format_digest([], {}, {}, _cfg()) == []


def test_digest_splits_long_list() -> None:
    """200 блоков раскладываются на несколько сообщений, каждое не длиннее 4096."""
    sigs = [
        _signal(ticker=f"T{index:03d}", name=f"Тикер {index}", prediction_id=index + 1)
        for index in range(200)
    ]
    messages = format_digest(sigs, {}, {}, _cfg())
    assert len(messages) > 1
    assert all(len(message) <= MAX_MESSAGE_CHARS for message in messages)
    assert messages[0].startswith("Утренний дайджест")
    assert messages[-1].endswith(DISCLAIMER)


def test_split_blocks_clips_single_huge_block() -> None:
    """Блок длиннее лимита обрезается с многоточием и не ломает лимит."""
    messages = split_blocks("Заголовок", ["я" * 5000], DISCLAIMER, limit=1000)
    assert len(messages) == 1
    assert len(messages[0]) <= 1000
    assert "…" in messages[0]


def test_split_blocks_respects_limit() -> None:
    """Много коротких блоков делятся по границам блоков."""
    blocks = [f"блок {index} " + "х" * 100 for index in range(60)]
    messages = split_blocks("Заголовок", blocks, DISCLAIMER)
    assert len(messages) > 1
    assert all(len(message) <= MAX_MESSAGE_CHARS for message in messages)
    for message in messages[1:]:
        assert "блок " in message
