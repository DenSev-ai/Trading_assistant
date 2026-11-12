"""Загрузка и валидация конфигурации (settings.yaml, keywords.yaml, .env)."""
from __future__ import annotations

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """Ошибка загрузки или валидации конфигурации."""


class Keywords(BaseModel):
    include: list[str]
    exclude: list[str]


class ScheduleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_from: str
    stop_at: str
    non_working_dates: list[str] = []
    skip_weekends: bool = True


class PollingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cycle_interval_sec: int
    backtest_interval_sec: int
    http_timeout_sec: int

    @model_validator(mode="after")
    def _check_cycle_interval(self) -> Self:
        if self.cycle_interval_sec < 0:
            raise ValueError("cycle_interval_sec must be >= 0")
        return self


class NewsSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    url: str
    fulltext: str
    enabled: bool


class NewsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_hours: int
    max_items_per_feed: int
    min_fulltext_chars: int
    max_fulltext_bytes: int
    cluster_window_hours: int
    cluster_similarity: int
    user_agent: str
    sources: list[NewsSource]


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    price_in: float
    price_out: float
    reasoning_effort: str | None = None


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    prompt_version: str
    temperature: float
    max_tokens: int
    candidate_budget_share: float
    max_text_chars: int
    timeout_sec: int
    retry_pauses_sec: list[float]
    concurrency: int
    daily_budget_usd: float
    pair: list[ModelSpec]
    candidates_parallel: int
    candidates: list[ModelSpec]

    @model_validator(mode="after")
    def _check_pair(self) -> Self:
        if len(self.pair) != 2:
            raise ValueError("llm.pair must contain exactly 2 models")
        prefixes = {m.id.split("/", 1)[0] for m in self.pair}
        if len(prefixes) != 2:
            raise ValueError("модели пары должны иметь разные префиксы (до '/')")
        return self


class SignalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_confidence: int
    min_severity: int
    min_move_pct: float
    max_horizon_days: int
    magnitude_tolerance: float
    priority_move_pct: float
    priority_fast_days: int

    @model_validator(mode="after")
    def _check_thresholds(self) -> Self:
        if self.min_move_pct <= 0:
            raise ValueError("signal.min_move_pct must be > 0")
        if not 0 <= self.magnitude_tolerance < 1:
            raise ValueError("signal.magnitude_tolerance must be in [0, 1)")
        if self.priority_move_pct < self.min_move_pct:
            raise ValueError("signal.priority_move_pct must be >= signal.min_move_pct")
        if self.max_horizon_days < 1:
            raise ValueError("signal.max_horizon_days must be >= 1")
        return self


class EvaluationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_n: int
    min_success_pct: float

    @model_validator(mode="after")
    def _check_limits(self) -> Self:
        if self.sample_n < 1:
            raise ValueError("evaluation.sample_n must be >= 1")
        if not 0 < self.min_success_pct <= 100:
            raise ValueError("evaluation.min_success_pct must be in (0, 100]")
        return self


class TelegramSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    antispam_hours: int
    daily_alert_limit: int
    stat_min_n: int
    stat_ok_n: int
    send_pause_sec: int


class ReportsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    daily_time: str
    weekly_weekday: int
    weekly_time: str
    event_reports: bool


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str
    data_dir: str
    log_level: str
    schedule: ScheduleSettings
    polling: PollingSettings
    news: NewsSettings
    llm: LLMSettings
    signal: SignalSettings
    evaluation: EvaluationSettings
    telegram: TelegramSettings
    reports: ReportsSettings


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
    OPENROUTER_API_KEY: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_IDS: str = ""
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 465
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    REPORT_EMAIL_TO: str | None = None


def load_settings(path: Path = Path("config/settings.yaml")) -> AppSettings:
    """Загрузить и валидировать config/settings.yaml. При любой ошибке — ConfigError."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"не прочитать конфиг {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"не распарсить конфиг {path}: {exc}") from exc
    try:
        return AppSettings.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError и прочие
        raise ConfigError(f"конфиг {path} не прошёл валидацию: {exc}") from exc


def load_keywords(path: Path) -> Keywords:
    """Загрузить include/exclude подстроки из config/keywords.yaml."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"не прочитать ключевые слова {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"не распарсить ключевые слова {path}: {exc}") from exc
    return Keywords(
        include=list(data.get("include", [])),
        exclude=list(data.get("exclude", [])),
    )


def env_checklist(env: EnvSettings) -> tuple[list[str], bool]:
    """Чек-лист переменных .env: (строки, всё ли обязательное на месте). Значения не печатаются."""
    lines: list[str] = []
    ok = True

    def line(name: str, required: bool, value: str | None) -> None:
        nonlocal ok
        if value:
            lines.append(f"[OK] {name}")
        elif required:
            lines.append(f"[НЕТ] {name}")
            ok = False
        else:
            lines.append(f"[ОПЦИОНАЛЬНО] {name}")

    line("OPENROUTER_API_KEY", True, env.OPENROUTER_API_KEY)
    line("TELEGRAM_BOT_TOKEN", False, env.TELEGRAM_BOT_TOKEN)
    line("TELEGRAM_CHAT_IDS", False, env.TELEGRAM_CHAT_IDS)
    line("SMTP_HOST", False, env.SMTP_HOST)
    line("SMTP_PORT", False, str(env.SMTP_PORT))
    line("SMTP_USER", False, env.SMTP_USER)
    line("SMTP_PASSWORD", False, env.SMTP_PASSWORD)
    line("REPORT_EMAIL_TO", False, env.REPORT_EMAIL_TO)
    return lines, ok
