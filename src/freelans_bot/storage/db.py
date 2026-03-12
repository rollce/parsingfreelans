from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite

from freelans_bot.core.models import (
    ApplyResult,
    Lead,
    LeadStatus,
    ProposalDraft,
    ProposalExample,
    ScoredLead,
)


class SQLiteStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS leads (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  platform TEXT NOT NULL,
                  external_id TEXT,
                  title TEXT NOT NULL,
                  url TEXT NOT NULL,
                  description TEXT,
                  budget TEXT,
                  language TEXT,
                  score REAL DEFAULT 0,
                  score_reasons TEXT,
                  status TEXT NOT NULL DEFAULT 'new',
                  proposal_url TEXT,
                  chat_url TEXT,
                  error_message TEXT,
                  discovered_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(platform, url)
                );

                CREATE TABLE IF NOT EXISTS proposals (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  lead_id INTEGER NOT NULL,
                  language TEXT NOT NULL,
                  text TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(lead_id) REFERENCES leads(id)
                );

                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  lead_id INTEGER,
                  event_type TEXT NOT NULL,
                  payload TEXT,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(lead_id) REFERENCES leads(id)
                );

                CREATE TABLE IF NOT EXISTS proposal_feedback (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  lead_id INTEGER NOT NULL,
                  verdict TEXT NOT NULL,
                  note TEXT,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(lead_id) REFERENCES leads(id)
                );

                CREATE TABLE IF NOT EXISTS runtime_config (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_profile (
                  field TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS platform_profile (
                  platform TEXT NOT NULL,
                  field TEXT NOT NULL,
                  value TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(platform, field)
                );
                """
            )
            await db.commit()

    async def get_last_seen_time(self, platform: str) -> datetime | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT MAX(discovered_at) as last_seen FROM leads WHERE platform = ?", (platform,)
            )
            row = await cur.fetchone()
            if not row or not row["last_seen"]:
                return None
            return datetime.fromisoformat(row["last_seen"])

    async def upsert_scored_lead(self, scored: ScoredLead) -> tuple[int, bool]:
        lead = scored.lead
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id FROM leads WHERE platform = ? AND url = ?",
                (lead.platform, lead.url),
            )
            existing = await cur.fetchone()
            if existing:
                await db.execute(
                    """
                    UPDATE leads
                    SET score = ?, score_reasons = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (scored.score, json.dumps(scored.reasons, ensure_ascii=False), now, existing["id"]),
                )
                await db.commit()
                return int(existing["id"]), False

            cur = await db.execute(
                """
                INSERT INTO leads (
                  platform, external_id, title, url, description, budget, language,
                  score, score_reasons, status, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lead.platform,
                    lead.external_id,
                    lead.title,
                    lead.url,
                    lead.description,
                    lead.budget,
                    lead.language,
                    scored.score,
                    json.dumps(scored.reasons, ensure_ascii=False),
                    LeadStatus.NEW.value,
                    now,
                    now,
                ),
            )
            await db.commit()
            return int(cur.lastrowid), True

    async def save_proposal(self, lead_id: int, draft: ProposalDraft) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO proposals (lead_id, language, text, created_at) VALUES (?, ?, ?, ?)",
                (lead_id, draft.language, draft.text, draft.created_at.isoformat()),
            )
            await db.execute(
                "UPDATE leads SET status = ?, updated_at = ? WHERE id = ?",
                (LeadStatus.DRAFTED.value, datetime.utcnow().isoformat(), lead_id),
            )
            await db.commit()

    async def mark_skipped(self, lead_id: int, reason: str) -> None:
        await self.mark_result(
            lead_id,
            ApplyResult(platform="", lead_url="", ok=False, message=reason),
            status=LeadStatus.SKIPPED,
        )

    async def mark_result(self, lead_id: int, result: ApplyResult, status: LeadStatus | None = None) -> None:
        final_status = status
        if not final_status:
            final_status = LeadStatus.APPLIED if result.ok else LeadStatus.FAILED

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE leads
                SET status = ?, proposal_url = ?, chat_url = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    final_status.value,
                    result.proposal_url,
                    result.chat_url,
                    None if result.ok else result.message,
                    datetime.utcnow().isoformat(),
                    lead_id,
                ),
            )
            await db.commit()

    async def record_event(self, lead_id: int | None, event_type: str, payload: dict) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO events (lead_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                (lead_id, event_type, json.dumps(payload, ensure_ascii=False), datetime.utcnow().isoformat()),
            )
            await db.commit()

    async def stats(self) -> dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            result: dict[str, int] = {}
            for status in LeadStatus:
                cur = await db.execute("SELECT COUNT(*) as cnt FROM leads WHERE status = ?", (status.value,))
                row = await cur.fetchone()
                result[status.value] = int(row["cnt"] if row else 0)
            return result

    async def recent_events(self, limit: int = 30) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, lead_id, event_type, payload, created_at FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
        result: list[dict] = []
        for row in rows:
            result.append({
                "id": row["id"],
                "lead_id": row["lead_id"],
                "event_type": row["event_type"],
                "payload": row["payload"],
                "created_at": row["created_at"],
            })
        return result

    async def recent_leads(self, *, limit: int = 20, min_score: float = 0.0) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                  id, platform, title, url, budget, language,
                  score, status, discovered_at, updated_at
                FROM leads
                WHERE score >= ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (min_score, limit),
            )
            rows = await cur.fetchall()

        out: list[dict] = []
        for row in rows:
            out.append(
                {
                    "id": int(row["id"]),
                    "platform": row["platform"],
                    "title": row["title"],
                    "url": row["url"],
                    "budget": row["budget"],
                    "language": row["language"],
                    "score": float(row["score"] or 0),
                    "status": row["status"],
                    "discovered_at": row["discovered_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return out

    async def find_lead_id_by_url(self, url: str) -> int | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT id FROM leads WHERE url = ? LIMIT 1", (url,))
            row = await cur.fetchone()
            if not row:
                return None
            return int(row["id"])

    async def get_lead_by_id(self, lead_id: int) -> Lead | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT platform, title, url, description, budget, language, external_id
                FROM leads
                WHERE id = ?
                LIMIT 1
                """,
                (lead_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return Lead(
                platform=row["platform"],
                title=row["title"],
                url=row["url"],
                description=row["description"] or "",
                budget=row["budget"],
                language=row["language"],
                external_id=row["external_id"],
            )

    async def save_feedback(self, lead_id: int, verdict: str, note: str = "") -> None:
        verdict_norm = verdict.strip().lower()
        if verdict_norm not in {"good", "bad", "neutral"}:
            raise ValueError("verdict must be one of: good, bad, neutral")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO proposal_feedback (lead_id, verdict, note, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (lead_id, verdict_norm, note.strip(), datetime.utcnow().isoformat()),
            )
            await db.commit()

    async def get_success_examples(
        self,
        *,
        language: str | None = None,
        limit: int = 4,
    ) -> list[ProposalExample]:
        query = """
            WITH latest_proposals AS (
              SELECT p1.lead_id, p1.language, p1.text, p1.created_at
              FROM proposals p1
              JOIN (
                SELECT lead_id, MAX(id) AS max_id
                FROM proposals
                GROUP BY lead_id
              ) p2 ON p1.id = p2.max_id
            )
            SELECT
              l.title AS lead_title,
              l.description AS lead_description,
              lp.text AS proposal_text,
              lp.language AS language,
              l.platform AS source_platform,
              pf.created_at AS created_at
            FROM proposal_feedback pf
            JOIN leads l ON l.id = pf.lead_id
            JOIN latest_proposals lp ON lp.lead_id = l.id
            WHERE pf.verdict = 'good'
        """
        params: list[object] = []
        if language:
            query += " AND lp.language = ?"
            params.append(language)
        query += " ORDER BY pf.id DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(query, tuple(params))
            rows = await cur.fetchall()

        out: list[ProposalExample] = []
        for row in rows:
            created = row["created_at"]
            out.append(
                ProposalExample(
                    lead_title=row["lead_title"] or "",
                    lead_description=row["lead_description"] or "",
                    proposal_text=row["proposal_text"] or "",
                    language=row["language"] or "ru",
                    source_platform=row["source_platform"] or "unknown",
                    created_at=datetime.fromisoformat(created) if created else None,
                )
            )
        return out

    async def get_runtime_flag(self, key: str, default: bool = False) -> bool:
        value = await self.get_runtime_value(key)
        if value is None:
            return default
        return value in {"1", "true", "yes", "on"}

    async def get_runtime_value(self, key: str) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT value FROM runtime_config WHERE key = ? LIMIT 1", (key,))
            row = await cur.fetchone()
            if not row:
                return None
            return str(row["value"]).strip().lower()

    async def set_runtime_flag(self, key: str, value: bool) -> None:
        await self.set_runtime_value(key, "true" if value else "false")

    async def set_runtime_value(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO runtime_config (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value = excluded.value,
                  updated_at = excluded.updated_at
                """,
                (key, value, datetime.utcnow().isoformat()),
            )
            await db.commit()

    async def get_profile(self) -> dict[str, str]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT field, value FROM user_profile")
            rows = await cur.fetchall()
        result: dict[str, str] = {}
        for row in rows:
            result[str(row["field"])] = str(row["value"])
        return result

    async def set_profile_field(self, field: str, value: str) -> None:
        normalized = field.strip().lower()
        if normalized not in {"name", "resume", "avatar_url", "portfolio_urls"}:
            raise ValueError("Unknown profile field")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_profile (field, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(field) DO UPDATE SET
                  value = excluded.value,
                  updated_at = excluded.updated_at
                """,
                (normalized, value.strip(), datetime.utcnow().isoformat()),
            )
            await db.commit()

    async def get_platform_profile(self, platform: str) -> dict[str, str]:
        normalized_platform = platform.strip().lower()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT field, value FROM platform_profile WHERE platform = ?",
                (normalized_platform,),
            )
            rows = await cur.fetchall()
        result: dict[str, str] = {}
        for row in rows:
            result[str(row["field"])] = str(row["value"])
        return result

    async def get_all_platform_profiles(self) -> dict[str, dict[str, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT platform, field, value FROM platform_profile")
            rows = await cur.fetchall()
        result: dict[str, dict[str, str]] = {}
        for row in rows:
            platform = str(row["platform"])
            field = str(row["field"])
            value = str(row["value"])
            if platform not in result:
                result[platform] = {}
            result[platform][field] = value
        return result

    async def set_platform_profile_field(self, platform: str, field: str, value: str) -> None:
        normalized_platform = platform.strip().lower()
        normalized_field = field.strip().lower()
        if normalized_field not in {"name", "headline", "resume", "portfolio_urls", "rates", "profile_url"}:
            raise ValueError("Unknown platform profile field")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO platform_profile (platform, field, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(platform, field) DO UPDATE SET
                  value = excluded.value,
                  updated_at = excluded.updated_at
                """,
                (
                    normalized_platform,
                    normalized_field,
                    value.strip(),
                    datetime.utcnow().isoformat(),
                ),
            )
            await db.commit()
