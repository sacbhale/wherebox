"""OpenRouter chat-completions adapter (OpenAI-compatible HTTP API, plain httpx).

The output strategy follows the model's catalog capabilities, so any model can be used:

1. ``tools``: one forced function call whose parameters are ``TOOL_INPUT_SCHEMA``;
2. ``json_schema``: ``response_format`` with ``schemas.strict_json_schema()`` (strict);
3. ``prompt``: the prompt asks for the JSON object; the first object in the text is parsed.

Strategies 1-2 send ``provider.require_parameters`` so OpenRouter only routes to providers
that support them. Unknown capabilities (catalog unavailable) use strategy 1. The key is only
ever put in the Authorization header: never in messages, exceptions or logs.

The catalog can list a parameter that no provider actually serves with
``require_parameters`` (OpenRouter then answers 404 "No endpoints found that support the
provided '<param>'", or the generic "No endpoints found that can handle the requested
parameters.", unbilled). ``extract`` then retries at once, in the same call, without it: an
unsupported ``reasoning`` / ``parallel_tool_calls`` is dropped, anything else (or the generic
message) moves to the next strategy (tools -> json_schema -> prompt). The downgrade is
remembered per model id (``_downgrades``) as soon as a fallback request is accepted, merged
monotonically, for DOWNGRADE_TTL_S; after that the model is tried at its catalog strategy
again.

Provider error text is collapsed to one line, capped (``provider.bounded``) and redacted (the
key, and anything shaped like an OpenRouter key) before it reaches a message or a log.

The response is streamed so the whole call keeps to ``ExtractRequest.timeout_s`` (httpx
timeouts are per phase: a slow trickle would otherwise never time out) and its body is capped
at ``MAX_RESPONSE_BYTES``. A call that ends after the request was sent but without a usage
report is marked ``maybe_billed``: OpenRouter may still charge it.
"""

import base64
import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError

from app.ai import prompts
from app.ai.openrouter_catalog import ModelInfo, Strategy
from app.ai.provider import (
    ProviderError,
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
    strict_json_schema,
)
from app.config import APP_NAME

log = logging.getLogger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"
# Same caps as the Anthropic adapter: photo modes get schemas.output_cap (16,384).
DEFAULT_MAX_TOKENS = 4096
# 408 request timeout and 429 rate limit are worth retrying; so is any 5xx.
_TRANSIENT_4XX = frozenset({408, 429})
INSUFFICIENT_CREDITS = "OpenRouter: insufficient credits"
_MODEL_GONE_HINTS = ("not a valid model", "model not found", "no such model", "does not exist")
CONNECT_TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# first_json_object only looks this far into a text answer (it retries from every "{").
MAX_JSON_TEXT_CHARS = 100_000
TRUNCATED = "output truncated"
TRUNCATED_BY_REASONING = "output truncated (reasoning used the token cap)"
# Failures raised before the request left this machine: OpenRouter cannot bill those.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)
# 404 when no provider serves a parameter under require_parameters (not billed).
_NO_ENDPOINTS = re.compile(r"no endpoints found that support the provided '([^']*)'", re.I)
# The same without naming the parameter: treated like an unsupported strategy parameter.
_NO_ENDPOINTS_GENERIC = re.compile(r"no endpoints found that can handle the requested param", re.I)
# Anything shaped like an OpenRouter key is redacted from provider text.
_KEY_SHAPED = re.compile(r"sk-or-[A-Za-z0-9_-]{8,}")
STRATEGIES: tuple[Strategy, ...] = ("tools", "json_schema", "prompt")
# Unsupported optional parameters are dropped; any other unsupported one changes strategy.
_OPTIONAL_PARAMS = frozenset({"reasoning", "parallel_tool_calls"})
# A parameter name from OpenRouter's message is logged up to this length.
MAX_LOGGED_PARAM_CHARS = 64
NO_STRUCTURED_OUTPUT = "No provider for {model} supports structured output right now"

# A remembered downgrade is used for this long (seconds, monotonic clock).
DOWNGRADE_TTL_S = 3600.0
# model id -> (strategy, dropped optional parameters, monotonic time saved): a downgrade
# whose request OpenRouter accepted.
_downgrades: dict[str, tuple[Strategy, frozenset[str], float]] = {}
_downgrades_lock = threading.Lock()


