"""Scripted provider for tests, e2e runs and the restore check (AI_PROVIDER=fake).

The script is a queue of steps; each extract() call records the request and consumes one
step. The JSON format read from FAKE_AI_SCRIPT is documented in docs/ai-provider-contract.md.
"""

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ai.provider import (
    ProviderError,
    ProviderInvalidOutput,
    ProviderPermanentError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from app.ai.schemas import ExtractRequest, ExtractResult, Usage

DEFAULT_MODEL = "fake-model"
# How long past the request's timeout a "timeout" step sleeps before raising, in seconds.
TIMEOUT_OVERSHOOT_S = 0.01


@dataclass(frozen=True)
class FakeStep:
    """One scripted outcome.

    ``outcome`` is returned (ExtractResult) or raised (an exception instance).
    ``delay_s`` is slept before answering. A delay longer than the request's ``timeout_s``,
    or ``timeout=True``, sleeps past the timeout and raises ProviderTimeoutError instead.
    """

    outcome: ExtractResult | BaseException | None = None
    delay_s: float = 0.0
    timeout: bool = False


Scriptable = ExtractResult | BaseException | FakeStep


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        script: Iterable[Scriptable] = (),
        *,
        model: str = DEFAULT_MODEL,
        delay_s: float = 0.0,
        repeat_last: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.delay_s = delay_s
        self.repeat_last = repeat_last
        self.requests: list[ExtractRequest] = []
        self._sleep = sleep
        self._queue: deque[FakeStep] = deque()
        self._last: FakeStep | None = None
        self._lock = threading.Lock()
        for step in script:
            self.push(step)

    def push(self, step: Scriptable) -> None:
        with self._lock:
            self._queue.append(step if isinstance(step, FakeStep) else FakeStep(outcome=step))

    @property
    def remaining(self) -> int:
        return len(self._queue)

    def _next_step(self) -> FakeStep:
        with self._lock:
            if self._queue:
                self._last = self._queue.popleft()
                return self._last
            if self.repeat_last and self._last is not None:
                return self._last
        raise ProviderPermanentError("fake provider script exhausted")

    def extract(self, req: ExtractRequest) -> ExtractResult:
        self.requests.append(req)
        step = self._next_step()
        delay = step.delay_s or self.delay_s
        if step.timeout or delay > req.timeout_s:
            self._sleep(req.timeout_s + TIMEOUT_OVERSHOOT_S)
            raise ProviderTimeoutError()
        if delay:
            self._sleep(delay)
        if isinstance(step.outcome, BaseException):
            raise step.outcome
        if step.outcome is None:
            raise ProviderPermanentError("fake step has no outcome")
        return step.outcome.model_copy(deep=True)


def _usage(value: Any) -> Usage | None:
    return None if value is None else Usage.model_validate(value)


def parse_step(data: dict[str, Any]) -> FakeStep:
    """Build one step from its JSON object (see docs/ai-provider-contract.md)."""
    delay_s = float(data.get("delay_s", 0.0))
    error = data.get("error")
    message = data.get("message", "")
    outcome: ExtractResult | ProviderError | None
    if error is None:
        if "result" not in data:
            raise ValueError(f"fake step needs 'result' or 'error': {data!r}")
        result = dict(data["result"])
        result.setdefault("usage", {"input_tokens": 0, "output_tokens": 0})
        outcome = ExtractResult.model_validate(result)
    elif error == "timeout":
        return FakeStep(delay_s=delay_s, timeout=True)
    elif error == "transient":
        outcome = ProviderTransientError(message, usage=_usage(data.get("usage")))
    elif error == "permanent":
        outcome = ProviderPermanentError(message, usage=_usage(data.get("usage")))
    elif error == "invalid_output":
        outcome = ProviderInvalidOutput(
            message or "invalid output",
            usage=_usage(data.get("usage")) or Usage(),
            raw=str(data.get("raw", "")),
        )
    else:
        raise ValueError(f"unknown fake error kind {error!r}")
    return FakeStep(outcome=outcome, delay_s=delay_s)


def load_script(path: str | Path, **kwargs: Any) -> FakeProvider:
    """Build a FakeProvider from a FAKE_AI_SCRIPT JSON file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise ValueError(f"{path}: fake script must be an object with a 'steps' list")
    return FakeProvider(
        [parse_step(step) for step in data["steps"]],
        model=str(data.get("model", DEFAULT_MODEL)),
        delay_s=float(data.get("delay_s", 0.0)),
        repeat_last=bool(data.get("repeat_last", False)),
        **kwargs,
    )
