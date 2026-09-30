# AI provider contract

Capture code talks to AI only through three modules:

- `app/ai/provider.py`: the `AIProvider` protocol and the error classes.
- `app/ai/schemas.py`: the request and result types, plus the forced-tool JSON schema.
- `app/ai/registry.py`: `get_provider(settings)`.

Only adapter modules inside `app/ai/` may import a vendor SDK. `tests/unit/test_no_sdk_imports.py` checks this for `anthropic` (AC-21).

## Interface

```python
class AIProvider(Protocol):
    name: str  # "anthropic", "fake", ...
    model: str  # recorded on captures.model

    def extract(self, req: ExtractRequest) -> ExtractResult: ...
```

`extract` is synchronous. Only the single capture worker thread calls it, outside any database transaction.

**`ExtractRequest`**

| Field | Type | Notes |
|---|---|---|
| `mode` | `"shelf" \| "receipt" \| "text" \| "reconcile"` | |
| `images` | `list[bytes]` | JPEG with long edge ≤ `AI_MAX_IMAGE_EDGE`, produced by `app.capture.images.prepare_images` |
| `text` | `str \| None` | The sentence in text mode. An optional user note in the other modes. |
| `known_locations` | `list[str]` | Text mode. The model copies a matching name into `location_hint`. |
| `existing_items` | `list[ExistingItem(id, name, quantity)]` | Reconcile mode. The model sets `matched_existing_id` from this list. |
| `timeout_s` | `float` | Pass `settings.ai_timeout_seconds`. |

**`ExtractResult`**

- `items: list[ExtractedItem]`. Each item has:
  - `name` (non-empty)
  - `quantity` (int ≥ 1)
  - `category` (str or None)
  - `kind` (`consumable` or `belonging`)
  - `confidence` (0..1)
  - `location_hint`, `price_cents`, `purchase_date` (ISO date) and `matched_existing_id`, each of which may be None.
- `receipt: ReceiptMeta(merchant, purchase_date) | None`
- `usage: Usage(input_tokens, output_tokens)`

The model answers through one forced tool call named `record_items`. Its input schema is `schemas.TOOL_INPUT_SCHEMA`, which is written inline with no `$ref`, and adapters validate the tool input with `schemas.ToolOutput`.

The receipt prompt asks for product lines only: no tax, total, subtotal, bag, deposit, change or tip lines. Step 5's apply step still runs the denylist regex as a second filter (AC-17).

## Errors

Every error is a `ProviderError` with `.usage: Usage | None`, the usage the provider reported for the failed call.

| Class | When | `usage` | Ledger outcome |
|---|---|---|---|
| `ProviderTransientError` | connection errors, 408/409/429, any 5xx including 529 overloaded | usually None | `transient_error` |
| `ProviderTimeoutError` (a `ProviderTransientError` subclass) | `timeout_s` exceeded | always None | `timeout` |
| `ProviderPermanentError` | 400, 401, 403, 404, 413, 422, other 4xx, SDK misuse, unexpected SDK errors | usually None | `permanent_error` |
| `ProviderInvalidOutput(message, usage, raw)` | billed response without the tool call, output truncated at `max_tokens`, or tool input that fails validation | **always set** | `invalid_output` |

`provider.ledger_outcome(exc_or_None)` maps an outcome to the `ai_calls.outcome` value.

Adapters must not retry. The Anthropic adapter sets `max_retries=0`, so one `extract()` is one billed call and one ledger row. The worker handles retries by re-queuing the capture with `not_before`.

## Usage-reporting rule

Whenever the provider's response carried usage, the adapter reports it: on the result on success, or on the exception (`ProviderInvalidOutput.usage`) on failure. The worker writes one `ai_calls` row immediately after every call returns or raises:

```python
record_ai_call(
    conn, settings, capture_id, started_at, exc.usage if exc else result.usage, ledger_outcome(exc)
)
```

This way billed-but-failed calls still count toward the budget. A timeout has no usage and records 0 tokens. This is a known undercount, and the Settings page says so.

## Registry

