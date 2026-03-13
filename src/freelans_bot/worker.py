from __future__ import annotations

import asyncio
import re
import time
from contextlib import suppress
from pathlib import Path

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

        summary = await self.orchestrator.run_cycle_with_options(
            auto_apply=self.auto_apply,
            auto_generate_drafts=self.auto_apply,
            adapters=adapters,
            profile_text=profile_text,
            portfolio_urls=portfolio_urls,
            platform_profiles=platform_profiles,
        )

        payload = {
            "trigger": trigger,
            "found": summary["found"],
            "new": summary["new"],
            "applied": summary["applied"],
            "paused": self.paused,
            "auto_apply": self.auto_apply,
            "enabled_platforms": [a.name for a in adapters],
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
        timeout = max(0.0, float(settings.poll_interval_seconds) - max(0.0, elapsed_seconds))
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
        elif data == "menu:accounts":
            await self._send_accounts(callback=callback)
        elif data == "menu:profile":
            await self._send_profile_menu(callback=callback)
        elif data == "menu:settings":
            await self._send_settings(callback=callback)
        elif data == "act:cycle":
            self._run_now_event.set()
            await self._send_status(callback=callback, header="Ручной запуск цикла поставлен в очередь.")
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
        text = (
            f"{header + '\n' if header else ''}"
            "Статус:\n"
            f"Пауза: {self._yes_no(self.paused)}\n"
            f"Автоотклик: {self._yes_no(self.auto_apply)}\n"
            f"Активных платформ: {len(adapters)}\n"
            f"Новые: {stats.get('new', 0)}\n"
            f"Черновики: {stats.get('drafted', 0)}\n"
            f"Отправлено: {stats.get('applied', 0)}\n"
            f"Ошибки: {stats.get('failed', 0)}\n"
            f"Пропущено: {stats.get('skipped', 0)}"
        )
        await self._render_menu(text, self._kb_main(), callback=callback)

    async def _send_recent_leads(self, callback: CallbackQuery | None = None) -> None:
        leads = await self.store.recent_leads(
            limit=8,
            min_score=settings.min_score_to_apply,
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

        lines: list[str] = ["Последние вакансии:"]
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for item in leads:
            lines.append(
                "\n".join(
                    [
                        f"#{item['id']} | {item['platform']} | score={item['score']:.2f}",
                        f"{compact(item['title'], 110)}",
                        f"Бюджет: {item['budget'] or '-'} | Статус: {item['status']}",
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
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=pause_text, callback_data="toggle:pause")],
                [InlineKeyboardButton(text=auto_text, callback_data="toggle:auto")],
                [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
            ]
        )

    async def _send_settings(self, callback: CallbackQuery | None = None) -> None:
        await self._render_menu(
            "Настройки:\n"
            f"Пауза: {self._yes_no(self.paused)}\n"
            f"Автоотклик: {self._yes_no(self.auto_apply)}\n\n"
            "Если автоотклик выключен, бот только парсит и дает кнопку генерации.",
            self._kb_settings(),
            callback=callback,
        )

    def _portfolio_urls(self, raw: str) -> list[str]:
        return [x.strip() for x in re.split(r"[,\n]", raw) if x.strip()]

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
