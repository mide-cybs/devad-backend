"""
DEVAD — Multi-tenant FastAPI Backend
Supports multiple customers, each with their own org, Discord server and data.
"""

import logging
import os
import uuid
from datetime import datetime
from typing import Optional, List

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

try:
    from pydantic import BaseModel
except ImportError:
    from pydantic.v1 import BaseModel

import anthropic
import httpx

app = FastAPI(title="DEVAD API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_claude():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=key)

def supabase_headers():
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def supabase_url(path: str) -> str:
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    return f"{base}/rest/v1/{path}"

async def db_get(table: str, filters: str = "") -> list:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                supabase_url(f"{table}?{filters}"),
                headers=supabase_headers(),
                timeout=10,
            )
            if r.status_code == 200:
                return r.json()
            log.error(f"DB GET {table} error: {r.status_code} {r.text}")
            return []
    except Exception as e:
        log.error(f"DB GET error: {e}")
        return []

async def db_post(table: str, data: dict) -> Optional[dict]:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                supabase_url(table),
                headers=supabase_headers(),
                json=data,
                timeout=10,
            )
            if r.status_code in (200, 201):
                result = r.json()
                return result[0] if isinstance(result, list) else result
            log.error(f"DB POST {table} error: {r.status_code} {r.text}")
            return None
    except Exception as e:
        log.error(f"DB POST error: {e}")
        return None

async def db_patch(table: str, filters: str, data: dict) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.patch(
                supabase_url(f"{table}?{filters}"),
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                json=data,
                timeout=10,
            )
            return r.status_code in (200, 204)
    except Exception as e:
        log.error(f"DB PATCH error: {e}")
        return False


# ── HEALTH ─────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "message": "DEVAD v2 is running", "version": "2.0.0"}

@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}


@app.get("/debug")
async def debug():
    """Check env vars and Supabase connectivity."""
    supabase_url = os.environ.get("SUPABASE_URL", "")
    service_key  = os.environ.get("SUPABASE_SERVICE_KEY", "")
    anon_key     = os.environ.get("SUPABASE_ANON_KEY", "")
    active_key   = service_key or anon_key
    result = {
        "SUPABASE_URL_set": bool(supabase_url),
        "SUPABASE_SERVICE_KEY_set": bool(service_key),
        "SUPABASE_ANON_KEY_set": bool(anon_key),
        "ANTHROPIC_API_KEY_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "DEFAULT_ORG_ID": os.environ.get("DEFAULT_ORG_ID", "NOT SET"),
        "DISCORD_SERVER_ID": os.environ.get("DISCORD_SERVER_ID", "NOT SET"),
    }
    if not supabase_url or not active_key:
        result["supabase_test"] = "SKIPPED - missing URL or key"
        return result
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{supabase_url.rstrip(chr(47))}/rest/v1/questions?limit=3",
                headers={"apikey": active_key, "Authorization": f"Bearer {active_key}"},
                timeout=8,
            )
            result["questions_table_status"] = r.status_code
            result["questions_sample"] = r.text[:400]
    except Exception as e:
        result["questions_error"] = str(e)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{supabase_url.rstrip(chr(47))}/rest/v1/organizations?limit=3",
                headers={"apikey": active_key, "Authorization": f"Bearer {active_key}"},
                timeout=8,
            )
            result["orgs_table_status"] = r.status_code
            result["orgs_sample"] = r.text[:400]
    except Exception as e:
        result["orgs_error"] = str(e)
    return result


# ── ORGS — multi-tenant core ───────────────────────────────────────────────────

class CreateOrgRequest(BaseModel):
    name: str
    slug: Optional[str] = None
    owner_email: Optional[str] = None
    agent_name: Optional[str] = "DevBot"
    tone: Optional[str] = "friendly"
    confidence_threshold: Optional[int] = 80

@app.post("/orgs/create")
async def create_org(body: CreateOrgRequest):
    org_id = str(uuid.uuid4())
    slug = body.slug or body.name.lower().replace(" ", "-").replace("_", "-")
    
    org_data = {
        "id": org_id,
        "name": body.name,
        "slug": slug,
        "owner_email": body.owner_email or "",
        "agent_name": body.agent_name,
        "tone": body.tone,
        "confidence_threshold": body.confidence_threshold,
        "created_at": datetime.utcnow().isoformat(),
    }
    
    result = await db_post("organizations", org_data)
    
    if result:
        log.info(f"Created org: {org_id} ({body.name})")
        return {"org_id": org_id, "name": body.name, "slug": slug, "agent_name": body.agent_name}
    else:
        # Return org_id anyway so onboarding can continue even if DB is unavailable
        log.warning(f"DB save failed for org {org_id}, returning anyway")
        return {"org_id": org_id, "name": body.name, "slug": slug, "agent_name": body.agent_name}

@app.get("/orgs/{org_id}")
async def get_org(org_id: str):
    rows = await db_get("organizations", f"id=eq.{org_id}")
    if not rows:
        raise HTTPException(status_code=404, detail="Org not found")
    return rows[0]


# ── DISCORD INTEGRATION ────────────────────────────────────────────────────────

