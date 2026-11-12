"""Форматирование сообщений Telegram (CONTRACTS §7): чистые функции без БД и сети."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime

from ..config import TelegramSettings

# Ограничение Telegram на длину одного сообщения.
MAX_MESSAGE_CHARS = 4096
ELLIPSIS = "…"
DIGEST_HEADER = "Утренний дайджест"
DISCLAIMER = "Не является инвестиционной рекомендацией."
PRIORITY_MARK = "Приоритетный прогноз"
SOURCE_LABEL = "Источник"
UP_WORD = "рост"
DOWN_WORD = "падение"
DIGEST_COUNT_TEMPLATE = "Прогнозов по тикеру: {n}"
LOW_STATS_TEMPLATE = (
    "Статистики по {ticker} пока мало (n={n}): пара или модели недавно менялись."
)
SMALL_STATS_TEMPLATE = "Сбылось прогнозов по {ticker}: {pct}% (n={n}, по небольшой выборке)."
FULL_STATS_TEMPLATE = "Сбылось прогнозов по {ticker}: {pct}% (n={n})."


@dataclass
class SignalView:
    """Сигнал для сообщения: поля прогноза плюс данные кластера и инструмента."""

    prediction_id: int
    ticker: str
    name: str
    direction: str
    confidence: int
    horizon_days: int
    expected_change_pct: float
    reason: str
    sources_count: int
    severity: int
    url: str | None
    title: str | None
    created_at: datetime


def escape(text: str) -> str:
    """Экранировать динамический текст для HTML-разметки Telegram."""
    return html.escape(str(text))


def _number(value: float) -> str:
    """Число без лишних нулей: 18 -> '18', 18.5 -> '18.5'."""
    return f"{value:g}"


def _percent(value: float | None) -> str:
    """Процент точности целым числом ('80', '83')."""
    return f"{(value or 0.0):.0f}"


def accuracy_line(ticker: str, accuracy: tuple[float | None, int], cfg: TelegramSettings) -> str:
    """Строка о сбывшихся прогнозах по тикеру с оговоркой о размере выборки."""
    percent, n = accuracy
    if n < cfg.stat_min_n:
        return LOW_STATS_TEMPLATE.format(ticker=ticker, n=n)
    template = SMALL_STATS_TEMPLATE if n < cfg.stat_ok_n else FULL_STATS_TEMPLATE
    return template.format(ticker=ticker, pct=_percent(percent), n=n)


def _alert_block(
    sig: SignalView,
    accuracy: tuple[float | None, int],
    cfg: TelegramSettings,
    priority: bool,
) -> str:
    """Тело сообщения об одном сигнале без дисклеймера (общее для алерта и дайджеста)."""
    direction = UP_WORD if sig.direction == "up" else DOWN_WORD
    lines = [
        f"<b>{escape(sig.name)} ({escape(sig.ticker)})</b> — {direction} не менее "
        f"{_number(sig.expected_change_pct)}% за срок до {sig.horizon_days} дн., "
        f"потому что {escape(sig.reason)}.",
        f"Уверенность моделей: {sig.confidence}%. "
        f"Прогноз на основе {sig.sources_count} источников.",
        accuracy_line(sig.ticker, accuracy, cfg),
    ]
    if sig.url:
        title = escape(sig.title or sig.url)
        href = html.escape(sig.url, quote=True)
        lines.append(f'{SOURCE_LABEL}: <a href="{href}">{title}</a>')
    block = "\n".join(lines)
    return f"{PRIORITY_MARK}\n\n{block}" if priority else block


def format_alert(
    sig: SignalView,
    accuracy: tuple[float | None, int],
    cfg: TelegramSettings,
    priority: bool = False,
) -> str:
    """Сообщение-алерт по одному сигналу (CONTRACTS §7) с дисклеймером в конце."""
    block = _alert_block(sig, accuracy, cfg, priority)
    return f"{block}\n\n{DISCLAIMER}"


def _clip(text: str, limit: int) -> str:
    """Обрезать блок до limit символов, поставив многоточие."""
    if len(text) <= limit:
        return text
    return text[: max(limit - len(ELLIPSIS), 0)] + ELLIPSIS


def split_blocks(
    header: str, blocks: list[str], footer: str, limit: int = MAX_MESSAGE_CHARS
) -> list[str]:
    """Разложить блоки по сообщениям: заголовок в первом, дисклеймер в последнем."""
    body_limit = limit - len(footer) - 2
    messages: list[list[str]] = []
    current = [header]
    current_len = len(header)
    for block in blocks:
        piece = _clip(block, max(body_limit - len(header) - 2, 1))
        addition = len(piece) + 2
        if current_len + addition > body_limit and len(current) > 1:
            messages.append(current)
            current = [piece]
            current_len = len(piece)
            continue
        current.append(piece)
        current_len += addition
    messages.append(current)
    result = ["\n\n".join(parts) for parts in messages]
    result[-1] = f"{result[-1]}\n\n{footer}"
    return result


def _latest_by_ticker(sigs: list[SignalView]) -> list[SignalView]:
    """По одному (самому свежему) сигналу на тикер, в порядке severity и источников."""
    latest: dict[str, SignalView] = {}
    for sig in sigs:
        current = latest.get(sig.ticker)
        if current is None or sig.created_at >= current.created_at:
            latest[sig.ticker] = sig
    ordered = sorted(
        latest.values(), key=lambda sig: (-sig.severity, -sig.sources_count, sig.ticker)
    )
    return ordered


def format_digest(
    sigs: list[SignalView],
    accuracies: dict[str, tuple[float | None, int]],
    counts: dict[str, int],
    cfg: TelegramSettings,
) -> list[str]:
    """Утренний дайджест: по блоку на тикер, дисклеймер один раз; пусто — нет сигналов."""
    if not sigs:
        return []
    blocks: list[str] = []
    for sig in _latest_by_ticker(sigs):
        block = _alert_block(sig, accuracies.get(sig.ticker, (None, 0)), cfg, priority=False)
        seen = counts.get(sig.ticker, 0)
        if seen > 1:
            block = f"{block}\n{DIGEST_COUNT_TEMPLATE.format(n=seen)}"
        blocks.append(block)
    return split_blocks(DIGEST_HEADER, blocks, DISCLAIMER)
