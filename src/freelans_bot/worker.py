from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from pathlib import Path

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
            "[SYSTEM] Freelans bot started\n"
            f"paused={self.paused} auto_apply={self.auto_apply}\n"
            "Use /start to open menu."
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
                await self.notifier.send_text(f"[ERROR] worker: {type(exc).__name__}: {exc}")
                await asyncio.sleep(2)

    async def _run_cycle(self, trigger: str) -> None:
        adapters = await self._get_enabled_adapters()
        profile = await self.store.get_profile()
        profile_text = (profile.get("resume") or settings.freelancer_profile).strip()
        portfolio_urls = self._portfolio_urls(profile.get("portfolio_urls", "")) or settings.portfolio_list

        summary = await self.orchestrator.run_cycle_with_options(
            auto_apply=self.auto_apply,
            auto_generate_drafts=self.auto_apply,
            adapters=adapters,
            profile_text=profile_text,
            portfolio_urls=portfolio_urls,
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
            f"[CYCLE:{trigger}] found={summary['found']} new={summary['new']} "
            f"applied={summary['applied']} enabled={len(adapters)} "
            f"paused={self.paused} auto_apply={self.auto_apply}"
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
            await self._send_main_menu("Main menu")
            return

        pending = self._pending_profile_input.get(chat_key)
        if pending:
            await self._save_profile_input(chat_key, pending, text)
            return

        await self._send_main_menu("Use /start and inline buttons.")

    async def _handle_callback(self, callback: CallbackQuery) -> None:
        chat_id = callback.message.chat.id if callback.message else None
        if chat_id is None or not self._is_allowed_chat(chat_id):
            with suppress(Exception):
                await self.notifier.bot.answer_callback_query(callback.id)
            return

        data = (callback.data or "").strip()

        if data == "menu:main":
            await self._send_main_menu("Main menu")
        elif data == "menu:status":
            await self._send_status()
        elif data == "menu:accounts":
            await self._send_accounts()
        elif data == "menu:profile":
            await self._send_profile_menu()
        elif data == "menu:settings":
            await self._send_settings()
        elif data == "act:cycle":
            self._run_now_event.set()
            await self.notifier.send_text("[CONTROL] manual cycle requested", reply_markup=self._kb_main())
        elif data == "toggle:pause":
            self.paused = not self.paused
            await self.store.set_runtime_flag("paused", self.paused)
            await self._send_settings()
        elif data == "toggle:auto":
            self.auto_apply = not self.auto_apply
            await self.store.set_runtime_flag("auto_apply", self.auto_apply)
            await self._send_settings()
        elif data.startswith("pt:"):
            platform = data.split(":", 1)[1]
            await self._toggle_platform(platform)
        elif data.startswith("lo:"):
            platform = data.split(":", 1)[1]
            await self._logout_platform(platform)
        elif data.startswith("ed:"):
            field = data.split(":", 1)[1]
            await self._begin_profile_input(str(chat_id), field)
        elif data.startswith("gen:"):
            raw_id = data.split(":", 1)[1]
            if raw_id.isdigit():
                await self._generate_proposal_for_lead(int(raw_id), source="button")
            else:
                await self.notifier.send_text("Invalid lead id", reply_markup=self._kb_main())
        elif data.startswith("fb:"):
            parts = data.split(":")
            if len(parts) == 3 and parts[2].isdigit():
                verdict = parts[1].strip().lower()
                lead_id = int(parts[2])
                await self._save_feedback_by_lead_id(lead_id=lead_id, verdict=verdict, note="")
            else:
                await self.notifier.send_text("Invalid feedback callback", reply_markup=self._kb_main())
        elif data == "noop":
            pass
        else:
            await self.notifier.send_text("Unknown action", reply_markup=self._kb_main())

        with suppress(Exception):
            await self.notifier.bot.answer_callback_query(callback.id)

    def _is_allowed_chat(self, chat_id: int | str) -> bool:
        return str(chat_id) == str(settings.telegram_chat_id)

    async def _send_main_menu(self, text: str) -> None:
        await self.notifier.send_text(text, reply_markup=self._kb_main())

    def _kb_main(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Status", callback_data="menu:status"),
                    InlineKeyboardButton(text="Accounts", callback_data="menu:accounts"),
                ],
                [
                    InlineKeyboardButton(text="Profile", callback_data="menu:profile"),
                    InlineKeyboardButton(text="Settings", callback_data="menu:settings"),
                ],
                [InlineKeyboardButton(text="Run Cycle Now", callback_data="act:cycle")],
            ]
        )

    async def _send_status(self) -> None:
        stats = await self.store.stats()
        adapters = await self._get_enabled_adapters()
        await self.notifier.send_text(
            "[STATUS]\n"
            f"paused={self.paused}\n"
            f"auto_apply={self.auto_apply}\n"
            f"enabled_platforms={len(adapters)}\n"
            f"new={stats.get('new', 0)} drafted={stats.get('drafted', 0)} applied={stats.get('applied', 0)}\n"
            f"failed={stats.get('failed', 0)} skipped={stats.get('skipped', 0)}",
            reply_markup=self._kb_main(),
        )

    async def _send_accounts(self) -> None:
        lines: list[str] = ["[ACCOUNTS]"]
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
                f"{display} ({key}) | connected={connected} enabled={enabled}"
            )

            toggle_text = f"{'ON' if enabled else 'OFF'} {key}"
            logout_text = f"Logout {key}" if connected else f"No session {key}"
            logout_cb = f"lo:{key}" if connected else "noop"
            keyboard_rows.append(
                [
                    InlineKeyboardButton(text=toggle_text, callback_data=f"pt:{key}"),
                    InlineKeyboardButton(text=logout_text, callback_data=logout_cb),
                ]
            )
        keyboard_rows.append([InlineKeyboardButton(text="Back", callback_data="menu:main")])

        await self.notifier.send_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )

    async def _toggle_platform(self, platform: str) -> None:
        if platform not in self.platforms_cfg:
            await self.notifier.send_text("Unknown platform", reply_markup=self._kb_main())
            return
        default_enabled = self.platform_defaults.get(platform, True)
        current = await self.store.get_runtime_flag(
            f"platform:{platform}:enabled",
            default=default_enabled,
        )
        await self.store.set_runtime_flag(f"platform:{platform}:enabled", not current)
        await self._send_accounts()

    async def _logout_platform(self, platform: str) -> None:
        if platform not in self.platforms_cfg:
            await self.notifier.send_text("Unknown platform", reply_markup=self._kb_main())
            return
        session = self._session_file_path(platform)
        if session.exists():
            session.unlink()
            await self.notifier.send_text(f"[PLATFORM] session removed: {platform}")
        else:
            await self.notifier.send_text(f"[PLATFORM] session not found: {platform}")
        await self._send_accounts()

    async def _send_profile_menu(self) -> None:
        profile = await self.store.get_profile()
        name = profile.get("name", "")
        resume = profile.get("resume", settings.freelancer_profile)
        avatar = profile.get("avatar_url", "")
        portfolio = self._portfolio_urls(profile.get("portfolio_urls", "")) or settings.portfolio_list
        portfolio_txt = ", ".join(portfolio) if portfolio else "-"

        text = (
            "[PROFILE]\n"
            f"name: {name or '-'}\n"
            f"avatar: {avatar or '-'}\n"
            f"resume: {(resume or '-')[:450]}\n"
            f"portfolio: {portfolio_txt}\n\n"
            "Choose what to edit:"
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Edit Name", callback_data="ed:name")],
                [InlineKeyboardButton(text="Edit Resume", callback_data="ed:resume")],
                [InlineKeyboardButton(text="Edit Avatar URL", callback_data="ed:avatar_url")],
                [InlineKeyboardButton(text="Edit Portfolio URLs", callback_data="ed:portfolio_urls")],
                [InlineKeyboardButton(text="Back", callback_data="menu:main")],
            ]
        )
        await self.notifier.send_text(text, reply_markup=kb)

    async def _begin_profile_input(self, chat_key: str, field: str) -> None:
        if field not in {"name", "resume", "avatar_url", "portfolio_urls"}:
            await self.notifier.send_text("Unknown profile field", reply_markup=self._kb_main())
            return
        self._pending_profile_input[chat_key] = field

        hints = {
            "name": "Send your display name",
            "resume": "Send your resume/about text",
            "avatar_url": "Send avatar URL",
            "portfolio_urls": "Send comma-separated URLs",
        }
        await self.notifier.send_text(
            f"Input mode: {field}\n{hints[field]}\nSend one message now.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="menu:profile")]]
            ),
        )

    async def _save_profile_input(self, chat_key: str, field: str, text: str) -> None:
        value = text.strip()
        if not value:
            await self.notifier.send_text("Empty value. Try again.", reply_markup=self._kb_main())
            return

        if field == "portfolio_urls":
            urls = self._portfolio_urls(value)
            await self.store.set_profile_field("portfolio_urls", ",".join(urls))
            self._pending_profile_input.pop(chat_key, None)
            await self.notifier.send_text(f"[PROFILE] portfolio saved ({len(urls)} urls)")
            await self._send_profile_menu()
            return

        await self.store.set_profile_field(field, value)
        self._pending_profile_input.pop(chat_key, None)
        await self.notifier.send_text(f"[PROFILE] {field} saved")
        await self._send_profile_menu()

    def _kb_settings(self) -> InlineKeyboardMarkup:
        pause_text = "Resume" if self.paused else "Pause"
        auto_text = "Auto Apply: ON" if self.auto_apply else "Auto Apply: OFF"
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=pause_text, callback_data="toggle:pause")],
                [InlineKeyboardButton(text=auto_text, callback_data="toggle:auto")],
                [InlineKeyboardButton(text="Back", callback_data="menu:main")],
            ]
        )

    async def _send_settings(self) -> None:
        await self.notifier.send_text(
            "[SETTINGS]\n"
            f"paused={self.paused}\n"
            f"auto_apply={self.auto_apply}\n"
            "When auto_apply=false: parse only + manual generate by button.",
            reply_markup=self._kb_settings(),
        )

    def _portfolio_urls(self, raw: str) -> list[str]:
        return [x.strip() for x in re.split(r"[,\n]", raw) if x.strip()]

    def _session_file_path(self, platform: str) -> Path:
        cfg = self.platforms_cfg.get(platform, {})
        filename = cfg.get("session_file", f"{platform}.json")
        return settings.sessions_path / filename

    async def _generate_proposal_for_lead(self, lead_id: int, source: str) -> None:
        lead = await self.store.get_lead_by_id(lead_id)
        if not lead:
            await self.notifier.send_text(f"Lead not found: {lead_id}", reply_markup=self._kb_main())
            return

        profile = await self.store.get_profile()
        profile_text = (profile.get("resume") or settings.freelancer_profile).strip()
        portfolio_urls = self._portfolio_urls(profile.get("portfolio_urls", "")) or settings.portfolio_list
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
            await self.notifier.send_text("Invalid feedback verdict", reply_markup=self._kb_main())
            return
        await self.store.save_feedback(lead_id=lead_id, verdict=verdict, note=note)
        await self.store.record_event(
            lead_id,
            "feedback_saved",
            {"verdict": verdict, "note": note},
        )
        await self.notifier.send_text(f"[LEARNING] feedback saved: lead_id={lead_id}, verdict={verdict}")

    async def stop(self) -> None:
        self._stop_event.set()
        self._run_now_event.set()

        for task in (self._worker_task, self._control_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        await self.notifier.send_text("[SYSTEM] Freelans bot stopped")
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