class ConnectDiscordRequest(BaseModel):
    org_id: str
    server_id: str
    server_name: Optional[str] = ""
    channel: Optional[str] = "#help"

@app.post("/integrations/discord/connect")
async def connect_discord(body: ConnectDiscordRequest):
    data = {
        "id": str(uuid.uuid4()),
        "org_id": body.org_id,
        "server_id": body.server_id,
        "server_name": body.server_name,
        "watch_channel": body.channel,
        "created_at": datetime.utcnow().isoformat(),
    }
    await db_post("discord_integrations", data)
    log.info(f"Connected Discord server {body.server_id} to org {body.org_id}")
    return {"status": "connected", "server_id": body.server_id, "org_id": body.org_id}

@app.get("/integrations/discord")
async def get_all_discord_integrations():
    """Used by the Discord bot to load all server→org mappings."""
    return await db_get("discord_integrations", "select=server_id,org_id,watch_channel")

@app.get("/integrations/discord/{org_id}")
async def get_discord_integration(org_id: str):
    return await db_get("discord_integrations", f"org_id=eq.{org_id}")


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
    log.info(f"Question from {body.author_username} (org={body.org_id}): {body.content[:80]}")

    # Get org config for custom agent name/tone
    org_rows = await db_get("organizations", f"id=eq.{body.org_id}")
    org = org_rows[0] if org_rows else {}
    agent_name = org.get("agent_name", "DevBot")
    tone = org.get("tone", "friendly")

    try:
        claude = get_claude()
        message = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=f"""You are {agent_name}, a {tone} and helpful AI Developer Advocate.
Answer developer questions clearly and accurately.
Keep answers concise but complete. Use code examples when helpful.
If you don't know something specific, say so honestly.""",
            messages=[{"role": "user", "content": body.content}]
        )
        answer = message.content[0].text
        confidence = 85.0
        question_id = str(uuid.uuid4())

        # Save question + answer to database
        await db_post("questions", {
            "id": question_id,
            "org_id": body.org_id,
            "platform": body.platform,
            "channel": body.channel or "",
            "author_username": body.author_username,
            "author_external_id": body.author_external_id,
            "content": body.content,
            "status": "answered",
            "created_at": datetime.utcnow().isoformat(),
        })

        # Save answer
        response_id = str(uuid.uuid4())
        await db_post("agent_responses", {
            "id": response_id,
            "question_id": question_id,
            "org_id": body.org_id,
            "answer": answer,
            "confidence_score": confidence,
            "action_taken": "auto_posted",
            "created_at": datetime.utcnow().isoformat(),
        })

        log.info(f"Answer saved for org {body.org_id}, question_id={question_id}")

        return {
            "action": "auto_posted",
            "answer": answer,
            "confidence": confidence,
            "question_id": question_id,
            "response_id": response_id,
            "sources": [],
        }

    except Exception as e:
        log.error(f"Error: {e}")
        return {"action": "escalated", "answer": None, "confidence": 0, "error": str(e)}


# ── QUESTIONS LIST ─────────────────────────────────────────────────────────────

@app.get("/questions/{org_id}")
async def list_questions(org_id: str, limit: int = 50):
    rows = await db_get(
        "questions",
        f"org_id=eq.{org_id}&order=created_at.desc&limit={limit}"
    )
    return rows

@app.get("/questions/{org_id}/{question_id}/response")
async def get_question_response(org_id: str, question_id: str):
    rows = await db_get(
        "agent_responses",
        f"question_id=eq.{question_id}&org_id=eq.{org_id}&limit=1"
    )
    if not rows:
        return {"answer": None, "confidence_score": None, "action_taken": None}
    return rows[0]


# ── KNOWLEDGE BASE ─────────────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    org_id: str
    label: str
    url: str
    max_pages: int = 50

@app.post("/knowledge/ingest/docs", status_code=202)
async def ingest_docs(body: IngestRequest):
    source_id = str(uuid.uuid4())
    await db_post("knowledge_sources", {
        "id": source_id,
        "org_id": body.org_id,
        "label": body.label,
        "url": body.url,
        "status": "crawling",
        "chunks": 0,
        "created_at": datetime.utcnow().isoformat(),
    })
    return {"source_id": source_id, "status": "crawling", "message": "RAG pipeline coming in v3."}

@app.get("/knowledge/sources/{org_id}")
async def list_sources(org_id: str):
    return await db_get("knowledge_sources", f"org_id=eq.{org_id}")


# ── ANALYTICS ─────────────────────────────────────────────────────────────────

@app.get("/analytics/{org_id}/metrics")
async def get_metrics(org_id: str):
    rows = await db_get("questions", f"org_id=eq.{org_id}&select=id,status")
    total = len(rows)
    answered = len([r for r in rows if r.get("status") == "answered"])
    escalated = len([r for r in rows if r.get("status") == "escalated"])
    return {
        "questions_answered": answered,
        "total_questions": total,
        "avg_confidence": 85,
        "escalation_rate": round(escalated / total, 2) if total > 0 else 0,
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
    return {"status": "received"}
