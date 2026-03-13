from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from dateutil import parser as dt_parser

from playwright.async_api import BrowserContext, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from freelans_bot.adapters.base import BasePlatformAdapter
from freelans_bot.adapters.errors import SessionExpiredError
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

    async def fetch_new_leads(
        self,
        since: datetime | None,
        limit: int,
        *,
        max_pages: int | None = None,
    ) -> list[Lead]:
        feed_url = self.config["feed_url"]
        sel = self.config.get("selectors", {})
        card_sel = sel.get("card")
        if not card_sel:
            return []

        pagination_cfg = self.config.get("pagination", {})
        if not isinstance(pagination_cfg, dict):
            pagination_cfg = {}
        if max_pages is None:
            max_pages = int(pagination_cfg.get("max_pages", settings.max_pages_per_platform_scan))
        if max_pages < 1:
            max_pages = 1

        feed_timeout_ms = max(1_000, min(settings.playwright_timeout_ms, settings.playwright_feed_timeout_ms))
        cards_wait_timeout_ms = max(500, settings.playwright_cards_wait_timeout_ms)

        context = await self._new_context()
        page = await context.new_page()
        rows: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        try:
            for page_num in range(1, max_pages + 1):
                page_url = self._build_page_url(feed_url, page_num, pagination_cfg)
                await page.goto(page_url, wait_until="domcontentloaded", timeout=feed_timeout_ms)
                if await self._is_login_required(page=page, card_selector=card_sel):
                    raise SessionExpiredError(
                        f"SESSION_EXPIRED: platform={self.name} redirect/login detected on feed"
                    )
                try:
                    await page.wait_for_selector(card_sel, timeout=cards_wait_timeout_ms)
                except PlaywrightTimeoutError:
                    if await self._is_login_required(page=page, card_selector=card_sel):
                        raise SessionExpiredError(
                            f"SESSION_EXPIRED: platform={self.name} no access to feed, login required"
                        )
                    if page_num == 1:
                        return []
                    break

                page_rows = await self._extract_rows(page=page, selectors=sel, limit=limit)
                if not page_rows:
                    break

                new_count = 0
                for row in page_rows:
                    raw_url = (row.get("url") or "").strip()
                    if not raw_url:
                        continue
                    full_url = urljoin(feed_url, raw_url)
                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)
                    row["url"] = full_url
                    rows.append(row)
                    new_count += 1
                    if len(rows) >= limit:
                        break

                if len(rows) >= limit:
                    break
                if new_count == 0:
                    break
        except PlaywrightTimeoutError:
            return []
        finally:
            await self._close_context(context)

        leads: list[Lead] = []
        since_cmp = since if since and since.tzinfo else (since.replace(tzinfo=timezone.utc) if since else None)
        now_utc = datetime.now(timezone.utc)
        for row in rows:
            full_url = row["url"]
            body = row.get("description") or ""
            published_at, has_precise_time = self._parse_published_at(row.get("date", ""), now_utc)
            if since_cmp:
                # Filter by time only if listing timestamp includes precise time.
                # Many platforms expose only day-level dates ("сегодня"/"вчера"/dd.mm),
                # which would otherwise hide fresh leads.
                if has_precise_time and published_at <= since_cmp:
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

    async def _extract_rows(
        self,
        *,
        page: Any,
        selectors: dict[str, Any],
        limit: int,
    ) -> list[dict[str, str]]:
        return await page.evaluate(
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
                    "card": selectors.get("card"),
                    "title": selectors.get("title"),
                    "url": selectors.get("url"),
                    "description": selectors.get("description"),
                    "budget": selectors.get("budget"),
                    "date": selectors.get("date"),
                },
                "maxItems": limit,
            },
        )

    def _build_page_url(self, base_url: str, page_num: int, pagination_cfg: dict[str, Any]) -> str:
        if page_num <= 1:
            return base_url

        mode = str(pagination_cfg.get("mode", "query")).strip().lower()
        if mode == "template":
            template = str(pagination_cfg.get("template", "")).strip()
            if template:
                return template.format(page=page_num)

        param = str(pagination_cfg.get("param", "page")).strip() or "page"
        parsed = urlparse(base_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query[param] = str(page_num)
        new_query = urlencode(query, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    async def _is_login_required(self, *, page: Any, card_selector: str | None = None) -> bool:
        login_url = str(self.config.get("login_url") or "").strip()
        current_url = str(page.url or "").strip()

        if self._is_url_related_to_login(current_url, login_url):
            return True

        if card_selector:
            has_cards = await page.evaluate(
                "(selector) => document.querySelectorAll(selector).length > 0",
                card_selector,
            )
            if has_cards:
                return False

        return bool(
            await page.evaluate(
                """
                () => {
                  const hasPassword = !!document.querySelector("input[type='password']");
                  const hasLoginForm = !!document.querySelector(
                    "form[action*='login'], form[action*='auth'], input[name*='login'], input[name*='email']"
                  );
                  const txt = (document.body?.innerText || "").toLowerCase();
                  const hasLoginText =
                    txt.includes("войти") ||
                    txt.includes("вход") ||
                    txt.includes("авторизац") ||
                    txt.includes("log in") ||
                    txt.includes("sign in");
                  return hasPassword || (hasLoginForm && hasLoginText);
                }
                """
            )
        )

    def _is_url_related_to_login(self, current_url: str, login_url: str) -> bool:
        cur = current_url.strip().lower()
        login = login_url.strip().lower()
        if not cur or not login:
            return False
        if cur.startswith(login):
            return True
        parsed_cur = urlparse(cur)
        parsed_login = urlparse(login)
        if parsed_cur.netloc and parsed_login.netloc and parsed_cur.netloc != parsed_login.netloc:
            return False
        cur_path = parsed_cur.path or ""
        login_path = parsed_login.path or ""
        if login_path and cur_path.startswith(login_path):
            return True
        return any(token in cur for token in ("/login", "/signin", "/auth", "passport.yandex.ru/auth"))

    def _parse_published_at(self, raw_date: str, now_utc: datetime) -> tuple[datetime, bool]:
        text = " ".join((raw_date or "").strip().lower().split())
        if not text:
            return now_utc, False

        has_explicit_time = bool(re.search(r"(\d{1,2}):(\d{2})", text))

        m_minutes = re.search(r"(\d+)\s*мин", text)
        if m_minutes:
            return now_utc - timedelta(minutes=int(m_minutes.group(1))), True

        m_hours = re.search(r"(\d+)\s*час", text)
        if m_hours:
            return now_utc - timedelta(hours=int(m_hours.group(1))), True

        base_day: date | None = None
        if "сегодня" in text:
            base_day = now_utc.date()
        elif "вчера" in text:
            base_day = (now_utc - timedelta(days=1)).date()
        else:
            m_dm = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", text)
            if m_dm:
                day = int(m_dm.group(1))
                month = int(m_dm.group(2))
                year_raw = m_dm.group(3)
                year = now_utc.year if not year_raw else int(year_raw)
                if year < 100:
                    year += 2000
                try:
                    base_day = date(year, month, day)
                except ValueError:
                    base_day = None

        if base_day:
            m_time = re.search(r"(\d{1,2}):(\d{2})", text)
            hour = int(m_time.group(1)) if m_time else 0
            minute = int(m_time.group(2)) if m_time else 0
            dt = datetime(
                base_day.year,
                base_day.month,
                base_day.day,
                hour,
                minute,
                tzinfo=timezone.utc,
            )
            return dt, has_explicit_time

        try:
            parsed = dt_parser.parse(text, dayfirst=True, fuzzy=True)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc), has_explicit_time
        except Exception:
            return now_utc, False

    async def sync_profile(self, profile_data: dict[str, str]) -> tuple[bool, str]:
        profile_cfg = self.config.get("profile", {})
        field_selectors = profile_cfg.get("fields", {})
        if not isinstance(field_selectors, dict) or not field_selectors:
            return False, "Для платформы не настроены селекторы профиля"

        session_file = self._session_file()
        if not session_file.exists():
            return False, "Сессия не найдена. Сначала подключи аккаунт."

        target_url = (
            (profile_data.get("profile_url") or "").strip()
            or str(profile_cfg.get("edit_url") or "").strip()
            or str(self.config.get("login_url") or "").strip()
            or str(self.config.get("feed_url") or "").strip()
        )
        if not target_url:
            return False, "Не найден URL страницы профиля"

        context = await self._new_context()
        page = await context.new_page()
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=settings.playwright_timeout_ms)
            await page.wait_for_timeout(1000)
            if await self._is_login_required(
                page=page,
                card_selector=None,
            ):
                return False, f"SESSION_EXPIRED: platform={self.name} требуется повторный вход"

            updated: list[str] = []
            missing: list[str] = []
            failed: list[str] = []

            for field in ("name", "headline", "resume", "portfolio_urls", "rates"):
                value = (profile_data.get(field) or "").strip()
                selector = str(field_selectors.get(field) or "").strip()
                if not value or not selector:
                    continue

                try:
                    locator = page.locator(selector).first
                    if await locator.count() == 0:
                        missing.append(field)
                        continue
                    await locator.fill(value)
                    updated.append(field)
                except Exception:
                    failed.append(field)

            if not updated:
                return False, "Поля профиля не найдены на странице или пустые значения"

            save_selector = str(profile_cfg.get("save_button") or "").strip()
            save_clicked = False
            if save_selector:
                try:
                    save_btn = page.locator(save_selector).first
                    if await save_btn.count():
                        await save_btn.click()
                        save_clicked = True
                        await page.wait_for_timeout(1500)
                except Exception:
                    save_clicked = False

            await context.storage_state(path=str(session_file))

            parts = [f"Обновлено полей: {', '.join(updated)}"]
            if missing:
                parts.append(f"Не найдены селекторы: {', '.join(missing)}")
            if failed:
                parts.append(f"Ошибки заполнения: {', '.join(failed)}")
            if save_selector and not save_clicked:
                parts.append("Кнопка сохранения не найдена или не нажалась")
            return True, ". ".join(parts)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            await self._close_context(context)

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
            if await self._is_login_required(
                page=page,
                card_selector=None,
            ):
                return ApplyResult(
                    platform=self.name,
                    lead_url=lead.url,
                    ok=False,
                    message=f"SESSION_EXPIRED: platform={self.name} требуется повторный вход",
                )

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
