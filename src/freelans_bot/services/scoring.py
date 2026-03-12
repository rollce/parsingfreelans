from __future__ import annotations

import re

from freelans_bot.config.settings import settings
from freelans_bot.core.models import Lead, ScoredLead
from freelans_bot.utils.text import detect_language


class LeadScorer:
    def __init__(self) -> None:
        self.keywords = settings.keyword_list
        self.negative = settings.negative_keyword_list
        self.focus_keywords = settings.focus_keyword_list
        self.strict_topic_filter = settings.strict_topic_filter
        self.target_languages = settings.language_list

    def score(self, lead: Lead) -> ScoredLead:
        text = f"{lead.title}\n{lead.description}".lower()
        reasons: list[str] = []

        if not text.strip():
            return ScoredLead(lead=lead, score=0.0, reasons=["empty text"])

        pos_hits = self._match_keywords(text, self.keywords)
        neg_hits = self._match_keywords(text, self.negative)
        focus_hits = self._match_keywords(text, self.focus_keywords)

        if self.keywords:
            pos_score = min(1.0, len(pos_hits) / max(1, len(self.keywords)) * 1.8)
        else:
            pos_score = 0.5

        neg_penalty = min(0.9, len(neg_hits) * 0.25)

        lang = lead.language or detect_language(text)
        lead.language = lang
        lang_score = 0.15 if lang in self.target_languages or lang == "mixed" else -0.2

        score = max(0.0, min(1.0, pos_score - neg_penalty + lang_score))
        if focus_hits:
            focus_bonus = min(0.55, 0.20 + 0.10 * len(focus_hits))
            score = min(1.0, score + focus_bonus)
        elif self.focus_keywords and self.strict_topic_filter:
            score = 0.0

        if pos_hits:
            reasons.append(f"positive keywords: {', '.join(pos_hits)}")
        if neg_hits:
            reasons.append(f"negative keywords: {', '.join(neg_hits)}")
        if focus_hits:
            reasons.append(f"focus keywords: {', '.join(focus_hits)}")
        elif self.focus_keywords and self.strict_topic_filter:
            reasons.append("focus keywords: no match (strict filter)")
        reasons.append(f"language={lang}")

        return ScoredLead(lead=lead, score=score, reasons=reasons)

    def _match_keywords(self, text: str, keywords: list[str]) -> list[str]:
        hits: list[str] = []
        for kw in keywords:
            if self._contains_keyword(text, kw):
                hits.append(kw)
        return hits

    def _contains_keyword(self, text: str, keyword: str) -> bool:
        needle = keyword.strip().lower()
        if not needle:
            return False
        if re.search(r"[\s\-/]", needle):
            return needle in text
        pattern = rf"(?<!\w){re.escape(needle)}(?!\w)"
        return re.search(pattern, text) is not None
