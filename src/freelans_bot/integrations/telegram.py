from __future__ import annotations

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from freelans_bot.config.settings import settings
from freelans_bot.core.models import ApplyResult, ProposalDraft, ScoredLead
from freelans_bot.utils.text import compact


class TelegramNotifier:
    def __init__(self) -> None:
        self.bot = Bot(token=settings.telegram_bot_token)
        self.chat_id = settings.telegram_chat_id

    async def send_text(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=reply_markup)

    async def send_lead_scored(self, scored: ScoredLead, lead_id: int | None = None) -> None:
        lead = scored.lead
        lead_line = f"Lead ID: {lead_id}\n" if lead_id is not None else ""
        text = (
            f"[NEW] {lead.platform}\n"
            f"{lead_line}"
            f"Score: {scored.score:.2f}\n"
            f"Title: {lead.title}\n"
            f"Budget: {lead.budget or '-'}\n"
            f"Lang: {lead.language or 'unknown'}\n"
            f"Reasons: {'; '.join(scored.reasons)}\n"
            f"URL: {lead.url}\n"
            f"Desc: {compact(lead.description, 450)}"
        )
        rows = [[InlineKeyboardButton(text="Open job", url=lead.url)]]
        if lead_id is not None:
            rows.append([InlineKeyboardButton(text="Generate proposal", callback_data=f"gen:{lead_id}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=kb)

    async def send_draft(self, draft: ProposalDraft, lead_id: int | None = None) -> None:
        lead = draft.lead
        lead_line = f"Lead ID: {lead_id}\n" if lead_id is not None else ""
        text = (
            f"[DRAFT] {lead.platform}\n"
            f"{lead_line}"
            f"{lead.title}\n"
            f"Language: {draft.language}\n"
            f"Proposal:\n{compact(draft.text, 1200)}"
        )
        rows = [[InlineKeyboardButton(text="Open job", url=lead.url)]]
        if lead_id is not None:
            rows.append(
                [
                    InlineKeyboardButton(text="Mark Good", callback_data=f"fb:good:{lead_id}"),
                    InlineKeyboardButton(text="Mark Bad", callback_data=f"fb:bad:{lead_id}"),
                ]
            )
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=kb)

    async def send_apply_result(self, lead_url: str, result: ApplyResult) -> None:
        text = (
            f"[APPLY {'OK' if result.ok else 'FAIL'}] {result.platform}\n"
            f"Message: {result.message}\n"
            f"Lead: {lead_url}\n"
            f"Proposal: {result.proposal_url or '-'}\n"
            f"Chat: {result.chat_url or '-'}"
        )
        rows = [[InlineKeyboardButton(text="Open job", url=lead_url)]]
        if result.proposal_url:
            rows.append([InlineKeyboardButton(text="Open proposal", url=result.proposal_url)])
        if result.chat_url:
            rows.append([InlineKeyboardButton(text="Open chat", url=result.chat_url)])

        await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def close(self) -> None:
        await self.bot.session.close()