def clear_downgrades() -> None:
    """Forget every remembered strategy downgrade (tests)."""
    with _downgrades_lock:
        _downgrades.clear()


def _remembered(model: str) -> tuple[Strategy, frozenset[str]] | None:
    """The model's remembered downgrade, unless it is older than DOWNGRADE_TTL_S."""
    with _downgrades_lock:
        entry = _downgrades.get(model)
        if entry is None:
            return None
        if time.monotonic() - entry[2] > DOWNGRADE_TTL_S:
            del _downgrades[model]
            return None
        return entry[0], entry[1]


def _remember(model: str, strategy: Strategy, dropped: frozenset[str]) -> None:
    """Save a downgrade, merged with a live one: the later strategy, the union of dropped
    parameters (so concurrent calls can never move a model back up)."""
    with _downgrades_lock:
        entry = _downgrades.get(model)
        if entry is not None and time.monotonic() - entry[2] <= DOWNGRADE_TTL_S:
            strategy = max(strategy, entry[0], key=STRATEGIES.index)
            dropped = dropped | entry[1]
        _downgrades[model] = (strategy, dropped, time.monotonic())


class _NoEndpoints(Exception):
    """OpenRouter has no provider for ``param`` with this request (try without it; "" when
    the message names none). ``message`` is OpenRouter's own (redacted, bounded)."""

    def __init__(self, param: str, message: str) -> None:
        super().__init__(message)
        self.param = param
        self.message = message


_client: httpx.Client | None = None
_client_lock = threading.Lock()


def shared_client() -> httpx.Client:
    """One connection pool for every OpenRouter provider (httpx.Client is thread-safe)."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client()
        return _client


def model_gone_message(model: str) -> str:
    return f"Model {model} is no longer available on OpenRouter — pick another in Settings"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _usage(body: Any) -> Usage | None:
    """``usage`` of a response body (tokens, and ``cost`` in credits = USD), if present."""
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None
    return Usage(
        input_tokens=int(_number(usage.get("prompt_tokens")) or 0),
        output_tokens=int(_number(usage.get("completion_tokens")) or 0),
        cost_usd=_number(usage.get("cost")),
    )


def _error_message(body: Any) -> str:
    """The body's raw error message (not yet redacted or bounded: _http_error redacts first,
    then bounds, so a key cut at the length limit can't escape redaction)."""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or "")
    if isinstance(error, str):
        return error
    return ""


