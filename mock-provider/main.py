"""Mock LLM provider for load testing and benchmarks.

A tiny FastAPI server with configurable delay and failure rate.
NEVER benchmark against real free-tier APIs — always use this.
"""

import asyncio
import json
import os
import random
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Routewise Mock Provider")

# Configurable via environment variables
DELAY_MS: int = int(os.getenv("DELAY_MS", "200"))
FAILURE_RATE: float = float(os.getenv("FAILURE_RATE", "0.0"))
STREAM_CHUNKS: int = int(os.getenv("STREAM_CHUNKS", "10"))


@app.post("/v1/chat/completions")
async def mock_chat(request: Request):
    body = await request.json()
    stream = body.get("stream", False)

    # Simulate failures
    if FAILURE_RATE > 0.0 and random.random() < FAILURE_RATE:
        return JSONResponse(
            content={
                "error": {
                    "message": "Simulated provider failure",
                    "type": "server_error",
                }
            },
            status_code=500,
        )

    # Simulate latency
    if DELAY_MS > 0:
        await asyncio.sleep(DELAY_MS / 1000.0)

    if stream:
        return StreamingResponse(
            _stream_response(body.get("model", "mock-model")),
            media_type="text/event-stream",
        )

    return {
        "id": f"chatcmpl-mock-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "mock-model"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "This is a mock response for load testing.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 8,
            "total_tokens": 18,
        },
    }


async def _stream_response(model: str):
    """Yield SSE chunks in valid JSON format."""
    words = [
        "This",
        "is",
        "a",
        "mock",
        "streamed",
        "response",
        "for",
        "load",
        "testing",
        "purposes.",
    ]
    req_id = f"chatcmpl-mock-{uuid.uuid4().hex[:8]}"
    for i, word in enumerate(words):
        chunk = {
            "id": req_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": word + " "},
                    "finish_reason": None if i < len(words) - 1 else "stop",
                }
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        await asyncio.sleep(0.05)
    yield "data: [DONE]\n\n"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
