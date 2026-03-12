from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from freelans_bot.core.models import ApplyResult, Lead


class BasePlatformAdapter(ABC):
    name: str

    @abstractmethod
    async def fetch_new_leads(self, since: datetime | None, limit: int) -> list[Lead]:
        raise NotImplementedError

    @abstractmethod
    async def apply(self, lead: Lead, proposal_text: str) -> ApplyResult:
        raise NotImplementedError
