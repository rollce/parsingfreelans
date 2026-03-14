from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_APP_ENV = os.getenv("APP_ENV", "dev").strip().lower()
_ENV_FILE = ".env" if _APP_ENV != "prod" else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    database_path: str = "./data/freelans.db"
    poll_interval_seconds: int = 5
    min_score_to_apply: float = 0.45
    max_leads_per_platform: int = 40
    max_pages_per_platform_scan: int = 8
    max_applies_per_cycle: int = 30
    auto_apply: bool = True
    auto_apply_hour_limit: int = 6
    auto_apply_day_limit: int = 30

    telegram_bot_token: str
    telegram_chat_id: str
    telegram_control_enabled: bool = True
    telegram_control_poll_timeout: int = 20
    telegram_notify_batch_size: int = 8
    telegram_notify_retry_after_seconds: int = 45
    telegram_notify_max_attempts: int = 200
    telegram_platform_burst_limit: int = 12
    telegram_platform_burst_window_minutes: int = 10

    llm_provider: str = "openrouter"

    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"

    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-4.1-mini"
    openrouter_site_url: str = ""
    openrouter_app_name: str = "freelans-bot"

    proposal_validation_enabled: bool = True
    proposal_min_chars: int = 280
    proposal_max_chars: int = 2200
    proposal_similarity_threshold: float = 0.93
    proposal_similarity_window: int = 30
    proposal_banned_phrases: str = "guaranteed #1,100% guaranteed,предоплата на карту,только сегодня скидка"
    validator_spike_alert_enabled: bool = True
    validator_spike_threshold: int = 10
    validator_spike_window_minutes: int = 15
    validator_share_alert_enabled: bool = True
    validator_share_alert_threshold: float = 0.35
    validator_share_alert_min_attempts: int = 8

    target_languages: str = "ru"
    keywords: str = "telegram bot,python,ai automation"
    focus_keywords: str = (
        "python,fastapi,django,flask,"
        "telegram,телеграм,telegram bot,чат-бот,чатбот,"
        "website,web site,web app,сайт,веб-сайт,лендинг,"
        "automation,автоматизация,business process,workflow,"
        "parser,парсер,парсинг,scraping,scraper,"
        "api integration,интеграция api"
    )
    negative_keywords: str = "casino,adult"
    strict_topic_filter: bool = True

    freelancer_profile: str = ""
    portfolio_urls: str = ""

    playwright_headless: bool = True
    playwright_timeout_ms: int = 45_000
    playwright_feed_timeout_ms: int = 15_000
    playwright_cards_wait_timeout_ms: int = 5_000
    playwright_stealth_enabled: bool = True
    playwright_default_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    playwright_locale: str = "ru-RU"
    playwright_timezone_id: str = "Europe/Moscow"
    playwright_viewport_width: int = 1280
    playwright_viewport_height: int = 720
    playwright_proxy_server: str = ""
    playwright_proxy_username: str = ""
    playwright_proxy_password: str = ""
    playwright_anti_bot_jitter_min_ms: int = 250
    playwright_anti_bot_jitter_max_ms: int = 1200
    playwright_block_resources: bool = True
    playwright_block_resource_types: str = "image,media,font"
    playwright_reuse_browser: bool = False
    playwright_browser_recycle_contexts: int = 24
    playwright_browser_max_age_minutes: int = 30
    playwright_launch_args: str = ""
    sessions_dir: str = "./state"
    db_backup_enabled: bool = False
    db_backup_dir: str = "./backups"
    db_backup_hour_local: int = 4
    db_backup_retention_days: int = 14
    events_cleanup_enabled: bool = True
    events_cleanup_hour_local: int = 5
    events_retention_days: int = 30
    events_max_rows: int = 5000
    leads_cleanup_enabled: bool = True
    leads_cleanup_hour_local: int = 6
    leads_retention_days: int = 45
    stale_pending_alert_enabled: bool = True
    stale_pending_alert_threshold: int = 40
    stale_pending_alert_days: int = 45
    delivery_health_alert_enabled: bool = True
    delivery_health_window_minutes: int = 30
    delivery_health_alert_threshold: float = 0.4
    delivery_health_alert_min_attempts: int = 10
    delivery_health_alert_cooldown_minutes: int = 30
    delivery_streak_alert_enabled: bool = True
    delivery_streak_alert_threshold: int = 8
    telegram_heartbeat_enabled: bool = True
    telegram_heartbeat_interval_minutes: int = 15
    telegram_heartbeat_fail_threshold: int = 3
    pending_reanimate_enabled: bool = True
    pending_reanimate_interval_minutes: int = 30
    pending_reanimate_max_runs_per_day: int = 12
    pending_reanimate_min_locked: int = 3
    pending_queue_alert_enabled: bool = True
    pending_queue_alert_threshold: int = 60
    no_new_leads_alert_enabled: bool = True
    no_new_leads_alert_minutes: int = 45
    worker_stall_alert_enabled: bool = True
    worker_stall_alert_minutes: int = 12
    platform_failover_enabled: bool = True
    platform_failover_error_streak: int = 3
    platform_failover_skip_minutes: int = 30
    platform_failover_sla_alert_enabled: bool = True
    platform_failover_sla_threshold: int = 2

    enable_flru: bool = True
    enable_freelance_ru: bool = True
    enable_kwork: bool = True
    enable_workzilla: bool = True
    enable_youdo: bool = True
    enable_yandex_uslugi: bool = True
    enable_freelancejob: bool = True

    @property
    def keyword_list(self) -> list[str]:
        return [x.strip().lower() for x in self.keywords.split(",") if x.strip()]

    @property
    def negative_keyword_list(self) -> list[str]:
        return [x.strip().lower() for x in self.negative_keywords.split(",") if x.strip()]

    @property
    def focus_keyword_list(self) -> list[str]:
        return [x.strip().lower() for x in self.focus_keywords.split(",") if x.strip()]

    @property
    def language_list(self) -> set[str]:
        return {x.strip().lower() for x in self.target_languages.split(",") if x.strip()}

    @property
    def proposal_banned_list(self) -> list[str]:
        return [x.strip().lower() for x in self.proposal_banned_phrases.split(",") if x.strip()]

    @property
    def portfolio_list(self) -> list[str]:
        return [x.strip() for x in self.portfolio_urls.split(",") if x.strip()]

    @property
    def playwright_block_resource_types_list(self) -> list[str]:
        return [x.strip().lower() for x in self.playwright_block_resource_types.split(",") if x.strip()]

    @property
    def playwright_launch_args_list(self) -> list[str]:
        return [x.strip() for x in self.playwright_launch_args.split(",") if x.strip()]

    @property
    def database_file(self) -> Path:
        return Path(self.database_path)

    @property
    def sessions_path(self) -> Path:
        return Path(self.sessions_dir)

    @property
    def db_backup_path(self) -> Path:
        return Path(self.db_backup_dir)


settings = Settings()