- `get_provider(settings) -> AIProvider | None`.
  - `AI_PROVIDER=anthropic` returns `AnthropicProvider`, or **`None` when `AI_API_KEY` is unset**. `None` means AI is disabled: callers must check for it, show `ai_disabled_reason(settings)`, and leave manual entry working.
  - `AI_PROVIDER=fake` returns a `FakeProvider`. It loads `FAKE_AI_SCRIPT` when that is set; otherwise its script is empty and every call raises `ProviderPermanentError("fake provider script exhausted")`.
  - An unknown name raises `UnknownProviderError`, a subclass of `ValueError`. `Settings` already rejects unknown names at startup.
- `ai_disabled_reason(settings) -> str | None` returns the user-facing message, or None when AI is available. It never builds a provider.

## FAKE_AI_SCRIPT format

The script is a JSON object. Each `extract()` call records its request on `provider.requests` and consumes the next step.

```json
{
  "model": "fake-model",
  "delay_s": 0.0,
  "repeat_last": false,
  "steps": [
    {"result": {
      "items": [{"name": "Milk", "quantity": 2, "category": "Food", "kind": "consumable",
                 "confidence": 0.95, "location_hint": null, "price_cents": 129,
                 "purchase_date": "2026-09-14", "matched_existing_id": null}],
      "receipt": {"merchant": "Corner Grocery", "purchase_date": "2026-09-14"},
      "usage": {"input_tokens": 1200, "output_tokens": 150}
    }},
    {"delay_s": 0.5, "result": {"items": []}},
    {"error": "transient", "message": "HTTP 529 overloaded", "usage": null},
    {"error": "permanent", "message": "HTTP 400", "usage": {"input_tokens": 10, "output_tokens": 0}},
    {"error": "invalid_output", "usage": {"input_tokens": 900, "output_tokens": 40}, "raw": "{\"items\": 5}"},
    {"error": "timeout"}
  ]
}
```

Top-level keys:

- `steps` (required): the list of steps described below.
- `model` (optional): defaults to `fake-model`.
- `delay_s` (optional): the default delay for every step.
- `repeat_last` (optional): when true, keep replaying the last step after the list is used up. When false, an exhausted script raises `ProviderPermanentError`.

A step is exactly one of these:

- `{"result": ExtractResult}`. `usage` defaults to 0/0 when omitted.
- `{"error": "transient" | "permanent", "message"?, "usage"?}`. `usage` may be null or omitted.
- `{"error": "invalid_output", "message"?, "usage"?, "raw"?}`. `usage` defaults to 0/0.
- `{"error": "timeout"}`. The fake sleeps just past the request's `timeout_s`, then raises `ProviderTimeoutError`.

Any step may add `delay_s`. If `delay_s` is greater than the request's `timeout_s`, the fake sleeps past the timeout and raises `ProviderTimeoutError`, as a real client-side timeout would.

In Python, the constructor is `FakeProvider(script, *, model, delay_s, repeat_last, sleep)`. `script` items may be:

- an `ExtractResult`;
- any exception instance, raised as-is;
- a `FakeStep(outcome, delay_s, timeout)`.

`provider.push(step)` appends to the queue. `sleep` can be injected, so tests never actually wait.

## Adding an adapter (for example Ollama)

1. Write `app/ai/ollama_adapter.py` with a class that has `name`, `model` and `extract(req)`.
   - Build the prompt from `app.ai.prompts.system_prompt(req.mode)` and `prompts.user_text(req)`.
   - Ask for JSON that matches `schemas.TOOL_INPUT_SCHEMA`, and validate it with `schemas.ToolOutput`.
   - Map the client's errors to the classes above, and report usage on success and on invalid output.
   - Do not retry inside the adapter.
2. Add one entry to `PROVIDERS` in `app/ai/registry.py`: `"ollama": _ollama`. The factory returns None when the adapter is not configured. Also add the name to the `Settings.ai_provider` Literal.
3. Add the adapter to the `provider` fixture parameters in `tests/unit/test_provider_contract.py`, driven by a mocked transport, so the shared contract tests cover it.

No capture code changes.
