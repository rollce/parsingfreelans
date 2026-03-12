from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class LeadStatus(str, Enum):
    NEW = "new"
    DRAFTED = "drafted"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class Lead:
    platform: str
    title: str
    url: str
    description: str = ""
    budget: str | None = None
    language: str | None = None
    client_name: str | None = None
    external_id: str | None = None
    published_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScoredLead:
    lead: Lead
    score: float
    reasons: list[str]


@dataclass(slots=True)
class ProposalDraft:
    lead: Lead
    text: str
    language: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class ProposalExample:
    lead_title: str
    lead_description: str
    proposal_text: str
    language: str
    source_platform: str
    created_at: datetime | None = None


@dataclass(slots=True)
class ApplyResult:
    platform: str
    lead_url: str
    ok: bool
    message: str
    proposal_url: str | None = None
    chat_url: str | None = None
