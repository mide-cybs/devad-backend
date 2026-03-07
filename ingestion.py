"""
DevRel AI — Ingestion & RAG Pipeline
Handles: crawling docs, chunking, embedding, vector upsert, retrieval, generation
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import textwrap
from typing import Any
from uuid import UUID

import anthropic
import httpx
from openai import AsyncOpenAI

log = logging.getLogger(__name__)

# ── clients ───────────────────────────────────────────────────────────────────
openai_client  = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
claude_client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
EMBED_MODEL    = "text-embedding-3-small"
EMBED_DIM      = 1536
CLAUDE_MODEL   = "claude-sonnet-4-20250514"

# ── Qdrant (vector store) ─────────────────────────────────────────────────────
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    Filter, FieldCondition, MatchValue, SearchRequest,
)

qdrant = AsyncQdrantClient(
    url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
    api_key=os.environ.get("QDRANT_API_KEY"),
)

COLLECTION_NAME = "devrel_knowledge"


async def ensure_collection():
    """Create the Qdrant collection if it doesn't exist."""
    collections = await qdrant.get_collections()
    names = [c.name for c in collections.collections]
    if COLLECTION_NAME not in names:
        await qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
        log.info(f"Created Qdrant collection: {COLLECTION_NAME}")


# ═══════════════════════════════════════════════════════════════════════════════
# INGESTION: Crawl → Chunk → Embed → Upsert
# ═══════════════════════════════════════════════════════════════════════════════

async def ingest_url(
    source_id: UUID,
    org_id: UUID,
    url: str,
    max_pages: int = 200,
    db=None,
):
    """
    Full ingestion pipeline for a docs URL.
    1. Crawl all pages starting from url
    2. Chunk each page into ~500-token segments
    3. Embed each chunk with text-embedding-3-small
    4. Upsert into Qdrant with org_id + source_id metadata
    """
    await ensure_collection()
    if db:
        await db.update_knowledge_source_status(source_id, "crawling")

    # Step 1: Crawl
    pages = await crawl_docs(url, max_pages=max_pages)
    log.info(f"Crawled {len(pages)} pages from {url}")

    if db:
        await db.update_knowledge_source_status(source_id, "indexing")
        await db.update_knowledge_source_page_count(source_id, len(pages))

    # Step 2 & 3: Chunk and embed (batched)
    all_chunks = []
    for page in pages:
        chunks = chunk_text(page["content"], url=page["url"], title=page["title"])
        all_chunks.extend(chunks)

    log.info(f"Generated {len(all_chunks)} chunks from {len(pages)} pages")

    # Embed in batches of 100
    chunk_batches = [all_chunks[i:i+100] for i in range(0, len(all_chunks), 100)]
    points = []
    for batch in chunk_batches:
        texts = [c["text"] for c in batch]
        embeddings = await embed_texts(texts)
        for chunk, embedding in zip(batch, embeddings):
            points.append(PointStruct(
                id=str(chunk["id"]),
                vector=embedding,
                payload={
                    "org_id":    str(org_id),
                    "source_id": str(source_id),
                    "text":      chunk["text"],
                    "url":       chunk["url"],
                    "title":     chunk["title"],
                    "chunk_idx": chunk["chunk_idx"],
                },
            ))

    # Step 4: Upsert to Qdrant
    # Delete existing chunks for this source first (re-index case)
    await qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(
            must=[FieldCondition(key="source_id", match=MatchValue(value=str(source_id)))]
        ),
    )

    await qdrant.upsert(collection_name=COLLECTION_NAME, points=points)

    if db:
        await db.update_knowledge_source_status(source_id, "ready")
        await db.update_knowledge_source_chunk_count(source_id, len(points))

    log.info(f"Ingested {len(points)} chunks for source {source_id}")
    return {"pages": len(pages), "chunks": len(points)}


