from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import BrowserContext, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from freelans_bot.adapters.base import BasePlatformAdapter
from freelans_bot.config.settings import settings
from freelans_bot.core.models import ApplyResult, Lead
from freelans_bot.utils.text import detect_language


class PlaywrightPlatformAdapter(BasePlatformAdapter):
    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = config

    def _session_file(self) -> Path:
        fname = self.config.get("session_file")
        if not fname:
            return settings.sessions_path / f"{self.name}.json"
        return settings.sessions_path / fname

    async def _new_context(self) -> BrowserContext:
        settings.sessions_path.mkdir(parents=True, exist_ok=True)
        session_file = self._session_file()
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=settings.playwright_headless)
        storage_state = str(session_file) if session_file.exists() else None
        context = await browser.new_context(storage_state=storage_state)
        context._playwright_handle = playwright  # type: ignore[attr-defined]
        return context

    async def _close_context(self, context: BrowserContext) -> None:
        browser = context.browser
        playwright = getattr(context, "_playwright_handle", None)
        await context.close()
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()

    async def fetch_new_leads(self, since: datetime | None, limit: int) -> list[Lead]:
        feed_url = self.config["feed_url"]
        sel = self.config.get("selectors", {})
        card_sel = sel.get("card")
        if not card_sel:
            return []

        context = await self._new_context()
        page = await context.new_page()
        try:
            await page.goto(feed_url, wait_until="domcontentloaded", timeout=settings.playwright_timeout_ms)
            await page.wait_for_selector(card_sel, timeout=settings.playwright_timeout_ms)
            rows: list[dict[str, str]] = await page.evaluate(
                """
                (payload) => {
                  const selectors = payload.selectors;
                  const maxItems = payload.maxItems;
                  const cards = Array.from(document.querySelectorAll(selectors.card)).slice(0, maxItems);
                  return cards.map((card) => {
                    const q = (selector) => selector ? card.querySelector(selector) : null;
                    const titleEl = q(selectors.title);
                    const urlEl = q(selectors.url) || titleEl;
                    const descEl = q(selectors.description);
                    const budgetEl = q(selectors.budget);
                    const dateEl = q(selectors.date);
                    return {
                      title: (titleEl?.textContent || '').trim(),
                      url: (urlEl?.getAttribute('href') || '').trim(),
                      description: (descEl?.textContent || '').trim(),
                      budget: (budgetEl?.textContent || '').trim(),
                      date: (dateEl?.textContent || '').trim(),
                    };
                  }).filter(x => x.title && x.url);
                }
                """,
                {
                    "selectors": {
                        "card": card_sel,
                        "title": sel.get("title"),
                        "url": sel.get("url"),
                        "description": sel.get("description"),
                        "budget": sel.get("budget"),
                        "date": sel.get("date"),
                    },
                    "maxItems": limit,
                },
            )
        except PlaywrightTimeoutError:
            return []
        finally:
            await self._close_context(context)

        leads: list[Lead] = []
        for row in rows:
            full_url = urljoin(feed_url, row["url"])
            body = row.get("description") or ""
            published_at = datetime.now(timezone.utc)
            if since and published_at <= since:
                continue
            payload = f"{self.name}|{full_url}|{row['title']}"
            external_id = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
            language = detect_language(f"{row['title']}\n{body}")
            leads.append(
                Lead(
                    platform=self.name,
                    title=row["title"],
                    url=full_url,
                    description=body,
                    budget=row.get("budget") or None,
                    language=language,
                    external_id=external_id,
                    published_at=published_at,
                    meta={"raw_date": row.get("date", "")},
                )
            )

        return leads

    async def apply(self, lead: Lead, proposal_text: str) -> ApplyResult:
        apply_cfg = self.config.get("apply", {})
        if not apply_cfg:
            return ApplyResult(
                platform=self.name,
                lead_url=lead.url,
                ok=False,
                message="Apply flow is not configured for this platform",
            )

        context = await self._new_context()
        page = await context.new_page()
        try:
            await page.goto(lead.url, wait_until="domcontentloaded", timeout=settings.playwright_timeout_ms)

            apply_button = apply_cfg.get("apply_button")
            if apply_button:
                btn = page.locator(apply_button).first
                if await btn.count():
                    await btn.click()

            textarea = apply_cfg.get("proposal_textarea")
            if not textarea:
                return ApplyResult(
                    platform=self.name,
                    lead_url=lead.url,
                    ok=False,
                    message="proposal_textarea selector is missing",
                )

            box = page.locator(textarea).first
            if await box.count() == 0:
                return ApplyResult(
                    platform=self.name,
                    lead_url=lead.url,
                    ok=False,
                    message="proposal textarea not found",
                )

            await box.fill(proposal_text)

            submit_selector = apply_cfg.get("submit_button")
            if not submit_selector:
                return ApplyResult(
                    platform=self.name,
                    lead_url=lead.url,
                    ok=False,
                    message="submit_button selector is missing",
                )

            submit_btn = page.locator(submit_selector).first
            if await submit_btn.count() == 0:
                return ApplyResult(
                    platform=self.name,
                    lead_url=lead.url,
                    ok=False,
                    message="submit button not found",
                )

            await submit_btn.click()
            await page.wait_for_timeout(2000)
            proposal_url = page.url

            chat_url = None
            chat_selector = apply_cfg.get("chat_link")
            if chat_selector:
                el = page.locator(chat_selector).first
                if await el.count():
                    href = await el.get_attribute("href")
                    if href:
                        chat_url = urljoin(proposal_url, href)

            await context.storage_state(path=str(self._session_file()))
            return ApplyResult(
                platform=self.name,
                lead_url=lead.url,
                ok=True,
                message="Proposal submitted",
                proposal_url=proposal_url,
                chat_url=chat_url,
            )
        except Exception as exc:
            return ApplyResult(
                platform=self.name,
                lead_url=lead.url,
                ok=False,
                message=f"{type(exc).__name__}: {exc}",
            )
        finally:
            await self._close_context(context)
