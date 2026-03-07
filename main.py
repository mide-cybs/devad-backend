"""
DevRel AI — Simplified FastAPI Backend
Directly answers questions using Claude AI.
"""

import logging
import os
from datetime import datetime

import anthropic
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(
    title="DevRel AI API",
    version="1.0.0",
    description="AI Developer Advocate Agent",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


# ── HEALTH ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "ok", "message": "DevRel AI is running"}


@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}


# ── ASK A QUESTION ─────────────────────────────────────────────────────────────

class AskRequest(BaseModel):
    org_id: str
    platform: str = "discord"
    channel: Optional[str] = None
    author_username: str = ""
    author_external_id: str = ""
    content: str
    thread_context: Optional[List[dict]] = []


@app.post("/questions/ask")
async def ask_question(body: AskRequest):
    log.info(f"Question from {body.author_username}: {body.content[:80]}")

    try:
        # Call Claude directly to answer the question
        message = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system="""You are DevBot, a friendly and helpful AI Developer Advocate. 
Your job is to answer developer questions clearly and accurately.
Keep answers concise but complete. Use code examples when helpful.
If you don't know something specific, say so honestly and suggest where to find the answer.""",
            messages=[
                {"role": "user", "content": body.content}
            ]
        )

        answer = message.content[0].text
        confidence = 85.0

        log.info(f"Answer generated successfully, length={len(answer)}")

        return {
            "action": "auto_posted",
            "answer": answer,
            "confidence": confidence,
            "question_id": "local-" + str(hash(body.content))[:8],
            "response_id": "local-response",
            "sources": [],
        }

    except anthropic.BadRequestError as e:
        log.error(f"Anthropic error: {e}")
        return {
            "action": "escalated",
            "answer": None,
            "confidence": 0,
            "error": str(e),
        }

    except Exception as e:
        log.error(f"Unexpected error: {e}")
        return {
            "action": "escalated",
            "answer": None,
            "confidence": 0,
            "error": str(e),
        }


# ── KNOWLEDGE BASE (simplified) ────────────────────────────────────────────────

class IngestRequest(BaseModel):
    org_id: str
    label: str
    url: str
    max_pages: int = 50


@app.post("/knowledge/ingest/docs", status_code=202)
async def ingest_docs(body: IngestRequest):
    log.info(f"Ingest requested for {body.url}")
    return {
        "source_id": "pending-" + str(hash(body.url))[:8],
        "status": "crawling",
        "message": "Ingestion started. Full RAG pipeline coming in v2."
    }


@app.get("/knowledge/sources/{org_id}")
async def list_sources(org_id: str):
    return []


@app.get("/knowledge/sources/{org_id}/{source_id}")
async def get_source(org_id: str, source_id: str):
    return {"source_id": source_id, "status": "ready", "org_id": org_id}


# ── ANALYTICS (simplified) ────────────────────────────────────────────────────

@app.get("/analytics/{org_id}/metrics")
async def get_metrics(org_id: str):
    return {
        "questions_answered": 0,
        "avg_confidence": 85,
        "escalation_rate": 0.1,
        "top_categories": []
    }


@app.get("/analytics/{org_id}/pain-points")
async def get_pain_points(org_id: str):
    return []


# ── FEEDBACK ──────────────────────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    response_id: str
    rating: int
    source: str = "developer"


@app.post("/feedback")
async def submit_feedback(body: FeedbackRequest):
    log.info(f"Feedback received: {body.rating} for {body.response_id}")
    return {"status": "received"}


# ── INTEGRATIONS ──────────────────────────────────────────────────────────────

@app.get("/integrations/discord")
async def get_discord_integrations():
    return []