def _text(content: Any) -> str:
    """Message content as text (a string, or a list of content parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def first_json_object(text: str) -> Any:
    """The first JSON object in ``text`` (prose and ```json fences around it are ignored).
    Raises ValueError when there is none. Only the first MAX_JSON_TEXT_CHARS are searched."""
    text = text[:MAX_JSON_TEXT_CHARS]
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            return value
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in the response text")


class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        model_info: Callable[[], ModelInfo | None] = lambda: None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        http_client: httpx.Client | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._api_key = api_key
        self._model_info = model_info
        self._client = http_client or shared_client()
        self._url = f"{base_url}/chat/completions"

    def max_tokens_for(self, req: ExtractRequest) -> int:
        """The output cap of this request (larger for the photo modes)."""
        return output_cap(req.mode, self.max_tokens)

    def _redact(self, message: str) -> str:
        if self._api_key:
            message = message.replace(self._api_key, "***")
        return _KEY_SHAPED.sub("***", message)

    def _body(
        self,
        req: ExtractRequest,
        strategy: Strategy,
        info: ModelInfo | None,
        dropped: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        system = prompts.system_prompt(req.mode)
        if strategy != "tools":
            system += "\n\n" + prompts.json_answer(include_schema=strategy == "prompt")
        content: list[dict[str, Any]] = [{"type": "text", "text": prompts.user_text(req)}]
        for index, image in enumerate(req.images):
            content.append({"type": "text", "text": prompts.image_label(index)})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
                    },
                }
            )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "max_tokens": self.max_tokens_for(req),
        }
        if info is not None and info.lower_reasoning and "reasoning" not in dropped:
            body["reasoning"] = {"effort": "low"}
        if strategy == "tools":
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "description": TOOL_DESCRIPTION,
                        "parameters": TOOL_INPUT_SCHEMA,
                    },
                }
            ]
            body["tool_choice"] = {"type": "function", "function": {"name": TOOL_NAME}}
            parallel = info is not None and info.parallel_tool_calls
            if parallel and "parallel_tool_calls" not in dropped:
                body["parallel_tool_calls"] = False
        elif strategy == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": TOOL_NAME, "strict": True, "schema": strict_json_schema()},
            }
        if strategy != "prompt":
            body["provider"] = {"require_parameters": True}
        return body

    def _status_error(
        self, status: int, message: str, usage: Usage | None, retry_after: float | None = None
    ) -> ProviderError:
        """Map an HTTP status (or an ``error.code`` in a 200 body) to a contract error."""
        detail = self._redact(f"OpenRouter HTTP {status}: {message or 'no message'}")
        if status == 402:
            return ProviderPermanentError(INSUFFICIENT_CREDITS, usage=usage)
        lowered = message.casefold()
        if (status == 404 and "no endpoints found" not in lowered) or (
            status in (400, 404) and any(hint in lowered for hint in _MODEL_GONE_HINTS)
        ):
            return ProviderPermanentError(
                model_gone_message(self.model), usage=usage, detail=detail
            )
        if status == 429:
            return ProviderRateLimited(detail, usage=usage, retry_after=retry_after)
        if status in _TRANSIENT_4XX or status >= 500:
            return ProviderTransientError(detail, usage=usage, retry_after=retry_after)
        return ProviderPermanentError(detail, usage=usage)

    def _http_error(
        self, status: int, message: str, usage: Usage | None, retry_after: float | None
    ) -> Exception:
        message = bounded(self._redact(message))
        if status == 404:
            named = _NO_ENDPOINTS.search(message)
            if named or _NO_ENDPOINTS_GENERIC.search(message):
                return _NoEndpoints(named.group(1) if named else "", message)
        return self._status_error(status, message, usage, retry_after)

    def extract(self, req: ExtractRequest) -> ExtractResult:
        info = self._model_info()
        if req.images and info is not None and not info.accepts_images:
            raise ProviderPermanentError(f"Model {self.model} does not accept images")
        strategy: Strategy = info.strategy if info is not None else "tools"
        dropped: frozenset[str] = frozenset()
        remembered = _remembered(self.model)
        if remembered is not None:
            strategy = max(strategy, remembered[0], key=STRATEGIES.index)
            dropped = remembered[1]
        start = (strategy, dropped)
        deadline = time.monotonic() + req.timeout_s
        timeout_s = req.timeout_s
        while True:
            try:
                result = self._call(req, strategy, info, dropped, deadline, timeout_s)
            except _NoEndpoints as exc:
                # The whole call (fallbacks included) keeps to req.timeout_s.
                timeout_s = max(deadline - time.monotonic(), 0.001)
                if exc.param in _OPTIONAL_PARAMS and exc.param not in dropped:
                    dropped = dropped | {exc.param}
                elif strategy != STRATEGIES[-1]:
                    strategy = STRATEGIES[STRATEGIES.index(strategy) + 1]
                else:
                    raise ProviderPermanentError(
                        NO_STRUCTURED_OUTPUT.format(model=self.model),
                        detail=f"OpenRouter HTTP 404: {exc.message}",
                    ) from exc
                log.info(
                    "OpenRouter: no endpoint supports %r for %s; retrying with strategy %s%s",
                    exc.param[:MAX_LOGGED_PARAM_CHARS],
                    self.model,
                    strategy,
                    f" without {', '.join(sorted(dropped))}" if dropped else "",
                )
                continue
            except ProviderError:
                # Any answer but "no endpoints" means OpenRouter accepted the fallback request.
                if (strategy, dropped) != start:
                    _remember(self.model, strategy, dropped)
                raise
            if (strategy, dropped) != start:
                _remember(self.model, strategy, dropped)
            return result

    def _call(
        self,
        req: ExtractRequest,
        strategy: Strategy,
        info: ModelInfo | None,
        dropped: frozenset[str],
        deadline: float,
        timeout_s: float,
    ) -> ExtractResult:
        """One HTTP request. Raises _NoEndpoints for a strategy/parameter 404."""
        timeout = httpx.Timeout(timeout_s, connect=min(CONNECT_TIMEOUT_S, timeout_s))
        try:
            with self._client.stream(
                "POST",
                self._url,
                json=self._body(req, strategy, info, dropped),
                headers={"Authorization": f"Bearer {self._api_key}", "X-Title": APP_NAME},
                timeout=timeout,
            ) as response:
                status = response.status_code
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                content = _read_body(response, deadline)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(maybe_billed=not isinstance(exc, _NOT_SENT)) from exc
        except httpx.TransportError as exc:
            raise ProviderTransientError(
                self._redact(f"connection error: {type(exc).__name__}"),
                maybe_billed=not isinstance(exc, _NOT_SENT),
            ) from exc

        try:
            body = json.loads(content)
        except ValueError:
            body = None
        usage = _usage(body)
        if status != 200:
            raise self._http_error(status, _error_message(body), usage, retry_after)
        if not isinstance(body, dict):
            # A 200 means the provider answered, so the call was probably billed.
            raise ProviderTransientError(
                "OpenRouter returned a response that is not JSON", maybe_billed=True
            )
        if body.get("error"):
            error = body["error"]
            code = error.get("code") if isinstance(error, dict) else None
            status = code if isinstance(code, int) and code >= 400 else 502
            raise self._http_error(status, _error_message(body), usage, retry_after)
        choices = body.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        if not isinstance(choice, dict):
            raw = json.dumps(body)
            raise ProviderInvalidOutput("no choices in response", usage or Usage(), raw)
        if choice.get("finish_reason") == "error":
            raise ProviderTransientError("OpenRouter: the model stopped with an error", usage=usage)
        return self._parse(choice, strategy, usage or Usage())

    def _parse(self, choice: dict[str, Any], strategy: Strategy, usage: Usage) -> ExtractResult:
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        text = _text(message.get("content"))
        # Every record_items call counts (a model may split its answer over several calls).
        calls: list[str] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and function.get("name") == TOOL_NAME:
                arguments = function.get("arguments")
                calls.append(arguments if isinstance(arguments, str) else json.dumps(arguments))
        raw = "\n".join(calls) or text or json.dumps(message)
        if choice.get("finish_reason") == "length":
            if not calls and not text.strip() and message.get("reasoning"):
                raise ProviderInvalidOutput(TRUNCATED_BY_REASONING, usage, raw)
            raise ProviderInvalidOutput(TRUNCATED, usage, raw)
        # Strategies 2-3 answer in the text; a tools model that answered in text instead of
        # calling the tool gets the same lenient parse.
        try:
            answers = [json.loads(c) for c in calls] if calls else [first_json_object(text)]
        except ValueError as exc:
            if calls:
                what = f"{TOOL_NAME} arguments are not JSON"
            elif strategy == "tools":
                what = f"no {TOOL_NAME} tool call in response"
            else:
                what = "no JSON object in response"
            raise ProviderInvalidOutput(what, usage, raw) from exc
        try:
            outputs = [ToolOutput.model_validate(data) for data in answers]
        except ValidationError as exc:
            raise ProviderInvalidOutput(
                f"output failed validation ({exc.error_count()} errors)", usage, raw
            ) from exc
        return ExtractResult(
            items=[item for output in outputs for item in output.items],
            receipt=next((o.receipt for o in outputs if o.receipt is not None), None),
            overview_image=next(
                (o.overview_image for o in outputs if o.overview_image is not None), None
            ),
            usage=usage,
        )


def _read_body(response: httpx.Response, deadline: float) -> bytes:
    """The response body, read within ``deadline`` (monotonic) and MAX_RESPONSE_BYTES."""
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ProviderInvalidOutput(
                "OpenRouter response too large", Usage(), "", maybe_billed=True
            )
        chunks.append(chunk)
        if time.monotonic() > deadline:
            raise ProviderTimeoutError(maybe_billed=True)
    return b"".join(chunks)
