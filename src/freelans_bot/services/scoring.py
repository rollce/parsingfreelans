from __future__ import annotations

from freelans_bot.config.settings import settings
from freelans_bot.core.models import Lead, ScoredLead
from freelans_bot.utils.text import detect_language


class LeadScorer:
    def __init__(self) -> None:
        self.keywords = settings.keyword_list
        self.negative = settings.negative_keyword_list
        self.target_languages = settings.language_list

    def score(self, lead: Lead) -> ScoredLead:
        text = f"{lead.title}\n{lead.description}".lower()
        reasons: list[str] = []

        if not text.strip():
            return ScoredLead(lead=lead, score=0.0, reasons=["empty text"])

        pos_hits = [kw for kw in self.keywords if kw in text]
        neg_hits = [kw for kw in self.negative if kw in text]

        if self.keywords:
            pos_score = min(1.0, len(pos_hits) / max(1, len(self.keywords)) * 1.8)
        else:
            pos_score = 0.5

        neg_penalty = min(0.9, len(neg_hits) * 0.25)

        lang = lead.language or detect_language(text)
        lead.language = lang
        lang_score = 0.15 if lang in self.target_languages or lang == "mixed" else -0.2

        score = max(0.0, min(1.0, pos_score - neg_penalty + lang_score))

        if pos_hits:
            reasons.append(f"positive keywords: {', '.join(pos_hits)}")
        if neg_hits:
            reasons.append(f"negative keywords: {', '.join(neg_hits)}")
        reasons.append(f"language={lang}")

        return ScoredLead(lead=lead, score=score, reasons=reasons)
