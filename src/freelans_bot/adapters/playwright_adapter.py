from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from dateutil import parser as dt_parser
from playwright.async_api import Browser, BrowserContext, Playwright, Route, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from freelans_bot.adapters.base import BasePlatformAdapter
from freelans_bot.adapters.errors import SessionExpiredError
from freelans_bot.config.settings import settings
from freelans_bot.core.models import ApplyResult, Lead
from freelans_bot.utils.text import detect_language


class PlaywrightPlatformAdapter(BasePlatformAdapter):
    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._browser_lock = asyncio.Lock()
        self._contexts_created_since_launch = 0
        self._browser_started_at_monotonic = 0.0

    def _session_file(self) -> Path:
        fname = self.config.get("session_file")
        if not fname:
            return settings.sessions_path / f"{self.name}.json"
        return settings.sessions_path / fname

    def _anti_bot_config(self) -> dict[str, Any]:
        cfg = self.config.get("anti_bot")
        if isinstance(cfg, dict):
            return cfg
        return {}

    def _resolve_proxy_settings(self, anti_bot_cfg: dict[str, Any]) -> dict[str, str] | None:
        nested_proxy = anti_bot_cfg.get("proxy")
        proxy_server = ""
        proxy_username = ""
        proxy_password = ""

        if isinstance(nested_proxy, dict):
            proxy_server = str(nested_proxy.get("server") or "").strip()
            proxy_username = str(nested_proxy.get("username") or "").strip()
            proxy_password = str(nested_proxy.get("password") or "").strip()

        proxy_server = str(anti_bot_cfg.get("proxy_server") or proxy_server or settings.playwright_proxy_server).strip()
        proxy_username = str(
            anti_bot_cfg.get("proxy_username") or proxy_username or settings.playwright_proxy_username
        ).strip()
        proxy_password = str(
            anti_bot_cfg.get("proxy_password") or proxy_password or settings.playwright_proxy_password
        ).strip()

        if not proxy_server:
            return None

        payload: dict[str, str] = {"server": proxy_server}
        if proxy_username:
            payload["username"] = proxy_username
        if proxy_password:
            payload["password"] = proxy_password
        return payload

    def _default_launch_args(self) -> list[str]:
        return [
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-backgrounding-occluded-windows",
            "--disable-features=Translate,BackForwardCache",
            "--mute-audio",
            "--no-default-browser-check",
            "--no-first-run",
        ]

    def _resolve_launch_args(self, anti_bot_cfg: dict[str, Any]) -> list[str]:
        args: list[str] = []
        seen: set[str] = set()

        def add_arg(raw: object) -> None:
            value = str(raw or "").strip()
            if not value or value in seen:
                return
            seen.add(value)
            args.append(value)

        for value in self._default_launch_args():
            add_arg(value)
        for value in settings.playwright_launch_args_list:
            add_arg(value)

        raw_args = anti_bot_cfg.get("launch_args")
        if isinstance(raw_args, list):
            for item in raw_args:
                add_arg(item)
        return args

    def _resolve_blocked_resource_types(self, anti_bot_cfg: dict[str, Any]) -> set[str]:
        block_cfg = anti_bot_cfg.get("block_resources")
        enabled = bool(settings.playwright_block_resources)
        blocked_types = list(settings.playwright_block_resource_types_list)

        if isinstance(block_cfg, bool):
            enabled = block_cfg
        elif isinstance(block_cfg, dict):
            if "enabled" in block_cfg:
                enabled = bool(block_cfg.get("enabled"))
            raw_types = block_cfg.get("resource_types")
            if isinstance(raw_types, list):
                blocked_types = [str(x or "").strip().lower() for x in raw_types if str(x or "").strip()]

        if not enabled:
            return set()

        valid_types = {
            "document",
            "stylesheet",
            "image",
            "media",
            "font",
            "script",
            "texttrack",
            "xhr",
            "fetch",
            "eventsource",
            "websocket",
            "manifest",
            "other",
        }
        return {item for item in blocked_types if item in valid_types}

    def _resolve_context_profile(self, anti_bot_cfg: dict[str, Any]) -> dict[str, Any]:
        user_agent = str(anti_bot_cfg.get("user_agent") or settings.playwright_default_user_agent).strip()
        locale = str(anti_bot_cfg.get("locale") or settings.playwright_locale).strip()
        timezone_id = str(anti_bot_cfg.get("timezone_id") or settings.playwright_timezone_id).strip()

        viewport_cfg = anti_bot_cfg.get("viewport")
        width = int(settings.playwright_viewport_width)
        height = int(settings.playwright_viewport_height)
        if isinstance(viewport_cfg, dict):
            with_width = viewport_cfg.get("width")
            with_height = viewport_cfg.get("height")
            if with_width is not None:
                try:
                    width = int(with_width)
                except (TypeError, ValueError):
                    width = int(settings.playwright_viewport_width)
            if with_height is not None:
                try:
                    height = int(with_height)
                except (TypeError, ValueError):
                    height = int(settings.playwright_viewport_height)

        width = max(900, min(2560, width))
        height = max(600, min(1600, height))

        payload: dict[str, Any] = {
            "user_agent": user_agent,
            "locale": locale or "ru-RU",
            "timezone_id": timezone_id or "Europe/Moscow",
            "viewport": {"width": width, "height": height},
        }
        return payload

    async def _apply_stealth(self, context: BrowserContext) -> None:
        script = """
        () => {
          Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
          Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
          Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
          Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
          window.chrome = window.chrome || { runtime: {} };
        }
        """
        await context.add_init_script(script)

    async def _maybe_human_delay(self, anti_bot_cfg: dict[str, Any]) -> None:
        enabled = bool(anti_bot_cfg.get("enabled"))
        if not enabled:
            return
        raw_min = anti_bot_cfg.get("jitter_min_ms", settings.playwright_anti_bot_jitter_min_ms)
        raw_max = anti_bot_cfg.get("jitter_max_ms", settings.playwright_anti_bot_jitter_max_ms)
        try:
            jitter_min_ms = int(raw_min)
        except (TypeError, ValueError):
            jitter_min_ms = int(settings.playwright_anti_bot_jitter_min_ms)
        try:
            jitter_max_ms = int(raw_max)
        except (TypeError, ValueError):
            jitter_max_ms = int(settings.playwright_anti_bot_jitter_max_ms)
        jitter_min_ms = max(0, min(10_000, jitter_min_ms))
        jitter_max_ms = max(jitter_min_ms, min(15_000, jitter_max_ms))
        delay_ms = random.randint(jitter_min_ms, jitter_max_ms)
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    def _should_recycle_browser(self) -> bool:
        context_limit = int(settings.playwright_browser_recycle_contexts)
        if context_limit > 0 and self._contexts_created_since_launch >= context_limit:
            return True
        age_limit_minutes = int(settings.playwright_browser_max_age_minutes)
        if age_limit_minutes > 0 and self._browser_started_at_monotonic > 0:
            age_seconds = time.monotonic() - self._browser_started_at_monotonic
            if age_seconds >= (age_limit_minutes * 60):
                return True
        return False

    async def _shutdown_browser_locked(self) -> None:
        browser = self._browser
        playwright = self._playwright
        self._browser = None
        self._playwright = None
        self._contexts_created_since_launch = 0
        self._browser_started_at_monotonic = 0.0

        if browser and browser.is_connected():
            with suppress(Exception):
                await browser.close()
        if playwright:
            with suppress(Exception):
                await playwright.stop()

    async def _ensure_browser(self, anti_bot_cfg: dict[str, Any]) -> Browser:
        if self._browser and self._playwright and self._browser.is_connected():
            return self._browser

        async with self._browser_lock:
            if self._browser and self._playwright and self._browser.is_connected():
                return self._browser
            if self._browser or self._playwright:
                await self._shutdown_browser_locked()

            playwright = await async_playwright().start()
            launch_payload: dict[str, Any] = {"headless": settings.playwright_headless}
            proxy_payload = self._resolve_proxy_settings(anti_bot_cfg)
            if proxy_payload:
                launch_payload["proxy"] = proxy_payload
            launch_args = self._resolve_launch_args(anti_bot_cfg)
            if launch_args:
                launch_payload["args"] = launch_args
            browser = await playwright.chromium.launch(**launch_payload)

            self._playwright = playwright
            self._browser = browser
            self._contexts_created_since_launch = 0
            self._browser_started_at_monotonic = time.monotonic()
            return browser

    async def _apply_resource_blocking(self, context: BrowserContext, anti_bot_cfg: dict[str, Any]) -> None:
        blocked_types = self._resolve_blocked_resource_types(anti_bot_cfg)
        if not blocked_types:
            return

        async def route_handler(route: Route) -> None:
            resource_type = str(route.request.resource_type or "").strip().lower()
            if resource_type in blocked_types:
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", route_handler)

    async def _new_context(self) -> BrowserContext:
        settings.sessions_path.mkdir(parents=True, exist_ok=True)
        session_file = self._session_file()
        anti_bot_cfg = self._anti_bot_config()
        browser = await self._ensure_browser(anti_bot_cfg)
        context_payload = self._resolve_context_profile(anti_bot_cfg)
        if session_file.exists():
            context_payload["storage_state"] = str(session_file)
        context = await browser.new_context(**context_payload)
        if settings.playwright_stealth_enabled or bool(anti_bot_cfg.get("enabled")):
            await self._apply_stealth(context)
        await self._apply_resource_blocking(context, anti_bot_cfg)
        self._contexts_created_since_launch += 1
        return context

    async def _close_context(self, context: BrowserContext) -> None:
        with suppress(Exception):
            await context.close()
        if not self._should_recycle_browser():
            return
        async with self._browser_lock:
            if self._should_recycle_browser():
                await self._shutdown_browser_locked()

    async def close(self) -> None:
        async with self._browser_lock:
            await self._shutdown_browser_locked()

    async def fetch_new_leads(
        self,
        since: datetime | None,
        limit: int,
        *,
        max_pages: int | None = None,
    ) -> list[Lead]:
        anti_bot_cfg = self._anti_bot_config()
        feed_urls = self._collect_feed_urls()
        if not feed_urls:
            return []
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
        page: Any | None = None
        rows: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        try:
            page = await context.new_page()
            for feed_url in feed_urls:
                for page_num in range(1, max_pages + 1):
                    page_url = self._build_page_url(feed_url, page_num, pagination_cfg)
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=feed_timeout_ms)
                    await self._maybe_human_delay(anti_bot_cfg)
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
                            break
                        break

                    page_rows = await self._extract_rows(page=page, selectors=sel, limit=limit)
                    if not page_rows:
                        break

                    if len(rows) >= limit:
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
                    await self._maybe_human_delay(anti_bot_cfg)
                if len(rows) >= limit:
                    break
        except PlaywrightTimeoutError:
            return []
        finally:
            if page:
                with suppress(Exception):
                    await page.close()
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

    def _collect_feed_urls(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()

        def add_url(value: object) -> None:
            raw = str(value or "").strip()
            if not raw or raw in seen:
                return
            seen.add(raw)
            out.append(raw)

        add_url(self.config.get("feed_url"))
        raw_list = self.config.get("feed_urls")
        if isinstance(raw_list, list):
            for item in raw_list:
                add_url(item)
        return out

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
        anti_bot_cfg = self._anti_bot_config()
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
        page: Any | None = None
        try:
            page = await context.new_page()
            await page.goto(target_url, wait_until="domcontentloaded", timeout=settings.playwright_timeout_ms)
            await self._maybe_human_delay(anti_bot_cfg)
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
            if page:
                with suppress(Exception):
                    await page.close()
            await self._close_context(context)

    async def apply(self, lead: Lead, proposal_text: str) -> ApplyResult:
        anti_bot_cfg = self._anti_bot_config()
        apply_cfg = self.config.get("apply", {})
        if not apply_cfg:
            return ApplyResult(
                platform=self.name,
                lead_url=lead.url,
                ok=False,
                message="Apply flow is not configured for this platform",
            )

        context = await self._new_context()
        page: Any | None = None
        try:
            page = await context.new_page()
            await page.goto(lead.url, wait_until="domcontentloaded", timeout=settings.playwright_timeout_ms)
            await self._maybe_human_delay(anti_bot_cfg)
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
            if page:
                with suppress(Exception):
                    await page.close()
            await self._close_context(context)
