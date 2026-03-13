from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from freelans_bot.config.settings import settings
from freelans_bot.utils.text import detect_language


@dataclass
class ProposalValidationResult:
    ok: bool
    reasons: list[str]
    detected_language: str
    max_similarity: float


class ProposalValidator:
    def __init__(self) -> None:
        self.enabled = bool(settings.proposal_validation_enabled)
        self.min_chars = max(80, int(settings.proposal_min_chars))
        self.max_chars = max(self.min_chars, int(settings.proposal_max_chars))
        self.similarity_threshold = max(0.5, min(0.99, float(settings.proposal_similarity_threshold)))
        self.similarity_window = max(5, min(200, int(settings.proposal_similarity_window)))
        self.banned_phrases = settings.proposal_banned_list

    def validate(
        self,
        *,
        text: str,
        lead_language: str | None,
        recent_proposals: list[str],
    ) -> ProposalValidationResult:
        if not self.enabled:
            return ProposalValidationResult(ok=True, reasons=[], detected_language="unknown", max_similarity=0.0)

        content = (text or "").strip()
        reasons: list[str] = []
        length = len(content)
        if length < self.min_chars:
            reasons.append(f"слишком короткий текст ({length} < {self.min_chars})")
        if length > self.max_chars:
            reasons.append(f"слишком длинный текст ({length} > {self.max_chars})")

        detected = detect_language(content) if content else "unknown"
        expected = (lead_language or "").strip().lower()
        if expected in {"ru", "en"} and detected in {"ru", "en"} and expected != detected:
            reasons.append(f"язык не совпадает с заказчиком ({detected} != {expected})")

        lowered = content.lower()
        hit_phrases = [phrase for phrase in self.banned_phrases if phrase in lowered]
        if hit_phrases:
            reasons.append("обнаружены стоп-фразы: " + ", ".join(hit_phrases[:3]))

        max_similarity = self._max_similarity(content, recent_proposals)
        if max_similarity >= self.similarity_threshold:
            reasons.append(
                f"слишком похож на недавние шаблоны ({max_similarity:.2f} >= {self.similarity_threshold:.2f})"
            )

        return ProposalValidationResult(
            ok=not reasons,
            reasons=reasons,
            detected_language=detected,
            max_similarity=max_similarity,
        )

    def _max_similarity(self, text: str, recent_proposals: list[str]) -> float:
        base = self._normalize(text)
        if not base:
            return 0.0
        best = 0.0
        for candidate in recent_proposals[: self.similarity_window]:
            norm = self._normalize(candidate)
            if not norm:
                continue
            ratio = SequenceMatcher(None, base, norm).ratio()
            if ratio > best:
                best = ratio
        return best

    def _normalize(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip().lower())
