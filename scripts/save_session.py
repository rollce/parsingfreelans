#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from freelans_bot.config.platforms import load_platforms_config
from freelans_bot.config.settings import settings


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Open browser, login manually, save Playwright session")
    p.add_argument("--platform", default="", help="Platform key from platforms.yaml (e.g. flru)")
    p.add_argument("--login-url", default="", help="Optional login page URL override")
    p.add_argument("--list", action="store_true", help="Show available platforms and exit")
    return p.parse_args()


def print_platforms(platforms: dict[str, dict[str, Any]]) -> None:
    print("Available platforms:")
    for key, cfg in sorted(platforms.items()):
        display = cfg.get("display_name", key)
        login_url = cfg.get("login_url", cfg.get("feed_url", ""))
        print(f"- {key:14s} | {display:20s} | {login_url}")


async def main() -> None:
    args = parse_args()
    cfg_path = Path(__file__).resolve().parents[1] / "src" / "freelans_bot" / "config" / "platforms.yaml"
    platforms = load_platforms_config(cfg_path)

    if args.list:
        print_platforms(platforms)
        return

    if not args.platform:
        keys = ", ".join(sorted(platforms.keys()))
        raise SystemExit(f"Pass --platform. Available: {keys}")

    if args.platform not in platforms:
        keys = ", ".join(sorted(platforms.keys()))
        raise SystemExit(f"Unknown platform '{args.platform}'. Available: {keys}")

    cfg = platforms[args.platform]
    url = args.login_url or cfg.get("login_url") or cfg.get("feed_url")
    session_file = settings.sessions_path / cfg.get("session_file", f"{args.platform}.json")
    settings.sessions_path.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")

        print("")
        print(f"[{args.platform}] browser opened: {url}")
        print("1) Sign in manually in this browser window.")
        print("2) Complete captcha/2FA if prompted.")
        print("3) Ensure you are logged in (your account avatar/name is visible).")
        print("4) Return to this terminal and press ENTER.")
        input()

        await context.storage_state(path=str(session_file))
        await browser.close()

    print(f"Session saved: {session_file.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
