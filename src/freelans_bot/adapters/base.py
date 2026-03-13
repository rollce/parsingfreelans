from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from freelans_bot.core.models import ApplyResult, Lead


class BasePlatformAdapter(ABC):
    name: str

    @abstractmethod
    async def fetch_new_leads(
        self,
        since: datetime | None,
        limit: int,
        *,
        max_pages: int | None = None,
    ) -> list[Lead]:
        raise NotImplementedError

    @abstractmethod
    async def apply(self, lead: Lead, proposal_text: str) -> ApplyResult:
        raise NotImplementedError

    @abstractmethod
    async def sync_profile(self, profile_data: dict[str, str]) -> tuple[bool, str]:
        raise NotImplementedError
