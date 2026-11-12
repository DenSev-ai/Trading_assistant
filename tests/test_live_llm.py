"""Живой тест (маркер live): реальный вызов моделей пары на короткой новости (STEP 3C)."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from trading_assistant.config import EnvSettings, load_settings
from trading_assistant.instruments import InstrumentRegistry
from trading_assistant.llm.analyzer import build_system_prompt, build_user_message, run_model
from trading_assistant.llm.client import OpenRouterClient
from trading_assistant.llm.consensus import is_issued

TEMPLATE = Path("prompts/analyze_news.txt").read_text(encoding="utf-8")
NEWS = "ЦБ оставил ключевую ставку без изменений, как и ожидал рынок"


@pytest.mark.live
async def test_live_pair_models_on_expected_news() -> None:
    """Модели пары не должны выдавать прогноз по ожидаемой рынком новости о ставке."""
    env = EnvSettings()
    if not env.OPENROUTER_API_KEY:
        pytest.skip("нет OPENROUTER_API_KEY в .env")
    cfg = load_settings(Path("config/settings.yaml"))
    registry = InstrumentRegistry.load(Path("config/instruments.json"))
    system = build_system_prompt(TEMPLATE, registry, cfg.signal)
    user = build_user_message(
        title=NEWS,
        text=NEWS,
        sources_count=1,
        max_chars=cfg.llm.max_text_chars,
        published_msk="11.11.2026 19:00",
        source="interfax",
    )
    failed: list[str] = []
    async with httpx.AsyncClient() as client:
        llm = OpenRouterClient(
            client, env.OPENROUTER_API_KEY, cfg.llm.base_url, cfg.llm.timeout_sec
        )
        for spec in cfg.llm.pair:
            outcome, records = await run_model(llm, spec, system, user, cfg.llm, registry)
            print(f"{spec.id}: status={outcome.status}, стоимость={outcome.cost_usd:.6f}")
            for record in records:
                if record.raw_response:
                    print(record.raw_response)
            if outcome.analysis is None:
                failed.append(spec.id)
                continue
            for item in outcome.analysis.instruments:
                issued = is_issued(item, outcome.analysis.severity, cfg.signal)
                print(f"  {item.ticker} {item.direction} {item.expected_change_pct}% -> {issued}")
                assert not issued, f"модель {spec.id} выдала прогноз по ожидаемой новости"
    if len(failed) == len(cfg.llm.pair):
        pytest.skip(f"обе модели не ответили: {', '.join(failed)}")
