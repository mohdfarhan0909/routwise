"""College Assistant RAG Client powered by Routewise Gateway."""

import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Greenfield College Assistant (RAG Client)")

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000/v1/chat/completions")
API_KEY = os.getenv("GATEWAY_API_KEY", "rw_dev_key")
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "college_faq.txt"


def load_knowledge_base() -> list[str]:
    """Load FAQ snippets from text data."""
    if not DATA_FILE.exists():
        return ["Greenfield University campus services and academic policies."]
    text = DATA_FILE.read_text(encoding="utf-8")
    # Split into sections by questions
    snippets = [s.strip() for s in text.split("##") if s.strip()]
    return snippets


SNIPPETS = load_knowledge_base()


def retrieve_context(query: str, top_k: int = 2) -> str:
    """Simple keyword matching retriever for knowledge base."""
    terms = set(query.lower().split())
    scored = []
    for snippet in SNIPPETS:
        score = sum(1 for term in terms if term in snippet.lower())
        scored.append((score, snippet))
    scored.sort(key=lambda x: x[0], reverse=True)
    return "\n\n".join(s[1] for s in scored[:top_k])


class AskRequest(BaseModel):
    question: str
    stream: bool = False


@app.post("/ask")
async def ask_question(body: AskRequest) -> Any:
    """Ask a question; retrieves college documents and forwards to Routewise gateway."""
    context = retrieve_context(body.question)
    system_prompt = (
        "You are an AI advisor for Greenfield University. Answer the student's question "
        f"accurately using ONLY the following context:\n\n{context}"
    )

    payload = {
        "model": "gpt-4o-mini",
        "temperature": 0.1,  # Low temperature to leverage gateway semantic cache
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": body.question},
        ],
        "stream": body.stream,
    }

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    client = httpx.AsyncClient(timeout=30.0)

    if body.stream:
        try:
            req = client.build_request(
                "POST", GATEWAY_URL, json=payload, headers=headers
            )
            resp = await client.send(req, stream=True)
            if resp.status_code >= 400:
                body_bytes = await resp.aread()
                await resp.aclose()
                await client.aclose()
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Gateway responded with error: {body_bytes.decode('utf-8', errors='ignore')}",
                )

            async def _stream_generator():
                try:
                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                _stream_generator(), media_type="text/event-stream"
            )
        except httpx.RequestError as e:
            await client.aclose()
            raise HTTPException(
                status_code=503,
                detail=f"Unable to reach Routewise Gateway at {GATEWAY_URL}: {e}",
            ) from e

    async with client:
        try:
            resp = await client.post(GATEWAY_URL, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Gateway responded with error: {resp.text}",
                )
            return resp.json()
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=503,
                detail=f"Unable to reach Routewise Gateway at {GATEWAY_URL}: {e}",
            ) from e


@app.get("/health")
async def health():
    return {"status": "ok", "app": "college-assistant-rag"}
