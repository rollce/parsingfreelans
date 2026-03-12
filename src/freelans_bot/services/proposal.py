from __future__ import annotations

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_fixed

from freelans_bot.config.settings import settings
from freelans_bot.core.models import Lead, ProposalDraft, ProposalExample
from freelans_bot.utils.text import compact, detect_language


class ProposalService:
    def __init__(self) -> None:
        provider = settings.llm_provider.strip().lower()
        self.model = settings.openrouter_model
        self.client: AsyncOpenAI | None = None

        if provider == "openrouter":
            if not settings.openrouter_api_key:
                return
            headers: dict[str, str] = {}
            if settings.openrouter_site_url:
                headers["HTTP-Referer"] = settings.openrouter_site_url
            if settings.openrouter_app_name:
                headers["X-Title"] = settings.openrouter_app_name
            self.client = AsyncOpenAI(
                api_key=settings.openrouter_api_key,
                base_url=settings.openrouter_base_url,
                default_headers=headers,
            )
            self.model = settings.openrouter_model
            return

        if provider == "openai" and settings.openai_api_key:
            self.client = AsyncOpenAI(api_key=settings.openai_api_key)
            self.model = settings.openai_model

    async def create(
        self,
        lead: Lead,
        examples: list[ProposalExample] | None = None,
        profile_text: str | None = None,
        portfolio_urls: list[str] | None = None,
    ) -> ProposalDraft:
        language = self._target_language(lead)
        if not self.client:
            return ProposalDraft(
                lead=lead,
                text=self._fallback(lead, language, profile_text),
                language=language,
            )

        text = await self._ai_generate(
            lead,
            language,
            examples or [],
            profile_text=profile_text,
            portfolio_urls=portfolio_urls,
        )
        if not text.strip():
            text = self._fallback(lead, language, profile_text)
        return ProposalDraft(lead=lead, text=text.strip(), language=language)

    def _target_language(self, lead: Lead) -> str:
        if lead.language in {"ru", "en"}:
            return str(lead.language)
        guessed = detect_language(f"{lead.title}\n{lead.description}")
        if guessed in {"ru", "en"}:
            return guessed
        return "ru"

    def _fallback(self, lead: Lead, language: str, profile_text: str | None = None) -> str:
        intro = (profile_text or "").strip()
        if intro:
            intro = compact(intro, 220)
        if language == "en":
            return (
                f"Hello! I can deliver your task \"{lead.title}\" with clean code and clear communication. "
                f"{intro + ' ' if intro else ''}"
                "I build Telegram bots, parsers, API integrations and AI automation. "
                "I can start today and provide milestones, daily updates, and support after delivery."
            )
        return (
            f"Здравствуйте! Готов качественно выполнить задачу «{lead.title}». "
            f"{intro + ' ' if intro else ''}"
            "Специализируюсь на Telegram-ботах, парсинге, API-интеграциях и AI-автоматизации. "
            "Могу быстро стартовать, зафиксировать этапы и ежедневно отчитываться по прогрессу."
        )

    def _render_examples(self, examples: list[ProposalExample], language: str) -> str:
        filtered = [x for x in examples if x.language == language][:3]
        if not filtered:
            return "No approved examples yet."

        blocks: list[str] = []
        for idx, ex in enumerate(filtered, start=1):
            blocks.append(
                "\n".join(
                    [
                        f"Example #{idx} ({ex.source_platform}):",
                        f"Lead title: {compact(ex.lead_title, 220)}",
                        f"Lead summary: {compact(ex.lead_description, 350)}",
                        f"Winning proposal: {compact(ex.proposal_text, 900)}",
                    ]
                )
            )
        return "\n\n".join(blocks)

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
    async def _ai_generate(
        self,
        lead: Lead,
        language: str,
        examples: list[ProposalExample],
        *,
        profile_text: str | None = None,
        portfolio_urls: list[str] | None = None,
    ) -> str:
        assert self.client is not None

        instruction = (
            "You are a senior freelance proposal writer. "
            "Write compact, specific, and credible proposals. Avoid buzzwords and fake claims."
        )
        desired_language = "Russian" if language == "ru" else "English"
        selected_profile = (profile_text or "").strip() or settings.freelancer_profile
        profile = selected_profile or "Experienced Python freelancer"
        selected_portfolio = portfolio_urls or settings.portfolio_list
        portfolio = "\n".join(f"- {u}" for u in selected_portfolio) if selected_portfolio else "- N/A"
        learning_examples = self._render_examples(examples, language)

        prompt = f"""
Language: {desired_language}

Freelancer profile:
{profile}

Portfolio links:
{portfolio}

Project title:
{lead.title}

Project description:
{compact(lead.description, 2000)}

Approved examples from past successful proposals:
{learning_examples}

Constraints:
- 700-1200 characters
- Start with one personalized sentence based on the project
- Include technical plan in 3-5 bullet points
- Include one short risk/control note
- Finish with a concrete CTA (next step + ETA)
- Do not mention being an AI
- Reuse tone and structure from approved examples but do not copy sentences verbatim
""".strip()

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": prompt},
            ],
            temperature=0.45,
        )
        message = response.choices[0].message.content if response.choices else ""
        return (message or "").strip()
