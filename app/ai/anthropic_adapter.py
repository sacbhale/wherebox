"""Anthropic Messages API adapter. The only module in the app that imports ``anthropic``."""

import base64
import json
from typing import Any

import anthropic
import httpx2
from pydantic import ValidationError

from app.ai import prompts
from app.ai.provider import (
    ProviderInvalidOutput,
    ProviderPermanentError,
    ProviderRateLimited,
    ProviderTimeoutError,
    ProviderTransientError,
    bounded,
    parse_retry_after,
)
from app.ai.schemas import (
    TOOL_DESCRIPTION,
    TOOL_INPUT_SCHEMA,
    TOOL_NAME,
    ExtractRequest,
    ExtractResult,
    ToolOutput,
    Usage,
    output_cap,
)

# Receipts and text; the photo modes get schemas.output_cap (16,384).
DEFAULT_MAX_TOKENS = 4096
# 408 request timeout, 409 conflict and 429 rate limit are worth retrying; so is any 5xx
# (500, 502, 503, 504, 529 overloaded).
_TRANSIENT_4XX = frozenset({408, 409, 429})


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        # max_retries=0: the capture worker owns retries (re-queue with not_before), so one
        # extract() is exactly one billed call and one ledger row.
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=0, http_client=http_client)

    def max_tokens_for(self, req: ExtractRequest) -> int:
        """The output cap of this request (larger for the photo modes)."""
        return output_cap(req.mode, self.max_tokens)

    def extract(self, req: ExtractRequest) -> ExtractResult:
        content: list[dict[str, Any]] = []
        for index, image in enumerate(req.images):
            content.append({"type": "text", "text": prompts.image_label(index)})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(image).decode("ascii"),
                    },
                }
            )
        content.append({"type": "text", "text": prompts.user_text(req)})
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens_for(req),
                system=prompts.system_prompt(req.mode),
                messages=[{"role": "user", "content": content}],  # type: ignore[list-item]
                tools=[
                    {
                        "name": TOOL_NAME,
                        "description": TOOL_DESCRIPTION,
                        "input_schema": TOOL_INPUT_SCHEMA,
                    }
                ],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                timeout=req.timeout_s,
            )
        except anthropic.APITimeoutError as exc:
            raise ProviderTimeoutError() from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderTransientError(f"connection error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            detail = bounded(f"HTTP {exc.status_code}: {exc.message}")
            retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
            if exc.status_code == 429:
                raise ProviderRateLimited(detail, retry_after=retry_after) from exc
            if exc.status_code in _TRANSIENT_4XX or exc.status_code >= 500:
                raise ProviderTransientError(detail, retry_after=retry_after) from exc
            raise ProviderPermanentError(detail) from exc
        except anthropic.AnthropicError as exc:
            raise ProviderPermanentError(f"{type(exc).__name__}: {exc}") from exc

        usage = Usage(
            input_tokens=message.usage.input_tokens or 0,
            output_tokens=message.usage.output_tokens or 0,
        )
        tool_input = next(
            (
                block.input
                for block in message.content
                if block.type == "tool_use" and block.name == TOOL_NAME
            ),
            None,
        )
        if tool_input is None:
            raw = json.dumps([block.model_dump(mode="json") for block in message.content])
            raise ProviderInvalidOutput(f"no {TOOL_NAME} tool call in response", usage, raw)
        raw = json.dumps(tool_input, default=str)
        if message.stop_reason == "max_tokens":
            raise ProviderInvalidOutput("output truncated at max_tokens", usage, raw)
        try:
            output = ToolOutput.model_validate(tool_input)
        except ValidationError as exc:
            errors = exc.error_count()
            raise ProviderInvalidOutput(
                f"tool output failed validation ({errors} errors)", usage, raw
            ) from exc
        return ExtractResult(items=output.items, receipt=output.receipt, usage=usage)
