from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    data_dir: Path = ROOT / os.getenv("MERIDIAN_DATA_DIR", "data")
    blob_read_write_token: str | None = os.getenv("BLOB_READ_WRITE_TOKEN")
    blob_access: str = os.getenv("MERIDIAN_BLOB_ACCESS", "private")
    timezone: str = os.getenv("MERIDIAN_TIMEZONE", "Asia/Shanghai")
    scheduler_enabled: bool = _bool("MERIDIAN_SCHEDULER_ENABLED", True)
    http_timeout: float = float(os.getenv("MERIDIAN_HTTP_TIMEOUT", "25"))
    news_limit: int = int(os.getenv("MERIDIAN_NEWS_LIMIT", "8"))
    telegram_bot_token: str | None = os.getenv("MERIDIAN_TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = os.getenv("MERIDIAN_TELEGRAM_CHAT_ID")
    webhook_urls: tuple[str, ...] = tuple(filter(None, (item.strip() for item in os.getenv("MERIDIAN_WEBHOOK_URLS", "").split(","))))
    public_base_url: str = os.getenv("MERIDIAN_PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    admin_token: str | None = os.getenv("MERIDIAN_ADMIN_TOKEN")
    cron_secret: str | None = os.getenv("CRON_SECRET")
    professional_data_url: str | None = os.getenv("MERIDIAN_PRO_DATA_URL")
    professional_data_token: str | None = os.getenv("MERIDIAN_PRO_DATA_TOKEN")
    coinglass_api_key: str | None = os.getenv("COINGLASS_API_KEY")
    editor_enabled: bool = _bool("MERIDIAN_EDITOR_ENABLED", False)
    deepseek_api_key: str | None = os.getenv("DEEPSEEK_API_KEY")
    deepseek_base_url: str = os.getenv("MERIDIAN_DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    standard_editor_model: str = os.getenv("MERIDIAN_STANDARD_MODEL", "deepseek-flash")
    article_editor_model: str = os.getenv("MERIDIAN_ARTICLE_MODEL", "deepseek-flash")
    editor_timeout: float = float(os.getenv("MERIDIAN_EDITOR_TIMEOUT", "260"))
    insight_max_tokens: int = int(os.getenv("MERIDIAN_INSIGHT_MAX_TOKENS", "20000"))
    insight_short_max_tokens: int = int(os.getenv("MERIDIAN_INSIGHT_SHORT_MAX_TOKENS", "10000"))
    insight_task_timeout: float = float(os.getenv("MERIDIAN_INSIGHT_TASK_TIMEOUT", "275"))
    insight_fragment_timeout: float = float(os.getenv("MERIDIAN_INSIGHT_FRAGMENT_TIMEOUT", "110"))
    insight_fragment_max_tokens: int = int(os.getenv("MERIDIAN_INSIGHT_FRAGMENT_MAX_TOKENS", "5000"))
    insight_fragment_attempts: int = int(os.getenv("MERIDIAN_INSIGHT_FRAGMENT_ATTEMPTS", "2"))
    insight_source_limit: int = int(os.getenv("MERIDIAN_INSIGHT_SOURCE_LIMIT", "14"))


settings = Settings()
