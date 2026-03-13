from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from freelans_bot.config.settings import settings
from freelans_bot.core.models import ApplyResult, ProposalDraft, ScoredLead
from freelans_bot.utils.text import compact


class TelegramNotifier:
    def __init__(self) -> None:
        self.bot = Bot(token=settings.telegram_bot_token)
        self.chat_id = settings.telegram_chat_id
        self._display_tz = ZoneInfo("Europe/Moscow")

    async def send_text(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=reply_markup)

    async def send_lead_scored(self, scored: ScoredLead, lead_id: int | None = None) -> None:
        lead = scored.lead
        published_text = self._format_publication_time(
            published_at=lead.published_at,
            raw_date=(lead.meta.get("raw_date", "") if lead.meta else ""),
        )
        lead_line = f"ID лида: {lead_id}\n" if lead_id is not None else ""
        text = (
            f"[Новый заказ] {lead.platform}\n"
            f"{lead_line}"
            f"Оценка: {scored.score:.2f}\n"
            f"Заголовок: {lead.title}\n"
            f"Бюджет: {lead.budget or '-'}\n"
            f"Опубликовано: {published_text}\n"
            f"Язык: {lead.language or 'unknown'}\n"
            f"Причины: {'; '.join(scored.reasons)}\n"
            f"Ссылка: {lead.url}\n"
            f"Описание: {compact(lead.description, 450)}"
        )
        rows = [[InlineKeyboardButton(text="Открыть заказ", url=lead.url)]]
        if lead_id is not None:
            rows.append(
                [InlineKeyboardButton(text="Сгенерировать отклик", callback_data=f"gen:{lead_id}")]
            )
            rows.append(
                [InlineKeyboardButton(text="Свой запрос к ИИ", callback_data=f"aiw:{lead_id}")]
            )
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=kb)

    async def send_draft(self, draft: ProposalDraft, lead_id: int | None = None) -> None:
        lead = draft.lead
        lead_line = f"ID лида: {lead_id}\n" if lead_id is not None else ""
        text = (
            f"[Черновик] {lead.platform}\n"
            f"{lead_line}"
            f"Проект: {lead.title}\n"
            f"Язык: {draft.language}\n"
            f"Отклик:\n{compact(draft.text, 1200)}"
        )
        rows = [[InlineKeyboardButton(text="Открыть заказ", url=lead.url)]]
        if lead_id is not None:
            rows.append(
                [
                    InlineKeyboardButton(text="Хорошо", callback_data=f"fb:good:{lead_id}"),
                    InlineKeyboardButton(text="Плохо", callback_data=f"fb:bad:{lead_id}"),
                ]
            )
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=kb)

    async def send_apply_result(self, lead_url: str, result: ApplyResult) -> None:
        text = (
            f"[Отклик {'OK' if result.ok else 'FAIL'}] {result.platform}\n"
            f"Сообщение: {result.message}\n"
            f"Заказ: {lead_url}\n"
            f"Отклик: {result.proposal_url or '-'}\n"
            f"Чат: {result.chat_url or '-'}"
        )
        rows = [[InlineKeyboardButton(text="Открыть заказ", url=lead_url)]]
        if result.proposal_url:
            rows.append([InlineKeyboardButton(text="Открыть отклик", url=result.proposal_url)])
        if result.chat_url:
            rows.append([InlineKeyboardButton(text="Открыть чат", url=result.chat_url)])

        await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def close(self) -> None:
        await self.bot.session.close()

    def _format_publication_time(self, *, published_at: datetime | None, raw_date: str) -> str:
        raw = (raw_date or "").strip()
        if raw:
            return raw
        if not published_at:
            return "-"
        dt = published_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self._display_tz).strftime("%d.%m.%Y %H:%M МСК")
