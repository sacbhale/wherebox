"""Drive the Anthropic adapter over a mocked HTTP transport (no network)."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2  # the HTTP client anthropic 1.x is built on

from app.ai.anthropic_adapter import AnthropicProvider

FIXTURES = Path(__file__).parent.parent / "fixtures"
RECORDED = FIXTURES / "anthropic"


def recorded(name: str) -> dict[str, Any]:
    return json.loads((RECORDED / f"{name}.json").read_text())


@dataclass
class MockAnthropic:
    provider: AnthropicProvider
    requests: list[httpx2.Request] = field(default_factory=list)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


Reply = Callable[[httpx2.Request], httpx2.Response]


def reply_json(status: int, name: str) -> Reply:
    """Answer with a recorded JSON body and status."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=recorded(name), headers={"request-id": "req_test"})

    return handler


def reply_timeout(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("timed out", request=request)


def reply_connect_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused", request=request)


def build_mock_anthropic(reply: Reply, model: str = "claude-sonnet-5-5") -> MockAnthropic:
    """An AnthropicProvider whose HTTP calls are answered by ``reply`` and recorded."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        request.read()
        seen.append(request)
        return reply(request)

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    provider = AnthropicProvider("sk-test", model, http_client=client)
    return MockAnthropic(provider=provider, requests=seen)