async def ingest_github_repo(
    source_id: UUID,
    org_id: UUID,
    repo: str,
    include_issues: bool = True,
    db=None,
):
    """
    Ingest GitHub issues and discussions as Q&A knowledge.
    Uses GitHub REST API with pagination.
    """
    await ensure_collection()
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    docs = []

    if include_issues:
        async with httpx.AsyncClient() as client:
            page = 1
            while True:
                r = await client.get(
                    f"https://api.github.com/repos/{repo}/issues",
                    headers=headers,
                    params={"state": "all", "per_page": 100, "page": page},
                )
                issues = r.json()
                if not issues:
                    break
                for issue in issues:
                    if not isinstance(issue, dict):
                        continue
                    text = f"TITLE: {issue.get('title', '')}\n\nBODY: {issue.get('body', '') or ''}"
                    docs.append({"text": text, "url": issue.get("html_url", ""), "title": issue.get("title", "")})
                page += 1
                if len(docs) >= 500:
                    break

    all_chunks = []
    for doc in docs:
        chunks = chunk_text(doc["text"], url=doc["url"], title=doc["title"], max_tokens=400)
        all_chunks.extend(chunks)

    embeddings = await embed_texts([c["text"] for c in all_chunks])
    points = [
        PointStruct(
            id=str(c["id"]),
            vector=emb,
            payload={"org_id": str(org_id), "source_id": str(source_id),
                     "text": c["text"], "url": c["url"], "title": c["title"]},
        )
        for c, emb in zip(all_chunks, embeddings)
    ]

    await qdrant.upsert(collection_name=COLLECTION_NAME, points=points)
    if db:
        await db.update_knowledge_source_status(source_id, "ready")
        await db.update_knowledge_source_chunk_count(source_id, len(points))

    log.info(f"Ingested {len(points)} GitHub chunks for {repo}")
    return {"issues": len(docs), "chunks": len(points)}


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS: crawl, chunk, embed
# ═══════════════════════════════════════════════════════════════════════════════

async def crawl_docs(base_url: str, max_pages: int = 200) -> list[dict]:
    """
    Crawl a docs site starting from base_url, following same-origin links.
    Returns list of {url, title, content} dicts.
    """
    try:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
    except ImportError:
        log.warning("crawl4ai not installed; using single-page fallback")
        return await crawl_single_page(base_url)

    visited: set[str] = set()
    queue  = [base_url]
    pages  = []

    async with AsyncWebCrawler() as crawler:
        while queue and len(pages) < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            try:
                result = await crawler.arun(url=url)
                if not result.success:
                    continue

                pages.append({
                    "url":     url,
                    "title":   result.metadata.get("title", ""),
                    "content": result.markdown or result.cleaned_html or "",
                })

                # Enqueue same-origin links
                base_domain = "/".join(base_url.split("/")[:3])
                for link in (result.links.get("internal") or []):
                    href = link.get("href", "")
                    if href.startswith(base_domain) and href not in visited:
                        queue.append(href)

            except Exception as e:
                log.warning(f"Failed to crawl {url}: {e}")
                continue

    return pages


async def crawl_single_page(url: str) -> list[dict]:
    """Fallback: fetch a single page with httpx."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        r = await client.get(url, headers={"User-Agent": "DevRelAI/1.0 Docs Indexer"})
        text = r.text
        # Strip HTML tags roughly
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return [{"url": url, "title": url, "content": text}]


def chunk_text(
    text: str,
    url: str = "",
    title: str = "",
    max_tokens: int = 500,
    overlap_tokens: int = 50,
) -> list[dict]:
    """
    Split text into overlapping chunks of ~max_tokens words.
    Each chunk gets a stable UUID based on its content.
    """
    import hashlib

    words = text.split()
    chunks = []
    step   = max_tokens - overlap_tokens

    for i in range(0, max(len(words), 1), step):
        chunk_words = words[i : i + max_tokens]
        if not chunk_words:
            break
        chunk_text_str = " ".join(chunk_words)
        chunk_id = str(UUID(bytes=hashlib.md5(f"{url}:{i}:{chunk_text_str[:50]}".encode()).digest()))
        chunks.append({
            "id":        chunk_id,
            "text":      chunk_text_str,
            "url":       url,
            "title":     title,
            "chunk_idx": i // step,
        })
        if i + max_tokens >= len(words):
            break

    return chunks


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch embed texts using OpenAI text-embedding-3-small."""
    if not texts:
        return []

    # OpenAI supports up to 2048 texts per batch
    batches = [texts[i:i+2048] for i in range(0, len(texts), 2048)]
    all_embeddings: list[list[float]] = []

    for batch in batches:
        response = await openai_client.embeddings.create(
            model=EMBED_MODEL,
            input=batch,
        )
        all_embeddings.extend([item.embedding for item in response.data])

    return all_embeddings


