from __future__ import annotations

import asyncio
import re
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


class Worker:
    GLOBAL_PROFILE_HINTS = {
        "name": "Отправь имя для профиля / Send profile name",
        "resume": "Отправь текст резюме / Send resume text",
        "avatar_url": "Отправь ссылку на аватар / Send avatar URL",
        "portfolio_urls": "Отправь ссылки через запятую / Send comma-separated URLs",
    }

    GLOBAL_PROFILE_LABELS = {
        "name": "Имя / Name",
        "resume": "Резюме / Resume",
        "avatar_url": "Аватар URL / Avatar URL",
        "portfolio_urls": "Портфолио URL / Portfolio URLs",
    }

    PLATFORM_PROFILE_HINTS = {
        "name": "Имя на бирже / Name on platform",
        "headline": "Заголовок профиля / Profile headline",
        "resume": "Описание профиля / Platform resume",
        "portfolio_urls": "Ссылки портфолио через запятую / Portfolio URLs",
        "rates": "Ставки (фикс/час) / Rates (fixed/hourly)",
    }

    PLATFORM_PROFILE_LABELS = {
        "name": "Имя / Name",
        "headline": "Заголовок / Headline",
        "resume": "Описание / Resume",
        "portfolio_urls": "Портфолио / Portfolio",
        "rates": "Ставки / Rates",
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
            "Система запущена / System started\n"
            f"Пауза / Paused: {self._yes_no(self.paused)}\n"
            f"Авто-отклик / Auto apply: {self._yes_no(self.auto_apply)}"
        )

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._run_now_event.is_set():
                    self._run_now_event.clear()
                    await self._run_cycle(trigger="manual")
                elif not self.paused:
                    await self._run_cycle(trigger="timer")
                await self._wait_for_next_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.store.record_event(
                    None,
                    "worker_error",
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
                await self.notifier.send_text(f"[Ошибка / Error] Worker: {type(exc).__name__}: {exc}")
                await asyncio.sleep(2)

    async def _run_cycle(self, trigger: str) -> None:
        adapters = await self._get_enabled_adapters()
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
        await self.notifier.send_text(
            "[Цикл / Cycle]\n"
            f"Режим / Trigger: {trigger}\n"
            f"Найдено / Found: {summary['found']}\n"
            f"Новых / New: {summary['new']}\n"
            f"Откликов / Applied: {summary['applied']}\n"
            f"Платформ активно / Enabled platforms: {len(adapters)}\n"
            f"Пауза / Paused: {self._yes_no(self.paused)}\n"
            f"Авто-отклик / Auto apply: {self._yes_no(self.auto_apply)}"
        )

    async def _get_enabled_adapters(self) -> list[BasePlatformAdapter]:
        result: list[BasePlatformAdapter] = []
        for adapter in self.orchestrator.adapters:
            default_enabled = self.platform_defaults.get(adapter.name, True)
            enabled = await self.store.get_runtime_flag(
                f"platform:{adapter.name}:enabled",
                default=default_enabled,
            )
            if enabled:
                result.append(adapter)
        return result

    async def _wait_for_next_tick(self) -> None:
        wait_stop = asyncio.create_task(self._stop_event.wait())
        wait_manual = asyncio.create_task(self._run_now_event.wait())
        try:
            done, pending = await asyncio.wait(
                {wait_stop, wait_manual},
                timeout=settings.poll_interval_seconds,
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

        if text.startswith("/start"):
            self._pending_profile_input.pop(chat_key, None)
            await self._send_main_menu("Главное меню / Main menu")
            return

        pending = self._pending_profile_input.get(chat_key)
        if pending:
            await self._save_profile_input(chat_key, pending, text)
            return

        await self._send_main_menu("Используй /start и кнопки ниже / Use /start and inline buttons")

    async def _handle_callback(self, callback: CallbackQuery) -> None:
        chat_id = callback.message.chat.id if callback.message else None
        if chat_id is None or not self._is_allowed_chat(chat_id):
            with suppress(Exception):
                await self.notifier.bot.answer_callback_query(callback.id)
            return

        data = (callback.data or "").strip()

        if data == "menu:main":
            await self._send_main_menu("Главное меню / Main menu", callback=callback)
        elif data == "menu:status":
            await self._send_status(callback=callback)
        elif data == "menu:accounts":
            await self._send_accounts(callback=callback)
        elif data == "menu:profile":
            await self._send_profile_menu(callback=callback)
        elif data == "menu:settings":
            await self._send_settings(callback=callback)
        elif data == "act:cycle":
            self._run_now_event.set()
            await self._send_status(callback=callback, header="Цикл запущен вручную / Manual cycle requested")
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
        elif data.startswith("ed:"):
            field = data.split(":", 1)[1]
            await self._begin_global_profile_input(str(chat_id), field, callback=callback)
        elif data.startswith("apf:"):
            parts = data.split(":", 2)
            if len(parts) != 3:
                await self._send_main_menu("Некорректный запрос / Invalid action", callback=callback)
            else:
                platform, field = parts[1], parts[2]
                await self._begin_platform_profile_input(str(chat_id), platform, field, callback=callback)
        elif data.startswith("gen:"):
            raw_id = data.split(":", 1)[1]
            if raw_id.isdigit():
                await self._generate_proposal_for_lead(int(raw_id), source="button")
            else:
                await self.notifier.send_text("Некорректный ID лида / Invalid lead id", reply_markup=self._kb_main())
        elif data.startswith("fb:"):
            parts = data.split(":")
            if len(parts) == 3 and parts[2].isdigit():
                verdict = parts[1].strip().lower()
                lead_id = int(parts[2])
                await self._save_feedback_by_lead_id(lead_id=lead_id, verdict=verdict, note="")
            else:
                await self.notifier.send_text(
                    "Некорректный callback фидбека / Invalid feedback callback",
                    reply_markup=self._kb_main(),
                )
        elif data == "noop":
            pass
        else:
            await self._send_main_menu("Неизвестное действие / Unknown action", callback=callback)

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
        message = text or "Главное меню / Main menu"
        await self._render_menu(message, self._kb_main(), callback=callback)

    def _kb_main(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Статус / Status", callback_data="menu:status"),
                    InlineKeyboardButton(text="Аккаунты / Accounts", callback_data="menu:accounts"),
                ],
                [
                    InlineKeyboardButton(text="Профиль / Profile", callback_data="menu:profile"),
                    InlineKeyboardButton(text="Настройки / Settings", callback_data="menu:settings"),
                ],
                [InlineKeyboardButton(text="Запустить цикл / Run cycle", callback_data="act:cycle")],
            ]
        )

    async def _send_status(self, callback: CallbackQuery | None = None, header: str | None = None) -> None:
        stats = await self.store.stats()
        adapters = await self._get_enabled_adapters()
        text = (
            f"{header + '\n' if header else ''}"
            "[Статус / Status]\n"
            f"Пауза / Paused: {self._yes_no(self.paused)}\n"
            f"Авто-отклик / Auto apply: {self._yes_no(self.auto_apply)}\n"
            f"Активных платформ / Enabled platforms: {len(adapters)}\n"
            f"Новые / New: {stats.get('new', 0)}\n"
            f"Черновики / Drafted: {stats.get('drafted', 0)}\n"
            f"Отправлено / Applied: {stats.get('applied', 0)}\n"
            f"Ошибки / Failed: {stats.get('failed', 0)}\n"
            f"Пропущено / Skipped: {stats.get('skipped', 0)}"
        )
        await self._render_menu(text, self._kb_main(), callback=callback)

    async def _send_accounts(self, callback: CallbackQuery | None = None) -> None:
        lines: list[str] = ["[Аккаунты / Accounts]", "Выбери площадку / Choose platform:"]
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
                f"{display} ({key}) | Подключен/Connected: {self._yes_no(connected)} | "
                f"Мониторинг/Monitoring: {self._yes_no(enabled)}"
            )
            keyboard_rows.append([InlineKeyboardButton(text=display, callback_data=f"acc:{key}")])

        keyboard_rows.append([InlineKeyboardButton(text="Назад / Back", callback_data="menu:main")])
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
        login_url = cfg.get("login_url", cfg.get("feed_url", "-"))
        session = self._session_file_path(platform)
        connected = session.exists()
        default_enabled = self.platform_defaults.get(platform, True)
        enabled = await self.store.get_runtime_flag(
            f"platform:{platform}:enabled",
            default=default_enabled,
        )
        p = await self.store.get_platform_profile(platform)

        p_name = p.get("name", "-")
        p_headline = p.get("headline", "-")
        p_resume = (p.get("resume", "-") or "-")[:300]
        p_rates = p.get("rates", "-")
        p_portfolio = self._portfolio_urls(p.get("portfolio_urls", ""))
        p_portfolio_text = ", ".join(p_portfolio) if p_portfolio else "-"

        text = (
            f"{note + '\n' if note else ''}"
            f"[Площадка / Platform] {display} ({platform})\n"
            f"Подключен / Connected: {self._yes_no(connected)}\n"
            f"Мониторинг / Monitoring: {self._yes_no(enabled)}\n"
            f"URL входа / Login URL: {login_url}\n\n"
            "[Анкета площадки / Platform profile]\n"
            f"Имя / Name: {p_name}\n"
            f"Заголовок / Headline: {p_headline}\n"
            f"Описание / Resume: {p_resume}\n"
            f"Портфолио / Portfolio: {p_portfolio_text}\n"
            f"Ставки / Rates: {p_rates}"
        )

        toggle_text = (
            "Выключить мониторинг / Disable monitoring"
            if enabled
            else "Включить мониторинг / Enable monitoring"
        )
        logout_text = "Удалить сессию / Logout" if connected else "Сессии нет / No session"
        logout_cb = f"lo:{platform}" if connected else "noop"

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=toggle_text, callback_data=f"pt:{platform}")],
                [InlineKeyboardButton(text=logout_text, callback_data=logout_cb)],
                [
                    InlineKeyboardButton(text="Имя / Name", callback_data=f"apf:{platform}:name"),
                    InlineKeyboardButton(text="Заголовок / Headline", callback_data=f"apf:{platform}:headline"),
                ],
                [InlineKeyboardButton(text="Описание / Resume", callback_data=f"apf:{platform}:resume")],
                [InlineKeyboardButton(text="Портфолио / Portfolio", callback_data=f"apf:{platform}:portfolio_urls")],
                [InlineKeyboardButton(text="Ставки / Rates", callback_data=f"apf:{platform}:rates")],
                [InlineKeyboardButton(text="Открыть вход / Open login", url=login_url)],
                [InlineKeyboardButton(text="К списку / Back to list", callback_data="menu:accounts")],
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
            await self._send_account_detail(
                platform,
                callback=callback,
                note="Сессия удалена / Session removed",
            )
        else:
            await self._send_account_detail(
                platform,
                callback=callback,
                note="Сессия не найдена / Session not found",
            )

    async def _send_profile_menu(self, callback: CallbackQuery | None = None) -> None:
        profile = await self.store.get_profile()
        name = profile.get("name", "")
        resume = profile.get("resume", settings.freelancer_profile)
        avatar = profile.get("avatar_url", "")
        portfolio = self._portfolio_urls(profile.get("portfolio_urls", "")) or settings.portfolio_list
        portfolio_txt = ", ".join(portfolio) if portfolio else "-"

        text = (
            "[Общий профиль / Global profile]\n"
            f"Имя / Name: {name or '-'}\n"
            f"Аватар / Avatar: {avatar or '-'}\n"
            f"Резюме / Resume: {(resume or '-')[:450]}\n"
            f"Портфолио / Portfolio: {portfolio_txt}\n\n"
            "Выбери поле для редактирования / Choose field to edit"
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Имя / Name", callback_data="ed:name")],
                [InlineKeyboardButton(text="Резюме / Resume", callback_data="ed:resume")],
                [InlineKeyboardButton(text="Аватар / Avatar URL", callback_data="ed:avatar_url")],
                [InlineKeyboardButton(text="Портфолио / Portfolio URLs", callback_data="ed:portfolio_urls")],
                [InlineKeyboardButton(text="Назад / Back", callback_data="menu:main")],
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
            f"Режим ввода / Input mode: {label}\n{hint}\n\n"
            "Отправь одно сообщение текстом / Send one plain text message.",
            InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Назад / Back", callback_data="menu:profile")]]
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
            f"Площадка / Platform: {display}\n"
            f"Режим ввода / Input mode: {label}\n{hint}\n\n"
            "Отправь одно сообщение текстом / Send one plain text message.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Назад / Back", callback_data=f"acc:{platform}")],
                ]
            ),
            callback=callback,
        )

    async def _save_profile_input(self, chat_key: str, pending: str, text: str) -> None:
        value = text.strip()
        if not value:
            await self.notifier.send_text("Пустое значение / Empty value", reply_markup=self._kb_main())
            return

        parts = pending.split(":")
        if len(parts) == 2 and parts[0] == "g":
            field = parts[1]
            if field == "portfolio_urls":
                urls = self._portfolio_urls(value)
                await self.store.set_profile_field("portfolio_urls", ",".join(urls))
                self._pending_profile_input.pop(chat_key, None)
                await self.notifier.send_text(
                    f"Сохранено / Saved: {self.GLOBAL_PROFILE_LABELS[field]} ({len(urls)})"
                )
                await self._send_profile_menu()
                return

            await self.store.set_profile_field(field, value)
            self._pending_profile_input.pop(chat_key, None)
            await self.notifier.send_text(f"Сохранено / Saved: {self.GLOBAL_PROFILE_LABELS[field]}")
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
            await self.notifier.send_text(
                f"Сохранено / Saved: {display} -> {label}",
                reply_markup=self._kb_main(),
            )
            await self._send_account_detail(platform)
            return

        self._pending_profile_input.pop(chat_key, None)
        await self.notifier.send_text("Не удалось распознать режим ввода / Input mode not recognized")

    def _kb_settings(self) -> InlineKeyboardMarkup:
        pause_text = (
            "Продолжить / Resume" if self.paused else "Пауза / Pause"
        )
        auto_text = (
            "Авто-отклик: ВКЛ / Auto apply: ON"
            if self.auto_apply
            else "Авто-отклик: ВЫКЛ / Auto apply: OFF"
        )
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=pause_text, callback_data="toggle:pause")],
                [InlineKeyboardButton(text=auto_text, callback_data="toggle:auto")],
                [InlineKeyboardButton(text="Назад / Back", callback_data="menu:main")],
            ]
        )

    async def _send_settings(self, callback: CallbackQuery | None = None) -> None:
        await self._render_menu(
            "[Настройки / Settings]\n"
            f"Пауза / Paused: {self._yes_no(self.paused)}\n"
            f"Авто-отклик / Auto apply: {self._yes_no(self.auto_apply)}\n\n"
            "Если авто-отклик выключен, бот только парсит и дает кнопку генерации.\n"
            "If auto apply is off, bot only parses and offers manual generation button.",
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
            platform_lines.append(f"Platform name: {name}")
        if headline:
            platform_lines.append(f"Platform headline: {headline}")
        if resume:
            platform_lines.append(f"Platform resume: {resume}")
        if rates:
            platform_lines.append(f"Rates: {rates}")
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
        return "Да / Yes" if value else "Нет / No"

    async def _generate_proposal_for_lead(self, lead_id: int, source: str) -> None:
        lead = await self.store.get_lead_by_id(lead_id)
        if not lead:
            await self.notifier.send_text(f"Лид не найден / Lead not found: {lead_id}", reply_markup=self._kb_main())
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
            await self.notifier.send_text("Некорректная оценка / Invalid feedback verdict", reply_markup=self._kb_main())
            return
        await self.store.save_feedback(lead_id=lead_id, verdict=verdict, note=note)
        await self.store.record_event(
            lead_id,
            "feedback_saved",
            {"verdict": verdict, "note": note},
        )
        await self.notifier.send_text(
            f"Оценка сохранена / Feedback saved: lead_id={lead_id}, verdict={verdict}"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        self._run_now_event.set()

        for task in (self._worker_task, self._control_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        await self.notifier.send_text("Система остановлена / System stopped")
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
