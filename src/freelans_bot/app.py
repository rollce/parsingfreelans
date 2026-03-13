from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

from freelans_bot.config.settings import settings
from freelans_bot.worker import Worker

worker: Worker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker
    worker = Worker()
    await worker.start()
    try:
        yield
    finally:
        if worker:
            await worker.stop()


app = FastAPI(title="freelans-bot", lifespan=lifespan)


class FeedbackIn(BaseModel):
    lead_id: int | None = Field(default=None)
    lead_url: str | None = Field(default=None)
    verdict: str = Field(description="good | bad | neutral")
    note: str = Field(default="")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/stats")
async def stats() -> dict:
    if not worker:
        return {"error": "worker is not started"}
    data = await worker.store.stats()
    data["pending_delivery"] = await worker.store.count_pending_lead_notifications(
        min_score=settings.min_score_to_apply,
        exclude_skipped=True,
        max_attempts=settings.telegram_notify_max_attempts,
    )
    data["paused"] = worker.paused
    data["auto_apply"] = worker.auto_apply
    return data


@app.get("/events")
async def events(limit: int = 30) -> list[dict]:
    if not worker:
        return []
    return await worker.store.recent_events(limit=limit)


@app.get("/leads")
async def leads(
    limit: int = 20,
    min_score: float = settings.min_score_to_apply,
    exclude_skipped: bool = True,
) -> list[dict]:
    if not worker:
        return []
    return await worker.store.recent_leads(
        limit=min(limit, 100),
        min_score=max(0.0, min_score),
        exclude_skipped=exclude_skipped,
    )


@app.post("/feedback")
async def feedback(payload: FeedbackIn) -> dict:
    if not worker:
        return {"ok": False, "error": "worker is not started"}

    lead_id = payload.lead_id
    if lead_id is None:
        if not payload.lead_url:
            return {"ok": False, "error": "pass lead_id or lead_url"}
        lead_id = await worker.store.find_lead_id_by_url(payload.lead_url)
        if lead_id is None:
            return {"ok": False, "error": "lead not found by url"}

    try:
        await worker.store.save_feedback(lead_id, payload.verdict, payload.note)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    await worker.store.record_event(
        lead_id,
        "feedback_saved",
        {"verdict": payload.verdict.strip().lower(), "note": payload.note},
    )
    return {"ok": True, "lead_id": lead_id}


@app.get("/learning/examples")
async def learning_examples(language: str = "ru", limit: int = 10) -> list[dict]:
    if not worker:
        return []
    examples = await worker.store.get_success_examples(language=language, limit=min(limit, 30))
    out: list[dict] = []
    for ex in examples:
        out.append(
            {
                "lead_title": ex.lead_title,
                "lead_description": ex.lead_description,
                "proposal_text": ex.proposal_text,
                "language": ex.language,
                "source_platform": ex.source_platform,
                "created_at": ex.created_at.isoformat() if ex.created_at else None,
            }
        )
    return out
