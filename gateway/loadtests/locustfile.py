"""Locust load test script for Routewise LLM Gateway."""

import random

from locust import HttpUser, between, task

SAMPLE_PROMPTS = [
    "Explain the concept of rate limiting in distributed systems.",
    "Write a quicksort implementation in Python.",
    "What is the difference between Postgres and Redis?",
    "How does a token bucket rate limiter work?",
    "Explain semantic caching using vector embeddings.",
]


class GatewayUser(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self):
        # Default header with API key
        self.client.headers = {
            "Authorization": "Bearer rw_loadtest_key",
            "Content-Type": "application/json",
        }

    @task(3)
    def chat_completion(self):
        prompt = random.choice(SAMPLE_PROMPTS)
        payload = {
            "model": "mock-gpt",
            "messages": [
                {"role": "system", "content": "You are a helpful coding assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,  # Test semantic cache hit rate
        }
        self.client.post(
            "/v1/chat/completions", json=payload, name="/v1/chat/completions [cacheable]"
        )

    @task(1)
    def chat_completion_streaming(self):
        payload = {
            "model": "mock-gpt",
            "messages": [
                {"role": "user", "content": "Stream me a quick summary."},
            ],
            "stream": True,
        }
        with self.client.post(
            "/v1/chat/completions", json=payload, stream=True, name="/v1/chat/completions [stream]"
        ) as response:
            for _ in response.iter_lines():
                pass

    @task(1)
    def health_check(self):
        self.client.get("/health", name="/health")