# ═══════════════════════════════════════════════════════════════════════════════
# RAG PIPELINE: Retrieve → Rerank → Generate
# ═══════════════════════════════════════════════════════════════════════════════

async def answer_question(
    question: str,
    org_id: str,
    tone: str = "friendly",
    agent_name: str = "DevBot",
    context_messages: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Full RAG pipeline:
    1. Embed the question
    2. Retrieve top-k chunks from Qdrant (filtered by org_id)
    3. Generate a response with Claude
    4. Return response + confidence + source IDs

    Returns:
        {
            "answer": str,
            "confidence": float,   # 0–100
            "source_ids": list[str],
            "chunks": list[dict],
        }
    """
    await ensure_collection()

    # 1. Embed query
    query_embedding = (await embed_texts([question]))[0]

    # 2. Retrieve from Qdrant (org-scoped)
    results = await qdrant.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_embedding,
        limit=8,
        query_filter=Filter(
            must=[FieldCondition(key="org_id", match=MatchValue(value=org_id))]
        ),
        with_payload=True,
    )

    if not results:
        return {
            "answer": "I don't have enough information to answer this question yet. A human DevRel will follow up.",
            "confidence": 0.0,
            "source_ids": [],
            "chunks": [],
        }

    # 3. Build context from retrieved chunks
    chunks = [
        {
            "text":      r.payload["text"],
            "url":       r.payload.get("url", ""),
            "title":     r.payload.get("title", ""),
            "score":     r.score,
            "source_id": r.payload.get("source_id", ""),
        }
        for r in results
    ]

    context_text = "\n\n---\n\n".join(
        f"[Source: {c['title'] or c['url']}]\n{c['text']}"
        for c in chunks[:5]
    )

    tone_instructions = {
        "friendly":     "Warm, helpful, and encouraging. Like a helpful senior teammate.",
        "professional": "Precise, formal, and concise. No filler words.",
        "technical":    "Deep and code-first. Assume high technical competence. Include all relevant details.",
    }.get(tone, "friendly")

    # Build conversation history
    messages: list[dict] = []
    for msg in (context_messages or []):
        messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
    messages.append({"role": "user", "content": question})

    system = textwrap.dedent(f"""
        You are {agent_name}, an expert AI Developer Advocate.
        Tone: {tone_instructions}

        Use ONLY the following documentation excerpts to answer the question.
        If the answer isn't in the excerpts, say so and offer to escalate.

        Always:
        - Include runnable code examples in markdown code blocks
        - Mention the specific doc page or section the answer comes from
        - Flag common gotchas or related tips at the end

        DOCUMENTATION CONTEXT:
        {context_text}
    """).strip()

    # 4. Generate with Claude
    response = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=800,
        system=system,
        messages=messages,
    )
    answer = response.content[0].text

    # 5. Compute confidence from retrieval scores
    # Average of top-3 scores, normalised to 0–100
    top_scores = [r.score for r in results[:3]]
    raw_confidence = sum(top_scores) / len(top_scores)
    # Cosine similarity scores are -1..1; map to 0..100
    confidence = round(max(0.0, min(100.0, raw_confidence * 100)), 1)

    unique_source_ids = list({c["source_id"] for c in chunks if c["source_id"]})

    return {
        "answer":     answer,
        "confidence": confidence,
        "source_ids": unique_source_ids,
        "chunks":     chunks,
    }


async def classify_intent(text: str) -> str:
    """
    Classify a message as one of: question, bug_report, feature_request, feedback, off_topic.
    Uses a quick Claude call with a strict prompt.
    """
    response = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=10,
        system="Classify the following developer message into exactly one of these categories: question, bug_report, feature_request, feedback, off_topic. Reply with only the category word, nothing else.",
        messages=[{"role": "user", "content": text[:500]}],
    )
    raw = response.content[0].text.strip().lower()
    valid = {"question", "bug_report", "feature_request", "feedback", "off_topic"}
    return raw if raw in valid else "question"


def get_ingestion_status(source_id: str) -> dict:
    """Placeholder — real status comes from the DB."""
    return {"source_id": source_id, "status": "unknown"}
