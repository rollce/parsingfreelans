from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from freelans_bot.adapters.base import BasePlatformAdapter
from freelans_bot.adapters.russian_platforms import build_russian_adapters
from freelans_bot.config.platforms import load_platforms_config
from freelans_bot.config.settings import settings
from freelans_bot.core.orchestrator import Orchestrator
from freelans_bot.integrations.telegram import TelegramNotifier
from freelans_bot.services.proposal import ProposalService
from freelans_bot.services.scoring import LeadScorer
from freelans_bot.storage.db import SQLiteStore
from freelans_bot.utils.text import compact


class Worker:
    FILTER_RUNTIME_KEYS = {
        "min_score": "filter:min_score",
        "keywords": "filter:keywords",
        "negative_keywords": "filter:negative_keywords",
    }

    FILTER_INPUT_HINTS = {
        "min_score": "Отправь число от 0 до 1. Например: 0.45\nДля сброса: default",
        "keywords": "Отправь ключевые слова через запятую или с новой строки.\nДля сброса: default",
        "negative_keywords": "Отправь минус-слова через запятую или с новой строки.\nДля сброса: default",
    }

    FILTER_LABELS = {
        "min_score": "Минимальный score",
        "keywords": "Ключевые слова",
        "negative_keywords": "Минус-слова",
    }

    SCAN_RUNTIME_KEYS = {
        "interval_seconds": "scan:interval_seconds",
        "max_pages": "scan:max_pages",
        "max_leads": "scan:max_leads",
        "burst_limit": "notify:burst_limit",
        "burst_window_minutes": "notify:burst_window_minutes",
    }

    SCAN_INPUT_HINTS = {
        "interval_seconds": "Отправь интервал в секундах (3..120).\nДля сброса: default",
        "max_pages": "Отправь глубину страниц на биржу (1..30).\nДля сброса: default",
        "max_leads": "Отправь лимит лидов на биржу за цикл (1..200).\nДля сброса: default",
        "burst_limit": "Отправь лимит anti-flood (0..200 лидов, 0 = выкл).\nДля сброса: default",
        "burst_window_minutes": "Отправь окно anti-flood в минутах (1..180).\nДля сброса: default",
    }

    SCAN_LABELS = {
        "interval_seconds": "Интервал сканирования",
        "max_pages": "Глубина страниц",
        "max_leads": "Лимит лидов",
        "burst_limit": "Лимит anti-flood",
        "burst_window_minutes": "Окно anti-flood",
    }

    LANGUAGE_RUNTIME_KEY = "filter:language_mode"
    LANGUAGE_MODES = {
        "ru": ("ru",),
        "en": ("en",),
        "mixed": ("ru", "en"),
    }
    LANGUAGE_LABELS = {
        "ru": "Только RU",
        "en": "Только EN",
        "mixed": "RU + EN",
    }

    GLOBAL_PROFILE_HINTS = {
        "name": "Отправь имя для профиля.",
        "resume": "Отправь текст общего резюме.",
        "avatar_url": "Отправь ссылку на аватар.",
        "portfolio_urls": "Отправь ссылки портфолио через запятую или с новой строки.",
    }

    GLOBAL_PROFILE_LABELS = {
        "name": "Имя",
        "resume": "Резюме",
        "avatar_url": "Аватар",
        "portfolio_urls": "Портфолио",
    }

    PLATFORM_PROFILE_HINTS = {
        "name": "Имя на площадке.",
        "headline": "Заголовок профиля.",
        "resume": "Описание профиля на площадке.",
        "portfolio_urls": "Ссылки портфолио через запятую или с новой строки.",
        "rates": "Ставки (фикс/почасовая).",
        "profile_url": "URL страницы редактирования профиля на этой площадке.",
    }

    PLATFORM_PROFILE_LABELS = {
        "name": "Имя",
        "headline": "Заголовок",
        "resume": "Описание",
        "portfolio_urls": "Портфолио",
        "rates": "Ставки",
        "profile_url": "URL профиля",
    }

    def __init__(self) -> None:
        self.store = SQLiteStore(settings.database_file)
        self.notifier = TelegramNotifier()
        self.orchestrator = Orchestrator(
            adapters=build_russian_adapters(),
            store=self.store,
            scorer=LeadScorer(),
            proposal_service=ProposalService(),
            notifier=self.notifier,
        )

        cfg_path = Path(__file__).resolve().parent / "config" / "platforms.yaml"
        self.platforms_cfg = load_platforms_config(cfg_path)
        self.platform_defaults = {
            "flru": settings.enable_flru,
            "freelance_ru": settings.enable_freelance_ru,
            "kwork": settings.enable_kwork,
            "workzilla": settings.enable_workzilla,
            "youdo": settings.enable_youdo,
            "yandex_uslugi": settings.enable_yandex_uslugi,
            "freelancejob": settings.enable_freelancejob,
        }

        self._stop_event = asyncio.Event()
        self._run_now_event = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._control_task: asyncio.Task | None = None
        self._updates_offset: int | None = None

        self.paused: bool = False
        self.auto_apply: bool = settings.auto_apply

        self._pending_profile_input: dict[str, str] = {}
        self._rotation_index: int = 0
        self._display_tz = ZoneInfo("Europe/Moscow")

    async def start(self) -> None:
        await self.store.init()
        self.paused = await self.store.get_runtime_flag("paused", default=False)
        self.auto_apply = await self.store.get_runtime_flag("auto_apply", default=settings.auto_apply)

        if settings.telegram_control_enabled:
            with suppress(Exception):
                await self.notifier.bot.delete_webhook(drop_pending_updates=False)
            await self._bootstrap_updates_offset()

        self._worker_task = asyncio.create_task(self._loop(), name="freelans-worker")
        if settings.telegram_control_enabled:
            self._control_task = asyncio.create_task(
                self._control_loop(),
                name="freelans-telegram-control",
            )

        await self._send_main_menu(
            "Бот запущен.\n"
            f"Пауза: {self._yes_no(self.paused)}\n"
            f"Автоотклик: {self._yes_no(self.auto_apply)}\n\n"
            "Напиши любое сообщение, чтобы открыть меню."
        )

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            started_at = time.monotonic()
            try:
                if self._run_now_event.is_set():
                    self._run_now_event.clear()
                    await self._run_cycle(trigger="manual")
                elif not self.paused:
                    await self._run_cycle(trigger="timer")
                await self._dispatch_pending_lead_notifications()
                elapsed = time.monotonic() - started_at
                await self._wait_for_next_tick(elapsed_seconds=elapsed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.store.record_event(
                    None,
                    "worker_error",
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
                await self.notifier.send_text(f"Ошибка воркера: {type(exc).__name__}: {exc}")
                await asyncio.sleep(2)

    async def _run_cycle(self, trigger: str) -> None:
        enabled_adapters = await self._get_enabled_adapters()
        adapters = enabled_adapters
        if trigger == "timer" and enabled_adapters:
            idx = self._rotation_index % len(enabled_adapters)
            adapters = [enabled_adapters[idx]]
            self._rotation_index = (idx + 1) % len(enabled_adapters)

        if not adapters:
            await self.store.record_event(
                None,
                "cycle_no_adapters",
                {"trigger": trigger},
            )
            return
        profile = await self.store.get_profile()
        profile_text = (profile.get("resume") or settings.freelancer_profile).strip()
        portfolio_urls = self._portfolio_urls(profile.get("portfolio_urls", "")) or settings.portfolio_list
        platform_profiles = await self.store.get_all_platform_profiles()
        filters = await self._get_effective_filters()
        scan_settings = await self._get_effective_scan_settings()
        language_mode = await self._get_effective_language_mode()
        apply_limits = await self._get_auto_apply_limits_snapshot()
        min_score_to_apply = float(filters["min_score"])
        self.orchestrator.scorer = LeadScorer(
            keywords=list(filters["keywords"]),
            negative_keywords=list(filters["negative_keywords"]),
            focus_keywords=settings.focus_keyword_list,
            strict_topic_filter=settings.strict_topic_filter,
            target_languages=set(language_mode["languages"]),
        )
        runtime_before = await self.store.get_platform_runtime()

        summary = await self.orchestrator.run_cycle_with_options(
            auto_apply=self.auto_apply,
            auto_generate_drafts=self.auto_apply,
            adapters=adapters,
            profile_text=profile_text,
            portfolio_urls=portfolio_urls,
            platform_profiles=platform_profiles,
            min_score_to_apply=min_score_to_apply,
            max_leads_per_platform=int(scan_settings["max_leads"]),
            max_pages_per_platform=int(scan_settings["max_pages"]),
        )
        platform_rows = summary.get("platforms", [])
        for row in platform_rows:
            platform = str(row.get("platform") or "").strip().lower()
            if not platform:
                continue
            found = int(row.get("found") or 0)
            new = int(row.get("new") or 0)
            error = str(row.get("error") or "").strip() or None
            await self.store.update_platform_runtime(
                platform=platform,
                found=found,
                new=new,
                error=error,
            )
            await self._maybe_notify_platform_runtime_change(
                platform=platform,
                previous=runtime_before.get(platform),
                found=found,
                new=new,
                error=error,
            )

        payload = {
            "trigger": trigger,
            "found": summary["found"],
            "new": summary["new"],
            "applied": summary["applied"],
            "paused": self.paused,
            "auto_apply": self.auto_apply,
            "scan_interval_seconds": int(scan_settings["interval_seconds"]),
            "scan_max_pages": int(scan_settings["max_pages"]),
            "scan_max_leads": int(scan_settings["max_leads"]),
            "notify_burst_limit": int(scan_settings["burst_limit"]),
            "notify_burst_window_minutes": int(scan_settings["burst_window_minutes"]),
            "language_mode": str(language_mode["mode"]),
            "auto_apply_hour_limit": int(apply_limits["hour_limit"]),
            "auto_apply_day_limit": int(apply_limits["day_limit"]),
            "auto_apply_hour_used": int(apply_limits["hour_used"]),
            "auto_apply_day_used": int(apply_limits["day_used"]),
            "enabled_platforms": [a.name for a in adapters],
            "platforms": platform_rows,
        }
        await self.store.record_event(None, "cycle_summary", payload)
        if trigger == "manual":
            await self.notifier.send_text(
                "Цикл завершен:\n"
                f"Режим: {trigger}\n"
                f"Найдено: {summary['found']}\n"
                f"Новых: {summary['new']}\n"
                f"Откликов: {summary['applied']}\n"
                f"Активных платформ: {len(adapters)}\n"
                f"Пауза: {self._yes_no(self.paused)}\n"
                f"Автоотклик: {self._yes_no(self.auto_apply)}"
            )

    async def _dispatch_pending_lead_notifications(self) -> int:
        batch_limit = max(1, int(settings.telegram_notify_batch_size))
        fetch_limit = max(batch_limit * 5, batch_limit)
        scan_settings = await self._get_effective_scan_settings()
        pending = await self.store.pending_lead_notifications(
            limit=fetch_limit,
            min_score=float((await self._get_effective_filters())["min_score"]),
            exclude_skipped=True,
            retry_after_seconds=settings.telegram_notify_retry_after_seconds,
            max_attempts=settings.telegram_notify_max_attempts,
        )
        burst_limit = max(0, int(scan_settings["burst_limit"]))
        burst_window_minutes = max(1, int(scan_settings["burst_window_minutes"]))
        recent_by_platform = (
            await self.store.recent_delivery_counts_by_platform(window_minutes=burst_window_minutes)
            if burst_limit > 0
            else {}
        )

        sent = 0
        throttled_by_platform: dict[str, int] = {}
        for lead_id, scored in pending:
            if sent >= batch_limit:
                break
            platform = (scored.lead.platform or "").strip().lower()
            if burst_limit > 0:
                already_sent = int(recent_by_platform.get(platform, 0))
                if already_sent >= burst_limit:
                    throttled_by_platform[platform] = throttled_by_platform.get(platform, 0) + 1
                    continue
            try:
                await self.notifier.send_lead_scored(scored, lead_id=lead_id)
                await self.store.mark_lead_notified(lead_id)
                await self.store.record_event(lead_id, "lead_delivered", {"score": scored.score})
                sent += 1
                if burst_limit > 0:
                    recent_by_platform[platform] = int(recent_by_platform.get(platform, 0)) + 1
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                await self.store.mark_lead_notify_failed(lead_id, error)
                await self.store.record_event(lead_id, "lead_delivery_failed", {"error": error})
        if throttled_by_platform:
            await self.store.record_event(
                None,
                "lead_delivery_throttled_batch",
                {
                    "window_minutes": burst_window_minutes,
                    "limit_per_platform": burst_limit,
                    "platforms": throttled_by_platform,
                },
            )
        if sent:
            await self.store.record_event(
                None,
                "lead_delivery_batch",
                {"sent": sent, "requested": len(pending), "batch_limit": batch_limit},
            )
        return sent

    async def _get_enabled_adapters(self) -> list[BasePlatformAdapter]:
        result: list[BasePlatformAdapter] = []
        for adapter in self.orchestrator.adapters:
            if not self._session_file_path(adapter.name).exists():
                continue
            default_enabled = self.platform_defaults.get(adapter.name, True)
            enabled = await self.store.get_runtime_flag(
                f"platform:{adapter.name}:enabled",
                default=default_enabled,
            )
            if enabled:
                result.append(adapter)
        return result

    def _get_adapter(self, platform: str) -> BasePlatformAdapter | None:
        for adapter in self.orchestrator.adapters:
            if adapter.name == platform:
                return adapter
        return None

    async def _wait_for_next_tick(self, elapsed_seconds: float = 0.0) -> None:
        scan_settings = await self._get_effective_scan_settings()
        interval = int(scan_settings["interval_seconds"])
        timeout = max(0.0, float(interval) - max(0.0, elapsed_seconds))
        wait_stop = asyncio.create_task(self._stop_event.wait())
        wait_manual = asyncio.create_task(self._run_now_event.wait())
        try:
            done, pending = await asyncio.wait(
                {wait_stop, wait_manual},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            for task in done:
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            for task in (wait_stop, wait_manual):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    async def _control_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                updates = await self.notifier.bot.get_updates(
                    offset=self._updates_offset,
                    timeout=settings.telegram_control_poll_timeout,
                    allowed_updates=["message", "callback_query"],
                )
                for update in updates:
                    self._updates_offset = update.update_id + 1
                    if update.message:
                        await self._handle_message(update.message)
                    if update.callback_query:
                        await self._handle_callback(update.callback_query)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.store.record_event(
                    None,
                    "telegram_control_error",
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
                await asyncio.sleep(3)

    async def _bootstrap_updates_offset(self) -> None:
        with suppress(Exception):
            updates = await self.notifier.bot.get_updates(
                timeout=0,
                allowed_updates=["message", "callback_query"],
            )
            if updates:
                self._updates_offset = updates[-1].update_id + 1

    async def _handle_message(self, message: Message) -> None:
        if not message.text:
            return
        if not self._is_allowed_chat(message.chat.id):
            return

        chat_key = str(message.chat.id)
        text = message.text.strip()

        pending = self._pending_profile_input.get(chat_key)
        if pending:
            await self._save_profile_input(chat_key, pending, text)
            return

        await self._send_main_menu("Главное меню")

    async def _handle_callback(self, callback: CallbackQuery) -> None:
        chat_id = callback.message.chat.id if callback.message else None
        if chat_id is None or not self._is_allowed_chat(chat_id):
            with suppress(Exception):
                await self.notifier.bot.answer_callback_query(callback.id)
            return

        data = (callback.data or "").strip()

        if data == "menu:main":
            await self._send_main_menu("Главное меню", callback=callback)
        elif data == "menu:status":
            await self._send_status(callback=callback)
        elif data == "menu:leads":
            await self._send_recent_leads(callback=callback)
        elif data == "menu:flow":
            await self._send_flow_preview(callback=callback)
        elif data == "menu:logs":
            await self._send_apply_logs(callback=callback)
        elif data == "menu:accounts":
            await self._send_accounts(callback=callback)
        elif data == "menu:profile":
            await self._send_profile_menu(callback=callback)
        elif data == "menu:settings":
            await self._send_settings(callback=callback)
        elif data == "menu:filters":
            await self._send_filters_menu(callback=callback)
        elif data == "menu:scan":
            await self._send_scan_menu(callback=callback)
        elif data == "menu:languages":
            await self._send_language_menu(callback=callback)
        elif data == "act:cycle":
            self._run_now_event.set()
            await self._send_status(callback=callback, header="Ручной запуск цикла поставлен в очередь.")
        elif data == "act:stop_auto":
            self.auto_apply = False
            await self.store.set_runtime_flag("auto_apply", False)
            await self._send_settings(
                callback=callback,
                note="Автоотклик аварийно остановлен. Включи обратно вручную в настройках.",
            )
        elif data == "toggle:pause":
            self.paused = not self.paused
            await self.store.set_runtime_flag("paused", self.paused)
            await self._send_settings(callback=callback)
        elif data == "toggle:auto":
            self.auto_apply = not self.auto_apply
            await self.store.set_runtime_flag("auto_apply", self.auto_apply)
            await self._send_settings(callback=callback)
        elif data.startswith("pt:"):
            platform = data.split(":", 1)[1]
            await self._toggle_platform(platform, callback=callback)
        elif data.startswith("lo:"):
            platform = data.split(":", 1)[1]
            await self._logout_platform(platform, callback=callback)
        elif data.startswith("acc:"):
            platform = data.split(":", 1)[1]
            await self._send_account_detail(platform, callback=callback)
        elif data.startswith("psync:"):
            platform = data.split(":", 1)[1]
            await self._sync_platform_profile(platform, callback=callback)
        elif data.startswith("ed:"):
            field = data.split(":", 1)[1]
            await self._begin_global_profile_input(str(chat_id), field, callback=callback)
        elif data.startswith("edf:"):
            field = data.split(":", 1)[1]
            await self._begin_filter_input(str(chat_id), field, callback=callback)
        elif data.startswith("eds:"):
            field = data.split(":", 1)[1]
            await self._begin_scan_input(str(chat_id), field, callback=callback)
        elif data.startswith("setlang:"):
            mode = data.split(":", 1)[1].strip().lower()
            await self._set_language_mode(mode, callback=callback)
        elif data.startswith("apf:"):
            parts = data.split(":", 2)
            if len(parts) != 3:
                await self._send_main_menu("Некорректное действие", callback=callback)
            else:
                platform, field = parts[1], parts[2]
                await self._begin_platform_profile_input(str(chat_id), platform, field, callback=callback)
        elif data.startswith("gen:"):
            raw_id = data.split(":", 1)[1]
            if raw_id.isdigit():
                await self._generate_proposal_for_lead(int(raw_id), source="button")
            else:
                await self.notifier.send_text("Некорректный ID лида", reply_markup=self._kb_main())
        elif data.startswith("aiw:"):
            raw_id = data.split(":", 1)[1]
            if raw_id.isdigit():
                await self._begin_ai_prompt(str(chat_id), int(raw_id), callback=callback)
            else:
                await self.notifier.send_text("Некорректный ID лида", reply_markup=self._kb_main())
        elif data.startswith("fb:"):
            parts = data.split(":")
            if len(parts) == 3 and parts[2].isdigit():
                verdict = parts[1].strip().lower()
                lead_id = int(parts[2])
                await self._save_feedback_by_lead_id(lead_id=lead_id, verdict=verdict, note="")
            else:
                await self.notifier.send_text("Некорректный callback оценки", reply_markup=self._kb_main())
        elif data == "noop":
            pass
        else:
            await self._send_main_menu("Неизвестное действие", callback=callback)

        with suppress(Exception):
            await self.notifier.bot.answer_callback_query(callback.id)

    def _is_allowed_chat(self, chat_id: int | str) -> bool:
        return str(chat_id) == str(settings.telegram_chat_id)

    async def _render_menu(
        self,
        text: str,
        reply_markup: InlineKeyboardMarkup,
        callback: CallbackQuery | None = None,
    ) -> None:
        if callback and isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(text=text, reply_markup=reply_markup)
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
            except Exception:
                pass
        await self.notifier.send_text(text, reply_markup=reply_markup)

    async def _send_main_menu(self, text: str | None = None, callback: CallbackQuery | None = None) -> None:
        message = text or "Главное меню"
        await self._render_menu(message, self._kb_main(), callback=callback)

    def _kb_main(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Статус", callback_data="menu:status"),
                    InlineKeyboardButton(text="Вакансии", callback_data="menu:leads"),
                ],
                [
                    InlineKeyboardButton(text="Поток", callback_data="menu:flow"),
                    InlineKeyboardButton(text="Логи", callback_data="menu:logs"),
                ],
                [
                    InlineKeyboardButton(text="Аккаунты", callback_data="menu:accounts"),
                ],
                [
                    InlineKeyboardButton(text="Профиль", callback_data="menu:profile"),
                    InlineKeyboardButton(text="Настройки", callback_data="menu:settings"),
                ],
                [InlineKeyboardButton(text="Запустить цикл", callback_data="act:cycle")],
            ]
        )

    async def _send_status(self, callback: CallbackQuery | None = None, header: str | None = None) -> None:
        stats = await self.store.stats()
        adapters = await self._get_enabled_adapters()
        filters = await self._get_effective_filters()
        scan_settings = await self._get_effective_scan_settings()
        language_mode = await self._get_effective_language_mode()
        apply_limits = await self._get_auto_apply_limits_snapshot()
        min_score = float(filters["min_score"])
        pending_delivery = await self.store.count_pending_lead_notifications(
            min_score=min_score,
            exclude_skipped=True,
            max_attempts=settings.telegram_notify_max_attempts,
        )
        platform_runtime = await self.store.get_platform_runtime()
        text = (
            f"{header + '\n' if header else ''}"
            "Статус:\n"
            f"Пауза: {self._yes_no(self.paused)}\n"
            f"Автоотклик: {self._yes_no(self.auto_apply)}\n"
            f"Лимит автоотклика: {apply_limits['hour_used']}/{apply_limits['hour_limit']} за час, "
            f"{apply_limits['day_used']}/{apply_limits['day_limit']} за сутки\n"
            f"Активных платформ: {len(adapters)}\n"
            f"Фильтр score >= {min_score:.2f}\n"
            f"Языковой режим: {language_mode['label']}\n"
            f"Интервал: {int(scan_settings['interval_seconds'])} сек\n"
            f"Глубина: {int(scan_settings['max_pages'])} стр\n"
            f"Лимит: {int(scan_settings['max_leads'])} лид/биржа\n"
            f"Anti-flood: {int(scan_settings['burst_limit'])}/{int(scan_settings['burst_window_minutes'])}м на биржу\n"
            f"К отправке в Telegram: {pending_delivery}\n"
            f"Новые: {stats.get('new', 0)}\n"
            f"Черновики: {stats.get('drafted', 0)}\n"
            f"Отправлено: {stats.get('applied', 0)}\n"
            f"Ошибки: {stats.get('failed', 0)}\n"
            f"Пропущено: {stats.get('skipped', 0)}"
        )
        platform_lines: list[str] = ["", "Площадки:"]
        for key in sorted(self.platforms_cfg.keys()):
            cfg = self.platforms_cfg.get(key, {})
            display = str(cfg.get("display_name", key))
            runtime = platform_runtime.get(key, {})
            err = str(runtime.get("last_error") or "").strip()
            state = self._platform_state_label(str(runtime.get("state", "unknown")), err)
            found = int(runtime.get("last_found", 0) or 0)
            new = int(runtime.get("last_new", 0) or 0)
            last_ok = self._format_iso_dt(runtime.get("last_success_at"))

            default_enabled = self.platform_defaults.get(key, True)
            enabled = await self.store.get_runtime_flag(
                f"platform:{key}:enabled",
                default=default_enabled,
            )
            connected = self._session_file_path(key).exists()
            platform_lines.append(
                f"{display}: {state} | found={found} new={new} | ok={last_ok} | "
                f"вкл={self._yes_no(enabled)} | сессия={self._yes_no(connected)}"
            )
            if err and str(runtime.get("state", "")) == "error":
                platform_lines.append(f"Ошибка {display}: {compact(err, 120)}")
        text = f"{text}\n" + "\n".join(platform_lines)
        await self._render_menu(text, self._kb_main(), callback=callback)

    async def _send_recent_leads(self, callback: CallbackQuery | None = None) -> None:
        min_score = float((await self._get_effective_filters())["min_score"])
        leads = await self.store.recent_leads(
            limit=8,
            min_score=min_score,
            exclude_skipped=True,
        )
        if not leads:
            await self._render_menu(
                "Пока нет подходящих вакансий.\nЗапусти цикл и проверь снова.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Запустить цикл", callback_data="act:cycle")],
                        [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
                    ]
                ),
                callback=callback,
            )
            return

        lines: list[str] = [f"Последние вакансии (score >= {min_score:.2f}):"]
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for item in leads:
            published_text = self._format_lead_publication(item)
            delivered = "Да" if item.get("sent_at") else "Нет"
            lines.append(
                "\n".join(
                    [
                        f"#{item['id']} | {item['platform']} | score={item['score']:.2f}",
                        f"{compact(item['title'], 110)}",
                        f"Бюджет: {item['budget'] or '-'} | Статус: {item['status']} | Доставлено: {delivered}",
                        f"Опубликовано: {published_text}",
                        item["url"],
                    ]
                )
            )
            lines.append("")
            keyboard_rows.append(
                [
                    InlineKeyboardButton(text=f"Отклик #{item['id']}", callback_data=f"gen:{item['id']}"),
                    InlineKeyboardButton(text=f"Свой запрос #{item['id']}", callback_data=f"aiw:{item['id']}"),
                ]
            )

        text = "\n".join(lines).strip()
        keyboard_rows.extend(
            [
                [InlineKeyboardButton(text="Обновить список", callback_data="menu:leads")],
                [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
            ]
        )
        await self._render_menu(
            text,
            InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
            callback=callback,
        )

    async def _send_flow_preview(self, callback: CallbackQuery | None = None) -> None:
        cycle_payload, cycle_created_at = await self._latest_cycle_summary_payload()
        if not cycle_payload:
            await self._render_menu(
                "Пока нет данных по циклам.\nЗапусти цикл и открой экран снова.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Запустить цикл", callback_data="act:cycle")],
                        [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
                    ]
                ),
                callback=callback,
            )
            return

        created_text = self._format_iso_dt(cycle_created_at)
        trigger = str(cycle_payload.get("trigger") or "-")
        platforms = cycle_payload.get("platforms") or []
        if not isinstance(platforms, list):
            platforms = []

        lines: list[str] = [
            "Предпросмотр потока:",
            f"Цикл: {created_text} | trigger={trigger}",
            "Почему лиды прошли фильтр по площадкам:",
        ]

        for row in platforms:
            if not isinstance(row, dict):
                continue
            platform = str(row.get("platform") or "").strip().lower()
            if not platform:
                continue
            cfg = self.platforms_cfg.get(platform, {})
            display = str(cfg.get("display_name", platform))
            found = int(row.get("found") or 0)
            new = int(row.get("new") or 0)
            error = str(row.get("error") or "").strip()
            preview = row.get("passed_preview") or []
            if not isinstance(preview, list):
                preview = []

            lines.append("")
            lines.append(f"{display}: found={found} | new={new}")
            if error:
                lines.append(f"Ошибка: {compact(error, 140)}")
                continue

            if not preview:
                lines.append("Подходящих новых лидов за цикл нет.")
                continue

            for item in preview[:3]:
                if not isinstance(item, dict):
                    continue
                lead_id = item.get("lead_id")
                title = compact(str(item.get("title") or "-"), 100)
                url = str(item.get("url") or "").strip()
                score_val = item.get("score")
                score_text = "-"
                if isinstance(score_val, (int, float)):
                    score_text = f"{float(score_val):.2f}"
                reasons = item.get("reasons") or []
                if isinstance(reasons, list):
                    reasons_txt = ", ".join(str(x) for x in reasons if str(x).strip())
                else:
                    reasons_txt = str(reasons).strip()

                lead_prefix = f"#{lead_id} " if lead_id is not None else ""
                lines.append(f"{lead_prefix}score={score_text} | {title}")
                if reasons_txt:
                    lines.append(f"Почему: {compact(reasons_txt, 180)}")
                if url:
                    lines.append(url)

        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3890].rstrip() + "\n..."
        await self._render_menu(
            text,
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Обновить поток", callback_data="menu:flow")],
                    [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
                ]
            ),
            callback=callback,
        )

    async def _send_apply_logs(self, callback: CallbackQuery | None = None) -> None:
        logs = await self.store.recent_leads(
            limit=8,
            min_score=0.0,
            exclude_skipped=False,
        )
        if not logs:
            await self._render_menu(
                "Логи пока пустые.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
                    ]
                ),
                callback=callback,
            )
            return

        lines: list[str] = ["Логи откликов и ссылок:"]
        for item in logs:
            lead_id = int(item.get("id") or 0)
            platform = str(item.get("platform") or "-")
            status = str(item.get("status") or "-")
            sent = "Да" if item.get("sent_at") else "Нет"
            lead_url = str(item.get("url") or "").strip() or "-"
            proposal_url = str(item.get("proposal_url") or "").strip() or "-"
            chat_url = str(item.get("chat_url") or "").strip() or "-"
            err = compact(str(item.get("error_message") or item.get("notify_last_error") or ""), 140)

            lines.append(f"#{lead_id} | {platform} | статус={status} | доставлено={sent}")
            lines.append(f"Объявление: {lead_url}")
            lines.append(f"Отклик: {proposal_url}")
            lines.append(f"Чат: {chat_url}")
            if err:
                lines.append(f"Ошибка: {err}")
            lines.append("")

        text = "\n".join(lines).strip()
        if len(text) > 3900:
            text = text[:3890].rstrip() + "\n..."
        await self._render_menu(
            text,
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Обновить логи", callback_data="menu:logs")],
                    [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
                ]
            ),
            callback=callback,
        )

    async def _latest_cycle_summary_payload(self) -> tuple[dict[str, object] | None, str]:
        events = await self.store.recent_events(limit=60)
        for event in events:
            if str(event.get("event_type") or "") != "cycle_summary":
                continue
            raw_payload = str(event.get("payload") or "")
            if not raw_payload:
                continue
            with suppress(Exception):
                parsed = json.loads(raw_payload)
                if isinstance(parsed, dict):
                    created_at = str(event.get("created_at") or "")
                    return parsed, created_at
        return None, ""

    async def _begin_ai_prompt(
        self,
        chat_key: str,
        lead_id: int,
        callback: CallbackQuery | None = None,
    ) -> None:
        self._pending_profile_input[chat_key] = f"ai:{lead_id}"
        await self._render_menu(
            f"Лид #{lead_id}\n"
            "Напиши пожелания для отклика.\n"
            "Например: стиль, срок, бюджет, стек, акценты.\n\n"
            "После сообщения я сразу сгенерирую текст через ИИ.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Назад к вакансиям", callback_data="menu:leads")],
                ]
            ),
            callback=callback,
        )

    async def _send_accounts(self, callback: CallbackQuery | None = None) -> None:
        lines: list[str] = ["Аккаунты:", "Выбери площадку:"]
        keyboard_rows: list[list[InlineKeyboardButton]] = []

        for key in sorted(self.platforms_cfg.keys()):
            cfg = self.platforms_cfg.get(key, {})
            display = cfg.get("display_name", key)
            session = self._session_file_path(key)
            connected = session.exists()
            default_enabled = self.platform_defaults.get(key, True)
            enabled = await self.store.get_runtime_flag(
                f"platform:{key}:enabled",
                default=default_enabled,
            )
            lines.append(
                f"{display} ({key}) | Подключен: {self._yes_no(connected)} | Мониторинг: {self._yes_no(enabled)}"
            )
            keyboard_rows.append([InlineKeyboardButton(text=display, callback_data=f"acc:{key}")])

        keyboard_rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:main")])
        await self._render_menu(
            "\n".join(lines),
            InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
            callback=callback,
        )

    async def _send_account_detail(
        self,
        platform: str,
        callback: CallbackQuery | None = None,
        note: str = "",
    ) -> None:
        cfg = self.platforms_cfg.get(platform)
        if not cfg:
            await self._send_accounts(callback=callback)
            return

        display = cfg.get("display_name", platform)
        session = self._session_file_path(platform)
        connected = session.exists()
        default_enabled = self.platform_defaults.get(platform, True)
        enabled = await self.store.get_runtime_flag(
            f"platform:{platform}:enabled",
            default=default_enabled,
        )
        p = await self.store.get_platform_profile(platform)

        default_profile_url = str(cfg.get("profile", {}).get("edit_url") or cfg.get("login_url") or "")
        p_name = p.get("name", "-")
        p_headline = p.get("headline", "-")
        p_resume = (p.get("resume", "-") or "-")[:300]
        p_rates = p.get("rates", "-")
        p_profile_url = p.get("profile_url", default_profile_url or "-")
        p_portfolio = self._portfolio_urls(p.get("portfolio_urls", ""))
        p_portfolio_text = ", ".join(p_portfolio) if p_portfolio else "-"

        text = (
            f"{note + '\n' if note else ''}"
            f"Площадка: {display} ({platform})\n"
            f"Подключен: {self._yes_no(connected)}\n"
            f"Мониторинг: {self._yes_no(enabled)}\n\n"
            "Анкета площадки:\n"
            f"Имя: {p_name}\n"
            f"Заголовок: {p_headline}\n"
            f"Описание: {p_resume}\n"
            f"Портфолио: {p_portfolio_text}\n"
            f"Ставки: {p_rates}\n"
            f"URL профиля: {p_profile_url}"
        )

        toggle_text = "Выключить мониторинг" if enabled else "Включить мониторинг"
        logout_text = "Удалить сессию" if connected else "Сессии нет"
        logout_cb = f"lo:{platform}" if connected else "noop"

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=toggle_text, callback_data=f"pt:{platform}")],
                [InlineKeyboardButton(text=logout_text, callback_data=logout_cb)],
                [InlineKeyboardButton(text="Синхронизировать на сайт", callback_data=f"psync:{platform}")],
                [
                    InlineKeyboardButton(text="Имя", callback_data=f"apf:{platform}:name"),
                    InlineKeyboardButton(text="Заголовок", callback_data=f"apf:{platform}:headline"),
                ],
                [InlineKeyboardButton(text="Описание", callback_data=f"apf:{platform}:resume")],
                [InlineKeyboardButton(text="Портфолио", callback_data=f"apf:{platform}:portfolio_urls")],
                [InlineKeyboardButton(text="Ставки", callback_data=f"apf:{platform}:rates")],
                [InlineKeyboardButton(text="URL профиля", callback_data=f"apf:{platform}:profile_url")],
                [InlineKeyboardButton(text="Назад к списку", callback_data="menu:accounts")],
            ]
        )
        await self._render_menu(text, kb, callback=callback)

    async def _toggle_platform(self, platform: str, callback: CallbackQuery | None = None) -> None:
        if platform not in self.platforms_cfg:
            await self._send_accounts(callback=callback)
            return
        default_enabled = self.platform_defaults.get(platform, True)
        current = await self.store.get_runtime_flag(
            f"platform:{platform}:enabled",
            default=default_enabled,
        )
        await self.store.set_runtime_flag(f"platform:{platform}:enabled", not current)
        await self._send_account_detail(platform, callback=callback)

    async def _logout_platform(self, platform: str, callback: CallbackQuery | None = None) -> None:
        if platform not in self.platforms_cfg:
            await self._send_accounts(callback=callback)
            return
        session = self._session_file_path(platform)
        if session.exists():
            session.unlink()
            await self._send_account_detail(platform, callback=callback, note="Сессия удалена")
        else:
            await self._send_account_detail(platform, callback=callback, note="Сессия не найдена")

    async def _sync_platform_profile(
        self,
        platform: str,
        callback: CallbackQuery | None = None,
        silent: bool = False,
    ) -> None:
        adapter = self._get_adapter(platform)
        if not adapter:
            if callback:
                await self._send_account_detail(platform, callback=callback, note="Адаптер платформы не найден")
            elif not silent:
                await self.notifier.send_text(f"Адаптер платформы не найден: {platform}")
            return

        profile = await self.store.get_profile()
        platform_profile = await self.store.get_platform_profile(platform)
        cfg = self.platforms_cfg.get(platform, {})
        default_profile_url = str(cfg.get("profile", {}).get("edit_url") or cfg.get("login_url") or "")

        global_name = (profile.get("name") or "").strip()
        global_resume = (profile.get("resume") or settings.freelancer_profile).strip()
        global_portfolio = self._portfolio_urls(profile.get("portfolio_urls", "")) or settings.portfolio_list

        merged_resume = self._compose_profile_text(global_resume, platform_profile)
        merged_portfolio = self._merge_portfolio_urls(
            global_portfolio,
            platform_profile.get("portfolio_urls", ""),
        )

        payload = {
            "name": (platform_profile.get("name") or global_name).strip(),
            "headline": (platform_profile.get("headline") or "").strip(),
            "resume": merged_resume,
            "portfolio_urls": "\n".join(merged_portfolio),
            "rates": (platform_profile.get("rates") or "").strip(),
            "profile_url": (platform_profile.get("profile_url") or default_profile_url).strip(),
        }

        ok, message = await adapter.sync_profile(payload)
        note = f"Синхронизация {'успешна' if ok else 'с ошибкой'}: {message}"

        if callback:
            await self._send_account_detail(platform, callback=callback, note=note)
            return

        if not silent:
            await self.notifier.send_text(note, reply_markup=self._kb_main())

    async def _send_profile_menu(self, callback: CallbackQuery | None = None) -> None:
        profile = await self.store.get_profile()
        name = profile.get("name", "")
        resume = profile.get("resume", settings.freelancer_profile)
        avatar = profile.get("avatar_url", "")
        portfolio = self._portfolio_urls(profile.get("portfolio_urls", "")) or settings.portfolio_list
        portfolio_txt = ", ".join(portfolio) if portfolio else "-"

        text = (
            "Общий профиль:\n"
            f"Имя: {name or '-'}\n"
            f"Аватар: {avatar or '-'}\n"
            f"Резюме: {(resume or '-')[:450]}\n"
            f"Портфолио: {portfolio_txt}\n\n"
            "Выбери поле для редактирования"
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Имя", callback_data="ed:name")],
                [InlineKeyboardButton(text="Резюме", callback_data="ed:resume")],
                [InlineKeyboardButton(text="Аватар", callback_data="ed:avatar_url")],
                [InlineKeyboardButton(text="Портфолио", callback_data="ed:portfolio_urls")],
                [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
            ]
        )
        await self._render_menu(text, kb, callback=callback)

    async def _begin_global_profile_input(
        self,
        chat_key: str,
        field: str,
        callback: CallbackQuery | None = None,
    ) -> None:
        if field not in self.GLOBAL_PROFILE_HINTS:
            await self._send_profile_menu(callback=callback)
            return
        self._pending_profile_input[chat_key] = f"g:{field}"
        label = self.GLOBAL_PROFILE_LABELS[field]
        hint = self.GLOBAL_PROFILE_HINTS[field]
        await self._render_menu(
            f"Режим ввода: {label}\n{hint}\n\n"
            "Отправь одно текстовое сообщение.",
            InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu:profile")]]
            ),
            callback=callback,
        )

    async def _begin_platform_profile_input(
        self,
        chat_key: str,
        platform: str,
        field: str,
        callback: CallbackQuery | None = None,
    ) -> None:
        if platform not in self.platforms_cfg or field not in self.PLATFORM_PROFILE_HINTS:
            await self._send_accounts(callback=callback)
            return
        self._pending_profile_input[chat_key] = f"p:{platform}:{field}"
        label = self.PLATFORM_PROFILE_LABELS[field]
        hint = self.PLATFORM_PROFILE_HINTS[field]
        display = self.platforms_cfg.get(platform, {}).get("display_name", platform)
        await self._render_menu(
            f"Площадка: {display}\n"
            f"Режим ввода: {label}\n{hint}\n\n"
            "Отправь одно текстовое сообщение.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Назад", callback_data=f"acc:{platform}")],
                ]
            ),
            callback=callback,
        )

    async def _save_profile_input(self, chat_key: str, pending: str, text: str) -> None:
        value = text.strip()
        if not value:
            await self.notifier.send_text("Пустое значение", reply_markup=self._kb_main())
            return

        parts = pending.split(":")
        if len(parts) == 2 and parts[0] == "ai" and parts[1].isdigit():
            lead_id = int(parts[1])
            self._pending_profile_input.pop(chat_key, None)
            await self._generate_proposal_for_lead(
                lead_id,
                source="ai_custom_prompt",
                custom_request=value,
            )
            return

        if len(parts) == 2 and parts[0] == "f":
            field = parts[1]
            await self._save_filter_input(chat_key=chat_key, field=field, value=value)
            return

        if len(parts) == 2 and parts[0] == "s":
            field = parts[1]
            await self._save_scan_input(chat_key=chat_key, field=field, value=value)
            return

        if len(parts) == 2 and parts[0] == "g":
            field = parts[1]
            if field == "portfolio_urls":
                urls = self._portfolio_urls(value)
                await self.store.set_profile_field("portfolio_urls", ",".join(urls))
                self._pending_profile_input.pop(chat_key, None)
                await self.notifier.send_text(f"Сохранено: {self.GLOBAL_PROFILE_LABELS[field]} ({len(urls)})")
                await self._send_profile_menu()
                return

            await self.store.set_profile_field(field, value)
            self._pending_profile_input.pop(chat_key, None)
            await self.notifier.send_text(f"Сохранено: {self.GLOBAL_PROFILE_LABELS[field]}")
            await self._send_profile_menu()
            return

        if len(parts) == 3 and parts[0] == "p":
            platform = parts[1]
            field = parts[2]
            if field == "portfolio_urls":
                urls = self._portfolio_urls(value)
                await self.store.set_platform_profile_field(platform, field, ",".join(urls))
            else:
                await self.store.set_platform_profile_field(platform, field, value)
            self._pending_profile_input.pop(chat_key, None)
            label = self.PLATFORM_PROFILE_LABELS.get(field, field)
            display = self.platforms_cfg.get(platform, {}).get("display_name", platform)
            await self.notifier.send_text(f"Сохранено: {display} -> {label}")
            await self._sync_platform_profile(platform, silent=False)
            await self._send_account_detail(platform)
            return

        self._pending_profile_input.pop(chat_key, None)
        await self.notifier.send_text("Не удалось распознать режим ввода")

    def _kb_settings(self) -> InlineKeyboardMarkup:
        pause_text = "Продолжить" if self.paused else "Пауза"
        auto_text = "Автоотклик: ВКЛ" if self.auto_apply else "Автоотклик: ВЫКЛ"
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text=pause_text, callback_data="toggle:pause")],
            [InlineKeyboardButton(text=auto_text, callback_data="toggle:auto")],
        ]
        if self.auto_apply:
            rows.append([InlineKeyboardButton(text="Стоп автоотклик", callback_data="act:stop_auto")])
        rows.extend(
            [
                [InlineKeyboardButton(text="Фильтр заказов", callback_data="menu:filters")],
                [InlineKeyboardButton(text="Сканирование", callback_data="menu:scan")],
                [InlineKeyboardButton(text="Языковой режим", callback_data="menu:languages")],
                [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def _send_settings(self, callback: CallbackQuery | None = None, note: str = "") -> None:
        filters = await self._get_effective_filters()
        scan_settings = await self._get_effective_scan_settings()
        language_mode = await self._get_effective_language_mode()
        apply_limits = await self._get_auto_apply_limits_snapshot()
        min_score = float(filters["min_score"])
        keywords = list(filters["keywords"])
        negative_keywords = list(filters["negative_keywords"])
        await self._render_menu(
            f"{note + '\n' if note else ''}"
            "Настройки:\n"
            f"Пауза: {self._yes_no(self.paused)}\n"
            f"Автоотклик: {self._yes_no(self.auto_apply)}\n"
            f"Лимит автоотклика: {apply_limits['hour_used']}/{apply_limits['hour_limit']} за час, "
            f"{apply_limits['day_used']}/{apply_limits['day_limit']} за сутки\n"
            f"Мин. score: {min_score:.2f}\n"
            f"Язык: {language_mode['label']}\n"
            f"Интервал: {int(scan_settings['interval_seconds'])} сек\n"
            f"Глубина: {int(scan_settings['max_pages'])} стр\n"
            f"Лимит: {int(scan_settings['max_leads'])} лид/биржа\n"
            f"Anti-flood: {int(scan_settings['burst_limit'])}/{int(scan_settings['burst_window_minutes'])}м на биржу\n"
            f"Ключевые: {len(keywords)}\n"
            f"Минус-слова: {len(negative_keywords)}\n\n"
            "Если автоотклик выключен, бот только парсит и дает кнопку генерации.",
            self._kb_settings(),
            callback=callback,
        )

    async def _send_filters_menu(self, callback: CallbackQuery | None = None, note: str = "") -> None:
        filters = await self._get_effective_filters()
        min_score = float(filters["min_score"])
        keywords = list(filters["keywords"])
        negative_keywords = list(filters["negative_keywords"])
        text = (
            f"{note + '\n' if note else ''}"
            "Фильтр заказов:\n"
            f"Минимальный score: {min_score:.2f}\n"
            f"Ключевые слова: {self._keywords_preview(keywords)}\n"
            f"Минус-слова: {self._keywords_preview(negative_keywords)}\n\n"
            "Измени параметры фильтра через кнопки ниже."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Изменить минимальный score", callback_data="edf:min_score")],
                [InlineKeyboardButton(text="Изменить ключевые", callback_data="edf:keywords")],
                [InlineKeyboardButton(text="Изменить минус-слова", callback_data="edf:negative_keywords")],
                [InlineKeyboardButton(text="Назад в настройки", callback_data="menu:settings")],
            ]
        )
        await self._render_menu(text, kb, callback=callback)

    async def _begin_filter_input(
        self,
        chat_key: str,
        field: str,
        callback: CallbackQuery | None = None,
    ) -> None:
        if field not in self.FILTER_INPUT_HINTS:
            await self._send_filters_menu(callback=callback)
            return
        self._pending_profile_input[chat_key] = f"f:{field}"
        label = self.FILTER_LABELS[field]
        hint = self.FILTER_INPUT_HINTS[field]
        await self._render_menu(
            f"Режим ввода: {label}\n{hint}\n\n"
            "Отправь одно текстовое сообщение.",
            InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu:filters")]]
            ),
            callback=callback,
        )

    async def _send_scan_menu(self, callback: CallbackQuery | None = None, note: str = "") -> None:
        scan = await self._get_effective_scan_settings()
        text = (
            f"{note + '\n' if note else ''}"
            "Сканирование:\n"
            f"Интервал между биржами: {int(scan['interval_seconds'])} сек\n"
            f"Глубина обхода: {int(scan['max_pages'])} стр/биржа\n"
            f"Лимит лидов: {int(scan['max_leads'])} на биржу\n\n"
            f"Anti-flood: {int(scan['burst_limit'])} лидов / {int(scan['burst_window_minutes'])} мин\n\n"
            "Измени параметры через кнопки ниже."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Изменить интервал", callback_data="eds:interval_seconds")],
                [InlineKeyboardButton(text="Изменить глубину", callback_data="eds:max_pages")],
                [InlineKeyboardButton(text="Изменить лимит лидов", callback_data="eds:max_leads")],
                [InlineKeyboardButton(text="Изменить лимит anti-flood", callback_data="eds:burst_limit")],
                [InlineKeyboardButton(text="Изменить окно anti-flood", callback_data="eds:burst_window_minutes")],
                [InlineKeyboardButton(text="Назад в настройки", callback_data="menu:settings")],
            ]
        )
        await self._render_menu(text, kb, callback=callback)

    async def _begin_scan_input(
        self,
        chat_key: str,
        field: str,
        callback: CallbackQuery | None = None,
    ) -> None:
        if field not in self.SCAN_INPUT_HINTS:
            await self._send_scan_menu(callback=callback)
            return
        self._pending_profile_input[chat_key] = f"s:{field}"
        label = self.SCAN_LABELS[field]
        hint = self.SCAN_INPUT_HINTS[field]
        await self._render_menu(
            f"Режим ввода: {label}\n{hint}\n\n"
            "Отправь одно текстовое сообщение.",
            InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu:scan")]]
            ),
            callback=callback,
        )

    async def _send_language_menu(self, callback: CallbackQuery | None = None, note: str = "") -> None:
        mode_data = await self._get_effective_language_mode()
        current_mode = str(mode_data["mode"])
        current_label = str(mode_data["label"])
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=self._mode_button_text("ru", current_mode), callback_data="setlang:ru")],
                [InlineKeyboardButton(text=self._mode_button_text("en", current_mode), callback_data="setlang:en")],
                [
                    InlineKeyboardButton(
                        text=self._mode_button_text("mixed", current_mode),
                        callback_data="setlang:mixed",
                    )
                ],
                [InlineKeyboardButton(text="Назад в настройки", callback_data="menu:settings")],
            ]
        )
        text = (
            f"{note + '\n' if note else ''}"
            "Языковой режим:\n"
            f"Текущий: {current_label}\n\n"
            "Режим влияет на приоритезацию лидов по языку заказчика."
        )
        await self._render_menu(text, kb, callback=callback)

    async def _set_language_mode(self, mode: str, callback: CallbackQuery | None = None) -> None:
        normalized = mode.strip().lower()
        if normalized not in self.LANGUAGE_MODES:
            await self._send_language_menu(callback=callback, note="Неизвестный режим языка.")
            return
        await self.store.set_runtime_value(self.LANGUAGE_RUNTIME_KEY, normalized)
        label = self.LANGUAGE_LABELS.get(normalized, normalized)
        await self._send_language_menu(
            callback=callback,
            note=f"Сохранено: языковой режим = {label}",
        )

    def _mode_button_text(self, mode: str, current_mode: str) -> str:
        label = self.LANGUAGE_LABELS.get(mode, mode)
        if mode == current_mode:
            return f"● {label}"
        return label

    def _portfolio_urls(self, raw: str) -> list[str]:
        return [x.strip() for x in re.split(r"[,\n]", raw) if x.strip()]

    async def _save_filter_input(self, *, chat_key: str, field: str, value: str) -> None:
        normalized_field = field.strip().lower()
        if normalized_field not in self.FILTER_RUNTIME_KEYS:
            self._pending_profile_input.pop(chat_key, None)
            await self.notifier.send_text("Неизвестный фильтр", reply_markup=self._kb_main())
            return

        runtime_key = self.FILTER_RUNTIME_KEYS[normalized_field]
        lowered = value.strip().lower()
        reset_requested = lowered in {"default", "по умолчанию", "сброс", "reset", "clear"}

        if normalized_field == "min_score":
            if reset_requested:
                await self.store.set_runtime_value(runtime_key, "")
                self._pending_profile_input.pop(chat_key, None)
                await self._send_filters_menu(note="Фильтр score сброшен к значению по умолчанию.")
                return
            try:
                parsed = float(value.replace(",", "."))
            except ValueError:
                await self.notifier.send_text("Нужно число от 0 до 1. Пример: 0.45")
                return
            if not (0.0 <= parsed <= 1.0):
                await self.notifier.send_text("Нужно число в диапазоне 0..1.")
                return
            await self.store.set_runtime_value(runtime_key, f"{parsed:.3f}".rstrip("0").rstrip("."))
            self._pending_profile_input.pop(chat_key, None)
            await self._send_filters_menu(note=f"Сохранено: минимальный score = {parsed:.2f}")
            return

        if reset_requested:
            await self.store.set_runtime_value(runtime_key, "")
            self._pending_profile_input.pop(chat_key, None)
            label = self.FILTER_LABELS.get(normalized_field, normalized_field)
            await self._send_filters_menu(note=f"Сброшено: {label}")
            return

        words = self._keywords_from_text(value)
        if not words:
            await self.notifier.send_text("Список пустой. Добавь хотя бы одно слово или напиши default для сброса.")
            return
        await self.store.set_runtime_value(runtime_key, ",".join(words))
        self._pending_profile_input.pop(chat_key, None)
        label = self.FILTER_LABELS.get(normalized_field, normalized_field)
        await self._send_filters_menu(note=f"Сохранено: {label} ({len(words)})")

    async def _save_scan_input(self, *, chat_key: str, field: str, value: str) -> None:
        normalized_field = field.strip().lower()
        if normalized_field not in self.SCAN_RUNTIME_KEYS:
            self._pending_profile_input.pop(chat_key, None)
            await self.notifier.send_text("Неизвестная настройка сканирования", reply_markup=self._kb_main())
            return

        lowered = value.strip().lower()
        reset_requested = lowered in {"default", "по умолчанию", "сброс", "reset", "clear"}
        runtime_key = self.SCAN_RUNTIME_KEYS[normalized_field]

        if reset_requested:
            await self.store.set_runtime_value(runtime_key, "")
            self._pending_profile_input.pop(chat_key, None)
            label = self.SCAN_LABELS.get(normalized_field, normalized_field)
            await self._send_scan_menu(note=f"Сброшено: {label}")
            return

        try:
            parsed = int(value.strip())
        except ValueError:
            await self.notifier.send_text("Нужно целое число. Пример: 5")
            return

        if normalized_field == "interval_seconds":
            clamped = max(3, min(120, parsed))
        elif normalized_field == "max_pages":
            clamped = max(1, min(30, parsed))
        elif normalized_field == "max_leads":
            clamped = max(1, min(200, parsed))
        elif normalized_field == "burst_limit":
            clamped = max(0, min(200, parsed))
        else:
            clamped = max(1, min(180, parsed))

        await self.store.set_runtime_value(runtime_key, str(clamped))
        self._pending_profile_input.pop(chat_key, None)
        label = self.SCAN_LABELS.get(normalized_field, normalized_field)
        await self._send_scan_menu(note=f"Сохранено: {label} = {clamped}")

    async def _get_effective_filters(self) -> dict[str, object]:
        min_score = settings.min_score_to_apply
        raw_min = await self.store.get_runtime_value(self.FILTER_RUNTIME_KEYS["min_score"])
        if raw_min:
            with suppress(ValueError):
                min_score = min(1.0, max(0.0, float(raw_min.replace(",", "."))))

        raw_kw = await self.store.get_runtime_value(self.FILTER_RUNTIME_KEYS["keywords"])
        raw_neg = await self.store.get_runtime_value(self.FILTER_RUNTIME_KEYS["negative_keywords"])
        keywords = self._keywords_from_text(raw_kw) if raw_kw else settings.keyword_list
        negative_keywords = self._keywords_from_text(raw_neg) if raw_neg else settings.negative_keyword_list
        return {
            "min_score": float(min_score),
            "keywords": keywords,
            "negative_keywords": negative_keywords,
        }

    async def _get_effective_scan_settings(self) -> dict[str, int]:
        interval_seconds = settings.poll_interval_seconds
        max_pages = settings.max_pages_per_platform_scan
        max_leads = settings.max_leads_per_platform
        burst_limit = settings.telegram_platform_burst_limit
        burst_window_minutes = settings.telegram_platform_burst_window_minutes

        raw_interval = await self.store.get_runtime_value(self.SCAN_RUNTIME_KEYS["interval_seconds"])
        if raw_interval:
            with suppress(ValueError):
                interval_seconds = int(raw_interval)

        raw_pages = await self.store.get_runtime_value(self.SCAN_RUNTIME_KEYS["max_pages"])
        if raw_pages:
            with suppress(ValueError):
                max_pages = int(raw_pages)

        raw_leads = await self.store.get_runtime_value(self.SCAN_RUNTIME_KEYS["max_leads"])
        if raw_leads:
            with suppress(ValueError):
                max_leads = int(raw_leads)

        raw_burst_limit = await self.store.get_runtime_value(self.SCAN_RUNTIME_KEYS["burst_limit"])
        if raw_burst_limit:
            with suppress(ValueError):
                burst_limit = int(raw_burst_limit)

        raw_burst_window = await self.store.get_runtime_value(self.SCAN_RUNTIME_KEYS["burst_window_minutes"])
        if raw_burst_window:
            with suppress(ValueError):
                burst_window_minutes = int(raw_burst_window)

        return {
            "interval_seconds": max(3, min(120, int(interval_seconds))),
            "max_pages": max(1, min(30, int(max_pages))),
            "max_leads": max(1, min(200, int(max_leads))),
            "burst_limit": max(0, min(200, int(burst_limit))),
            "burst_window_minutes": max(1, min(180, int(burst_window_minutes))),
        }

    async def _get_effective_language_mode(self) -> dict[str, object]:
        mode = "ru"
        raw_mode = await self.store.get_runtime_value(self.LANGUAGE_RUNTIME_KEY)
        if raw_mode in self.LANGUAGE_MODES:
            mode = str(raw_mode)
        elif settings.language_list == {"en"}:
            mode = "en"
        elif settings.language_list == {"ru", "en"}:
            mode = "mixed"
        languages = tuple(self.LANGUAGE_MODES.get(mode, ("ru",)))
        return {
            "mode": mode,
            "label": self.LANGUAGE_LABELS.get(mode, mode),
            "languages": languages,
        }

    async def _get_auto_apply_limits_snapshot(self) -> dict[str, int]:
        hour_limit = max(0, int(settings.auto_apply_hour_limit))
        day_limit = max(0, int(settings.auto_apply_day_limit))
        hour_used = await self.store.count_apply_attempts_since(hours=1)
        day_used = await self.store.count_apply_attempts_since(hours=24)
        return {
            "hour_limit": hour_limit,
            "day_limit": day_limit,
            "hour_used": hour_used,
            "day_used": day_used,
        }

    def _keywords_from_text(self, raw: str | None) -> list[str]:
        if raw is None:
            return []
        return [x.strip().lower() for x in re.split(r"[,\n]", raw) if x.strip()]

    def _keywords_preview(self, words: list[str], limit: int = 6) -> str:
        if not words:
            return "-"
        if len(words) <= limit:
            return ", ".join(words)
        head = ", ".join(words[:limit])
        return f"{head} ... (+{len(words) - limit})"

    def _session_file_path(self, platform: str) -> Path:
        cfg = self.platforms_cfg.get(platform, {})
        filename = cfg.get("session_file", f"{platform}.json")
        return settings.sessions_path / filename

    def _compose_profile_text(self, global_profile: str, platform_profile: dict[str, str]) -> str:
        chunks: list[str] = []
        base = global_profile.strip()
        if base:
            chunks.append(base)

        name = (platform_profile.get("name") or "").strip()
        headline = (platform_profile.get("headline") or "").strip()
        resume = (platform_profile.get("resume") or "").strip()
        rates = (platform_profile.get("rates") or "").strip()

        platform_lines: list[str] = []
        if name:
            platform_lines.append(f"Имя на площадке: {name}")
        if headline:
            platform_lines.append(f"Заголовок: {headline}")
        if resume:
            platform_lines.append(f"Описание: {resume}")
        if rates:
            platform_lines.append(f"Ставки: {rates}")
        if platform_lines:
            chunks.append("\n".join(platform_lines))

        return "\n\n".join(chunks).strip()

    def _merge_portfolio_urls(self, global_urls: list[str], platform_raw: str) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for url in global_urls:
            cleaned = url.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                merged.append(cleaned)
        for url in self._portfolio_urls(platform_raw):
            if url not in seen:
                seen.add(url)
                merged.append(url)
        return merged

    def _yes_no(self, value: bool) -> str:
        return "Да" if value else "Нет"

    def _format_lead_publication(self, item: dict[str, object]) -> str:
        raw_date = str(item.get("raw_date") or "").strip()
        if raw_date:
            return raw_date
        published_at = str(item.get("published_at") or "").strip()
        if not published_at:
            return "-"
        try:
            dt = datetime.fromisoformat(published_at)
        except ValueError:
            return published_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self._display_tz).strftime("%d.%m.%Y %H:%M МСК")

    async def _maybe_notify_platform_runtime_change(
        self,
        *,
        platform: str,
        previous: dict[str, object] | None,
        found: int,
        new: int,
        error: str | None,
    ) -> None:
        prev_state = str((previous or {}).get("state", "unknown"))
        prev_error = str((previous or {}).get("last_error", "") or "").strip()
        display = str(self.platforms_cfg.get(platform, {}).get("display_name", platform))

        current_error = (error or "").strip()
        if current_error:
            if prev_state == "error" and prev_error == current_error:
                return
            if self._is_session_expired_error(current_error):
                text = (
                    f"Сессия истекла: {display}\n"
                    "Нужен повторный вход. Открой Аккаунты, выбери площадку и переподключи сессию."
                )
            else:
                text = f"Ошибка площадки {display}: {compact(current_error, 220)}"
            await self.notifier.send_text(text, reply_markup=self._kb_main())
            await self.store.record_event(
                None,
                "platform_runtime_alert",
                {"platform": platform, "state": "error", "error": current_error},
            )
            return

        if prev_state == "error":
            await self.notifier.send_text(
                f"Площадка снова в работе: {display}\n"
                f"Найдено: {found} | Новых: {new}",
                reply_markup=self._kb_main(),
            )
            await self.store.record_event(
                None,
                "platform_runtime_alert",
                {"platform": platform, "state": "recovered", "found": found, "new": new},
            )

    def _platform_state_label(self, state: str, error: str = "") -> str:
        normalized = (state or "").strip().lower()
        if normalized == "error" and self._is_session_expired_error(error):
            return "Сессия истекла"
        if normalized == "ok":
            return "OK"
        if normalized == "error":
            return "Ошибка"
        return "Нет данных"

    def _is_session_expired_error(self, error: str) -> bool:
        text = (error or "").strip().lower()
        if not text:
            return False
        return (
            "session_expired" in text
            or "login required" in text
            or "requires re-authentication" in text
            or "требуется повторный вход" in text
        )

    def _format_iso_dt(self, value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "-"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self._display_tz).strftime("%d.%m %H:%M")

    async def _generate_proposal_for_lead(
        self,
        lead_id: int,
        source: str,
        custom_request: str | None = None,
    ) -> None:
        lead = await self.store.get_lead_by_id(lead_id)
        if not lead:
            await self.notifier.send_text(f"Лид не найден: {lead_id}", reply_markup=self._kb_main())
            return

        profile = await self.store.get_profile()
        platform_profile = await self.store.get_platform_profile(lead.platform)
        global_profile_text = (profile.get("resume") or settings.freelancer_profile).strip()
        global_portfolio_urls = self._portfolio_urls(profile.get("portfolio_urls", "")) or settings.portfolio_list
        profile_text = self._compose_profile_text(global_profile_text, platform_profile)
        portfolio_urls = self._merge_portfolio_urls(
            global_portfolio_urls,
            platform_profile.get("portfolio_urls", ""),
        )
        examples = await self.store.get_success_examples(language=lead.language, limit=4)

        draft = await self.orchestrator.proposal_service.create(
            lead,
            examples=examples,
            profile_text=profile_text,
            portfolio_urls=portfolio_urls,
            custom_request=custom_request,
        )
        await self.store.save_proposal(lead_id, draft)
        await self.notifier.send_draft(draft, lead_id=lead_id)
        await self.store.record_event(
            lead_id,
            "proposal_created_manual",
            {
                "source": source,
                "language": draft.language,
                "chars": len(draft.text),
                "examples_used": len(examples),
            },
        )

    async def _save_feedback_by_lead_id(self, *, lead_id: int, verdict: str, note: str) -> None:
        if verdict not in {"good", "bad", "neutral"}:
            await self.notifier.send_text("Некорректная оценка", reply_markup=self._kb_main())
            return
        await self.store.save_feedback(lead_id=lead_id, verdict=verdict, note=note)
        await self.store.record_event(
            lead_id,
            "feedback_saved",
            {"verdict": verdict, "note": note},
        )
        await self.notifier.send_text(f"Оценка сохранена: lead_id={lead_id}, verdict={verdict}")

    async def stop(self) -> None:
        self._stop_event.set()
        self._run_now_event.set()

        for task in (self._worker_task, self._control_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        await self.notifier.send_text("Бот остановлен")
        await self.notifier.close()


async def main() -> None:
    worker = Worker()
    await worker.start()
    try:
        await asyncio.Future()
    finally:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(main())
