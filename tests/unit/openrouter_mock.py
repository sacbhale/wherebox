"""Drive the OpenRouter adapter over httpx.MockTransport (no network)."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.ai.openrouter_adapter import OpenRouterProvider
from app.ai.openrouter_catalog import ModelInfo, Strategy

FIXTURES = Path(__file__).parent.parent / "fixtures" / "openrouter"
API_KEY = "sk-or-v1-test-secret-0123456789"
MODEL = "google/gemini-3.1-flash-lite"


def recorded(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"chat_{name}.json").read_text())


def model_info(
    strategy: Strategy = "tools",
    accepts_images: bool = True,
    model: str = MODEL,
    **capabilities: bool,
) -> ModelInfo:
    return ModelInfo(
        id=model,
        name=model,
        context_length=1_000_000,
        prompt_usd_per_mtok=0.25,
        completion_usd_per_mtok=1.5,
        accepts_images=accepts_images,
        strategy=strategy,
        **capabilities,
    )


Reply = Callable[[httpx.Request], httpx.Response]


def reply_json(status: int, name: str) -> Reply:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=recorded(name))

    return handler


def reply_body(status: int, body: Any) -> Reply:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


def reply_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def reply_connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@dataclass
class MockOpenRouter:
    provider: OpenRouterProvider
    requests: list[httpx.Request] = field(default_factory=list)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


def build_mock_openrouter(
    reply: Reply, info: ModelInfo | None = None, model: str = MODEL
) -> MockOpenRouter:
    """An OpenRouterProvider whose HTTP calls are answered by ``reply`` and recorded;
    ``info`` stands in for the catalog entry (None: capabilities unknown)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return reply(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenRouterProvider(API_KEY, model, model_info=lambda: info, http_client=client)
    return MockOpenRouter(provider=provider, requests=seen)
