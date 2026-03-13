from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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
    sessions_dir: str = "./state"

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
    def database_file(self) -> Path:
        return Path(self.database_path)

    @property
    def sessions_path(self) -> Path:
        return Path(self.sessions_dir)


settings = Settings()
