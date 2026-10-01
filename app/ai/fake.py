"""Scripted provider for tests, e2e runs and the restore check (AI_PROVIDER=fake).

The script is a queue of steps; each extract() call records the request and consumes one
step. The JSON format read from FAKE_AI_SCRIPT is documented in docs/ai-provider-contract.md.
"""

import copy
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
    ProviderRateLimited,
    ProviderTimeoutError,
    ProviderTransientError,
)
from app.ai.schemas import ExtractRequest, ExtractResult, Usage

DEFAULT_MODEL = "fake-model"
# How long past the request's timeout a "timeout" step sleeps before raising, in seconds.
TIMEOUT_OVERSHOOT_S = 0.01
# Default message of a "rate_limited" step (what OpenRouter says for an upstream 429).
RATE_LIMITED_MESSAGE = "OpenRouter HTTP 429: Provider returned error"


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


class _Script:
    """A queue of steps plus its repeat-last behaviour."""

    def __init__(self, steps: Iterable[Scriptable], repeat_last: bool) -> None:
        self.queue: deque[FakeStep] = deque(
            step if isinstance(step, FakeStep) else FakeStep(outcome=step) for step in steps
        )
        self.repeat_last = repeat_last
        self.last: FakeStep | None = None


class _State:
    """What every model view of one FakeProvider shares: scripts, lock, recorded requests."""

    def __init__(self, default: _Script, models: dict[str, _Script]) -> None:
        self.default = default
        self.models = models
        self.requests: list[ExtractRequest] = []
        self.lock = threading.Lock()


class FakeProvider:
    """``models`` maps a model id to its own script; any other model uses ``script``.
    ``with_model(id)`` returns a view reporting that model that shares this provider's
    scripts and request log, so one FAKE_AI_SCRIPT is consumed across model changes."""

    name = "fake"

    def __init__(
        self,
        script: Iterable[Scriptable] = (),
        *,
        model: str = DEFAULT_MODEL,
        delay_s: float = 0.0,
        repeat_last: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        models: dict[str, Iterable[Scriptable]] | None = None,
    ) -> None:
        self.model = model
        self.delay_s = delay_s
        self._sleep = sleep
        self._state = _State(
            _Script(script, repeat_last),
            {model_id: _Script(steps, repeat_last) for model_id, steps in (models or {}).items()},
        )

    def with_model(self, model: str) -> "FakeProvider":
        view = copy.copy(self)
        view.model = model
        return view

    @property
    def requests(self) -> list[ExtractRequest]:
        return self._state.requests

    @property
    def repeat_last(self) -> bool:
        return self._state.default.repeat_last

    def _script(self) -> _Script:
        return self._state.models.get(self.model, self._state.default)

    def push(self, step: Scriptable) -> None:
        with self._state.lock:
            self._script().queue.append(
                step if isinstance(step, FakeStep) else FakeStep(outcome=step)
            )

    @property
    def remaining(self) -> int:
        return len(self._script().queue)

    def _next_step(self) -> FakeStep:
        with self._state.lock:
            script = self._script()
            if script.queue:
                script.last = script.queue.popleft()
                return script.last
            if script.repeat_last and script.last is not None:
                return script.last
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
    elif error in ("transient", "rate_limited"):
        retry_after = data.get("retry_after_s")
        kind = ProviderTransientError
        if error == "rate_limited":
            kind, message = ProviderRateLimited, message or RATE_LIMITED_MESSAGE
        outcome = kind(
            message,
            usage=_usage(data.get("usage")),
            retry_after=None if retry_after is None else float(retry_after),
        )
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


def _steps(data: Any, where: str) -> list[FakeStep]:
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise ValueError(f"{where}: fake script must be an object with a 'steps' list")
    return [parse_step(step) for step in data["steps"]]


def load_script(path: str | Path, **kwargs: Any) -> FakeProvider:
    """Build a FakeProvider from a FAKE_AI_SCRIPT JSON file (optional per-model scripts in
    ``"models": {"<id>": {"steps": [...]}}``)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    steps = _steps(data, str(path))
    models = data.get("models") or {}
    if not isinstance(models, dict):
        raise ValueError(f"{path}: 'models' must be an object of model id -> script")
    return FakeProvider(
        steps,
        model=str(data.get("model", DEFAULT_MODEL)),
        delay_s=float(data.get("delay_s", 0.0)),
        repeat_last=bool(data.get("repeat_last", False)),
        models={
            str(model_id): _steps(script, f"{path} models[{model_id!r}]")
            for model_id, script in models.items()
        },
        **kwargs,
    )
