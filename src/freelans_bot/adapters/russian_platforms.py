from __future__ import annotations

from pathlib import Path

from freelans_bot.adapters.base import BasePlatformAdapter
from freelans_bot.adapters.playwright_adapter import PlaywrightPlatformAdapter
from freelans_bot.config.platforms import load_platforms_config
from freelans_bot.config.settings import settings


def build_russian_adapters() -> list[BasePlatformAdapter]:
    cfg_path = Path(__file__).resolve().parents[1] / "config" / "platforms.yaml"
    all_cfg = load_platforms_config(cfg_path)

    switches = {
        "flru": settings.enable_flru,
        "freelance_ru": settings.enable_freelance_ru,
        "kwork": settings.enable_kwork,
        "workzilla": settings.enable_workzilla,
        "youdo": settings.enable_youdo,
        "yandex_uslugi": settings.enable_yandex_uslugi,
        "freelancejob": settings.enable_freelancejob,
    }

    adapters: list[BasePlatformAdapter] = []
    for key, enabled in switches.items():
        if not enabled:
            continue
        config = all_cfg.get(key)
        if not config:
            continue
        adapters.append(PlaywrightPlatformAdapter(name=key, config=config))
    return adapters
