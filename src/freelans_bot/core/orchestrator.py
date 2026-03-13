from __future__ import annotations

import re

from freelans_bot.adapters.base import BasePlatformAdapter
from freelans_bot.config.settings import settings
from freelans_bot.integrations.telegram import TelegramNotifier
from freelans_bot.services.proposal import ProposalService
from freelans_bot.services.proposal_validation import ProposalValidator
from freelans_bot.services.scoring import LeadScorer
from freelans_bot.storage.db import SQLiteStore


class Orchestrator:
    def __init__(
        self,
        adapters: list[BasePlatformAdapter],
        store: SQLiteStore,
        scorer: LeadScorer,
        proposal_service: ProposalService,
        proposal_validator: ProposalValidator,
        notifier: TelegramNotifier,
    ) -> None:
        self.adapters = adapters
        self.store = store
        self.scorer = scorer
        self.proposal_service = proposal_service
        self.proposal_validator = proposal_validator
        self.notifier = notifier

    async def run_cycle(self, *, auto_apply: bool | None = None) -> dict[str, int]:
        return await self.run_cycle_with_options(
            auto_apply=auto_apply,
            auto_generate_drafts=True,
            adapters=None,
            profile_text=None,
            portfolio_urls=None,
            platform_profiles=None,
        )

    async def run_cycle_with_options(
        self,
        *,
        auto_apply: bool | None = None,
        auto_generate_drafts: bool = True,
        adapters: list[BasePlatformAdapter] | None = None,
        profile_text: str | None = None,
        portfolio_urls: list[str] | None = None,
        platform_profiles: dict[str, dict[str, str]] | None = None,
        min_score_to_apply: float | None = None,
        max_leads_per_platform: int | None = None,
        max_pages_per_platform: int | None = None,
    ) -> dict[str, int]:
        total_found = 0
        total_new = 0
        total_applied = 0
        total_validation_failed = 0
        per_platform: list[dict[str, object]] = []
        should_apply = settings.auto_apply if auto_apply is None else auto_apply
        should_generate = auto_generate_drafts or should_apply
        apply_hour_limit = max(0, int(settings.auto_apply_hour_limit))
        apply_day_limit = max(0, int(settings.auto_apply_day_limit))
        apply_attempts_last_hour = await self.store.count_apply_attempts_since(hours=1) if should_apply else 0
        apply_attempts_last_day = await self.store.count_apply_attempts_since(hours=24) if should_apply else 0
        similarity_window = max(5, int(self.proposal_validator.similarity_window))
        recent_proposals_cache = (
            await self.store.recent_proposal_texts(limit=similarity_window)
            if should_apply
            else []
        )
        active_adapters = adapters if adapters is not None else self.adapters
        score_threshold = settings.min_score_to_apply if min_score_to_apply is None else max(0.0, min_score_to_apply)
        leads_limit = settings.max_leads_per_platform if max_leads_per_platform is None else max(1, max_leads_per_platform)
        pages_limit = (
            settings.max_pages_per_platform_scan
            if max_pages_per_platform is None
            else max(1, max_pages_per_platform)
        )

        for adapter in active_adapters:
            platform_found = 0
            platform_new = 0
            platform_error: str | None = None
            platform_validation_failed = 0
            passed_preview: list[dict[str, object]] = []
            try:
                since = await self.store.get_last_seen_time(adapter.name)
                leads = await adapter.fetch_new_leads(
                    since=since,
                    limit=leads_limit,
                    max_pages=pages_limit,
                )
                platform_found = len(leads)
                total_found += len(leads)

                await self.store.record_event(
                    None,
                    "fetch_done",
                    {"platform": adapter.name, "found": len(leads)},
                )

                for lead in leads:
                    scored = self.scorer.score(lead)
                    lead_id, is_new = await self.store.upsert_scored_lead(scored)
                    if not is_new:
                        continue

                    total_new += 1
                    platform_new += 1

                    if scored.score < score_threshold:
                        await self.store.mark_skipped(lead_id, "score below apply threshold")
                        await self.store.record_event(
                            lead_id,
                            "lead_skipped",
                            {"score": scored.score, "threshold": score_threshold},
                        )
                        continue

                    await self.store.record_event(
                        lead_id,
                        "lead_queued_for_delivery",
                        {"score": scored.score, "reasons": scored.reasons[:6]},
                    )
                    if len(passed_preview) < 5:
                        passed_preview.append(
                            {
                                "lead_id": lead_id,
                                "title": scored.lead.title[:140],
                                "url": scored.lead.url,
                                "score": round(float(scored.score), 4),
                                "reasons": [str(x)[:120] for x in scored.reasons[:4]],
                            }
                        )

                    if not should_generate:
                        continue

                    examples = await self.store.get_success_examples(language=scored.lead.language, limit=4)
                    platform_profile = (platform_profiles or {}).get(scored.lead.platform.lower(), {})
                    effective_profile = self._compose_profile_text(profile_text, platform_profile)
                    effective_portfolio = self._compose_portfolio_urls(portfolio_urls, platform_profile)
                    draft = await self.proposal_service.create(
                        scored.lead,
                        examples=examples,
                        profile_text=effective_profile,
                        portfolio_urls=effective_portfolio,
                    )
                    await self.store.save_proposal(lead_id, draft)
                    await self.notifier.send_draft(draft, lead_id=lead_id)
                    await self.store.record_event(
                        lead_id,
                        "proposal_created",
                        {
                            "language": draft.language,
                            "chars": len(draft.text),
                            "examples_used": len(examples),
                        },
                    )

                    if not should_apply:
                        continue
                    if total_applied >= settings.max_applies_per_cycle:
                        continue
                    if apply_hour_limit and (apply_attempts_last_hour + total_applied) >= apply_hour_limit:
                        await self.store.record_event(
                            lead_id,
                            "apply_limited",
                            {
                                "reason": "hour_limit",
                                "limit": apply_hour_limit,
                                "used": apply_attempts_last_hour + total_applied,
                            },
                        )
                        continue
                    if apply_day_limit and (apply_attempts_last_day + total_applied) >= apply_day_limit:
                        await self.store.record_event(
                            lead_id,
                            "apply_limited",
                            {
                                "reason": "day_limit",
                                "limit": apply_day_limit,
                                "used": apply_attempts_last_day + total_applied,
                            },
                        )
                        continue

                    validation = self.proposal_validator.validate(
                        text=draft.text,
                        lead_language=scored.lead.language,
                        recent_proposals=recent_proposals_cache,
                    )
                    recent_proposals_cache.insert(0, draft.text)
                    if len(recent_proposals_cache) > similarity_window:
                        recent_proposals_cache = recent_proposals_cache[:similarity_window]
                    if not validation.ok:
                        platform_validation_failed += 1
                        total_validation_failed += 1
                        await self.store.record_event(
                            lead_id,
                            "apply_validation_failed",
                            {
                                "reasons": validation.reasons,
                                "detected_language": validation.detected_language,
                                "max_similarity": validation.max_similarity,
                            },
                        )
                        continue

                    result = await adapter.apply(scored.lead, draft.text)
                    await self.store.mark_result(lead_id, result)
                    await self.notifier.send_apply_result(scored.lead.url, result)
                    await self.store.record_event(
                        lead_id,
                        "apply_done",
                        {
                            "platform": result.platform,
                            "ok": result.ok,
                            "message": result.message,
                            "proposal_url": result.proposal_url,
                            "chat_url": result.chat_url,
                        },
                    )
                    total_applied += 1
            except Exception as exc:
                platform_error = f"{type(exc).__name__}: {exc}"
                await self.store.record_event(
                    None,
                    "adapter_error",
                    {"platform": adapter.name, "error": platform_error},
                )
            finally:
                per_platform.append(
                    {
                        "platform": adapter.name,
                        "found": platform_found,
                        "new": platform_new,
                        "error": platform_error,
                        "passed_preview": passed_preview,
                        "validation_failed": platform_validation_failed,
                    }
                )

        return {
            "found": total_found,
            "new": total_new,
            "applied": total_applied,
            "validation_failed": total_validation_failed,
            "platforms": per_platform,
        }

    def _compose_profile_text(
        self,
        base_profile: str | None,
        platform_profile: dict[str, str],
    ) -> str:
        chunks: list[str] = []
        base = (base_profile or "").strip()
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

    def _compose_portfolio_urls(
        self,
        base_urls: list[str] | None,
        platform_profile: dict[str, str],
    ) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for url in base_urls or []:
            cleaned = url.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                merged.append(cleaned)
        raw_platform = platform_profile.get("portfolio_urls", "")
        for url in re.split(r"[,\n]", raw_platform):
            cleaned = url.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                merged.append(cleaned)
        return merged
