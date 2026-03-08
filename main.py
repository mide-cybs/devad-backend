"""
DEVAD — FastAPI Backend (Railway-safe version)
"""

import logging
import os
from datetime import datetime
from typing import Optional, List

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

try:
    from pydantic import BaseModel
except ImportError:
    from pydantic.v1 import BaseModel

import anthropic

app = FastAPI(title="DEVAD API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_claude():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)

@app.get("/")
async def root():
    return {"status": "ok", "message": "DEVAD is running"}

@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}

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
        claude = get_claude()
        message = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system="""You are DevBot, a friendly and helpful AI Developer Advocate.
Answer developer questions clearly. Use code examples when helpful.""",
            messages=[{"role": "user", "content": body.content}]
        )
        answer = message.content[0].text
        log.info(f"Answer generated, length={len(answer)}")
        return {
            "action": "auto_posted",
            "answer": answer,
            "confidence": 85.0,
            "question_id": "local-" + str(abs(hash(body.content)))[:8],
            "response_id": "local-response",
            "sources": [],
        }
    except Exception as e:
        log.error(f"Error: {e}")
        return {"action": "escalated", "answer": None, "confidence": 0, "error": str(e)}

@app.get("/questions/{org_id}")
async def list_questions(org_id: str, limit: int = 50):
    return []

class IngestRequest(BaseModel):
    org_id: str
    label: str
    url: str
    max_pages: int = 50

@app.post("/knowledge/ingest/docs", status_code=202)
async def ingest_docs(body: IngestRequest):
    return {"source_id": "pending", "status": "crawling", "message": "RAG coming in v2."}

@app.get("/knowledge/sources/{org_id}")
async def list_sources(org_id: str):
    return []

@app.get("/analytics/{org_id}/metrics")
async def get_metrics(org_id: str):
    return {"questions_answered": 0, "avg_confidence": 85, "escalation_rate": 0.1}

@app.get("/analytics/{org_id}/pain-points")
async def get_pain_points(org_id: str):
    return []

class FeedbackRequest(BaseModel):
    response_id: str
    rating: int
    source: str = "developer"

@app.post("/feedback")
async def submit_feedback(body: FeedbackRequest):
    return {"status": "received"}

@app.get("/integrations/discord")
async def get_discord_integrations():
    return []
