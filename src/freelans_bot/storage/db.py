from __future__ import annotations

import json
from datetime import datetime, timedelta
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
                  notified_at TEXT,
                  notify_attempts INTEGER NOT NULL DEFAULT 0,
                  notify_last_attempt_at TEXT,
                  notify_last_error TEXT,
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

                CREATE TABLE IF NOT EXISTS platform_runtime (
                  platform TEXT PRIMARY KEY,
                  state TEXT NOT NULL DEFAULT 'unknown',
                  last_success_at TEXT,
                  last_error_at TEXT,
                  last_error TEXT,
                  last_found INTEGER NOT NULL DEFAULT 0,
                  last_new INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL
                );
                """
            )
            await self._ensure_leads_columns(db)
            await db.commit()

    async def _ensure_leads_columns(self, db: aiosqlite.Connection) -> None:
        cur = await db.execute("PRAGMA table_info(leads)")
        rows = await cur.fetchall()
        columns = {str(row[1]) for row in rows}
        added_notified_at = False
        if "published_at" not in columns:
            await db.execute("ALTER TABLE leads ADD COLUMN published_at TEXT")
        if "raw_date" not in columns:
            await db.execute("ALTER TABLE leads ADD COLUMN raw_date TEXT")
        if "notified_at" not in columns:
            await db.execute("ALTER TABLE leads ADD COLUMN notified_at TEXT")
            added_notified_at = True
        if "notify_attempts" not in columns:
            await db.execute("ALTER TABLE leads ADD COLUMN notify_attempts INTEGER NOT NULL DEFAULT 0")
        if "notify_last_attempt_at" not in columns:
            await db.execute("ALTER TABLE leads ADD COLUMN notify_last_attempt_at TEXT")
        if "notify_last_error" not in columns:
            await db.execute("ALTER TABLE leads ADD COLUMN notify_last_error TEXT")
        if added_notified_at:
            # Existing rows might already have been delivered by previous bot versions.
            # Backfill sent marker once to avoid replaying old backlog after migration.
            await db.execute("UPDATE leads SET notified_at = updated_at WHERE notified_at IS NULL")

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
        published_at = lead.published_at.isoformat() if lead.published_at else None
        raw_date = (lead.meta.get("raw_date", "") if lead.meta else "").strip()
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
                    SET score = ?, score_reasons = ?, updated_at = ?,
                        published_at = COALESCE(published_at, ?),
                        raw_date = COALESCE(NULLIF(raw_date, ''), NULLIF(?, ''))
                    WHERE id = ?
                    """,
                    (
                        scored.score,
                        json.dumps(scored.reasons, ensure_ascii=False),
                        now,
                        published_at,
                        raw_date,
                        existing["id"],
                    ),
                )
                await db.commit()
                return int(existing["id"]), False

            cur = await db.execute(
                """
                INSERT INTO leads (
                  platform, external_id, title, url, description, budget, language,
                  score, score_reasons, status, published_at, raw_date, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    published_at,
                    raw_date or None,
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

    async def recent_leads(
        self,
        *,
        limit: int = 20,
        min_score: float = 0.0,
        exclude_skipped: bool = True,
    ) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            where_parts = ["score >= ?"]
            params: list[object] = [min_score]
            if exclude_skipped:
                where_parts.append("status != ?")
                params.append(LeadStatus.SKIPPED.value)
            where_clause = " AND ".join(where_parts)
            cur = await db.execute(
                """
                SELECT
                  id, platform, title, url, budget, language,
                  score, status, proposal_url, chat_url, error_message,
                  published_at, raw_date, notified_at, notify_attempts, notify_last_error,
                  discovered_at, updated_at
                FROM leads
                WHERE """
                + where_clause
                + """
                ORDER BY id DESC
                LIMIT ?
                """,
                tuple([*params, limit]),
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
                    "proposal_url": row["proposal_url"],
                    "chat_url": row["chat_url"],
                    "error_message": row["error_message"],
                    "published_at": row["published_at"],
                    "raw_date": row["raw_date"],
                    "sent_at": row["notified_at"],
                    "notify_attempts": int(row["notify_attempts"] or 0),
                    "notify_last_error": row["notify_last_error"],
                    "discovered_at": row["discovered_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return out

    async def pending_lead_notifications(
        self,
        *,
        limit: int = 10,
        min_score: float = 0.0,
        exclude_skipped: bool = True,
        retry_after_seconds: int = 45,
        max_attempts: int = 200,
    ) -> list[tuple[int, ScoredLead]]:
        cutoff = (datetime.utcnow() - timedelta(seconds=max(0, retry_after_seconds))).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            where_parts = ["score >= ?", "notified_at IS NULL", "COALESCE(notify_attempts, 0) < ?"]
            params: list[object] = [min_score, max(1, max_attempts)]
            if exclude_skipped:
                where_parts.append("status != ?")
                params.append(LeadStatus.SKIPPED.value)
            where_parts.append("(notify_last_attempt_at IS NULL OR notify_last_attempt_at <= ?)")
            params.append(cutoff)
            where_clause = " AND ".join(where_parts)
            cur = await db.execute(
                """
                SELECT
                  id, platform, external_id, title, url, description, budget, language,
                  score, score_reasons, published_at, raw_date
                FROM leads
                WHERE """
                + where_clause
                + """
                ORDER BY id ASC
                LIMIT ?
                """,
                tuple([*params, max(1, limit)]),
            )
            rows = await cur.fetchall()

        result: list[tuple[int, ScoredLead]] = []
        for row in rows:
            reasons_raw = row["score_reasons"] or "[]"
            try:
                reasons = json.loads(reasons_raw)
                if not isinstance(reasons, list):
                    reasons = [str(reasons)]
            except Exception:
                reasons = []
            lead = Lead(
                platform=row["platform"],
                external_id=row["external_id"],
                title=row["title"],
                url=row["url"],
                description=row["description"] or "",
                budget=row["budget"],
                language=row["language"],
                published_at=self._parse_datetime(row["published_at"]),
                meta={"raw_date": row["raw_date"] or ""},
            )
            result.append(
                (
                    int(row["id"]),
                    ScoredLead(
                        lead=lead,
                        score=float(row["score"] or 0.0),
                        reasons=[str(x) for x in reasons],
                    ),
                )
            )
        return result

    async def mark_lead_notified(self, lead_id: int) -> None:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE leads
                SET notified_at = ?,
                    notify_last_attempt_at = ?,
                    notify_last_error = NULL,
                    notify_attempts = COALESCE(notify_attempts, 0) + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, now, lead_id),
            )
            await db.commit()

    async def mark_lead_notify_failed(self, lead_id: int, error: str) -> None:
        now = datetime.utcnow().isoformat()
        err = (error or "").strip()[:700]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE leads
                SET notify_last_attempt_at = ?,
                    notify_last_error = ?,
                    notify_attempts = COALESCE(notify_attempts, 0) + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, err, now, lead_id),
            )
            await db.commit()

    async def count_pending_lead_notifications(
        self,
        *,
        min_score: float = 0.0,
        exclude_skipped: bool = True,
        max_attempts: int = 200,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            where_parts = ["score >= ?", "notified_at IS NULL", "COALESCE(notify_attempts, 0) < ?"]
            params: list[object] = [min_score, max(1, max_attempts)]
            if exclude_skipped:
                where_parts.append("status != ?")
                params.append(LeadStatus.SKIPPED.value)
            where_clause = " AND ".join(where_parts)
            cur = await db.execute(
                f"SELECT COUNT(*) as cnt FROM leads WHERE {where_clause}",
                tuple(params),
            )
            row = await cur.fetchone()
            return int(row["cnt"] if row else 0)

    async def recent_delivery_counts_by_platform(self, *, window_minutes: int) -> dict[str, int]:
        minutes = max(1, int(window_minutes))
        cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT platform, COUNT(*) AS cnt
                FROM leads
                WHERE notified_at IS NOT NULL
                  AND notified_at >= ?
                GROUP BY platform
                """,
                (cutoff,),
            )
            rows = await cur.fetchall()
        out: dict[str, int] = {}
        for row in rows:
            platform = str(row["platform"] or "").strip().lower()
            if not platform:
                continue
            out[platform] = int(row["cnt"] or 0)
        return out

    async def count_apply_attempts_since(self, *, hours: int) -> int:
        window_hours = max(1, int(hours))
        cutoff = (datetime.utcnow() - timedelta(hours=window_hours)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM events
                WHERE event_type = 'apply_done'
                  AND created_at >= ?
                """,
                (cutoff,),
            )
            row = await cur.fetchone()
            return int(row["cnt"] if row else 0)

    async def update_platform_runtime(
        self,
        *,
        platform: str,
        found: int,
        new: int,
        error: str | None = None,
    ) -> None:
        now = datetime.utcnow().isoformat()
        clean_platform = platform.strip().lower()
        clean_error = (error or "").strip()[:700]
        async with aiosqlite.connect(self.db_path) as db:
            if clean_error:
                await db.execute(
                    """
                    INSERT INTO platform_runtime (
                      platform, state, last_success_at, last_error_at, last_error, last_found, last_new, updated_at
                    ) VALUES (?, 'error', NULL, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform) DO UPDATE SET
                      state = excluded.state,
                      last_error_at = excluded.last_error_at,
                      last_error = excluded.last_error,
                      last_found = excluded.last_found,
                      last_new = excluded.last_new,
                      updated_at = excluded.updated_at
                    """,
                    (
                        clean_platform,
                        now,
                        clean_error,
                        max(0, found),
                        max(0, new),
                        now,
                    ),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO platform_runtime (
                      platform, state, last_success_at, last_error_at, last_error, last_found, last_new, updated_at
                    ) VALUES (?, 'ok', ?, NULL, NULL, ?, ?, ?)
                    ON CONFLICT(platform) DO UPDATE SET
                      state = excluded.state,
                      last_success_at = excluded.last_success_at,
                      last_error_at = NULL,
                      last_error = NULL,
                      last_found = excluded.last_found,
                      last_new = excluded.last_new,
                      updated_at = excluded.updated_at
                    """,
                    (
                        clean_platform,
                        now,
                        max(0, found),
                        max(0, new),
                        now,
                    ),
                )
            await db.commit()

    async def get_platform_runtime(self) -> dict[str, dict[str, object]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                  platform, state, last_success_at, last_error_at, last_error,
                  last_found, last_new, updated_at
                FROM platform_runtime
                ORDER BY platform
                """
            )
            rows = await cur.fetchall()

        out: dict[str, dict[str, object]] = {}
        for row in rows:
            key = str(row["platform"])
            out[key] = {
                "state": row["state"] or "unknown",
                "last_success_at": row["last_success_at"],
                "last_error_at": row["last_error_at"],
                "last_error": row["last_error"],
                "last_found": int(row["last_found"] or 0),
                "last_new": int(row["last_new"] or 0),
                "updated_at": row["updated_at"],
            }
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
                SELECT platform, title, url, description, budget, language, external_id, published_at, raw_date
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
                published_at=self._parse_datetime(row["published_at"]),
                meta={"raw_date": row["raw_date"] or ""},
            )

    def _parse_datetime(self, value: str | None) -> datetime | None:
        raw = (value or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

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
