# AI provider contract

Capture code talks to AI only through three modules:

- `app/ai/provider.py`: the `AIProvider` protocol and the error classes.
- `app/ai/schemas.py`: the request and result types, plus the forced-tool JSON schema.
- `app/ai/registry.py`: `get_provider(settings, model=None)` and `make_provider_for(settings)`.

Only adapter modules inside `app/ai/` may import a vendor SDK. `tests/unit/test_no_sdk_imports.py` checks this for `anthropic` (AC-21).

## Interface

```python
class AIProvider(Protocol):
    name: str  # "anthropic", "openrouter", "fake", ...
    model: str  # recorded on captures.model

    def extract(self, req: ExtractRequest) -> ExtractResult: ...
```

`extract` is synchronous. Only the single capture worker thread calls it, outside any database transaction.

**`ExtractRequest`**

| Field | Type | Notes |
|---|---|---|
| `mode` | `"shelf" \| "receipt" \| "text" \| "reconcile" \| "extend"` | `extend`: an "add another angle" capture (see below) |
| `images` | `list[bytes]` | JPEG with long edge ≤ `AI_MAX_IMAGE_EDGE`, produced by `app.capture.images.prepare_images`. **Image `i`** = the `i`-th entry (0-based): prompts, the `Image {i}:` labels and box indexes all use this numbering. A capture sends all of its photos in one call (1-6; an extension sends its whole group, up to 12), in photo position order. |
| `text` | `str \| None` | The sentence in text mode. An optional user note in the other modes. |
| `known_locations` | `list[str]` | Text mode. The model copies a matching name into `location_hint`. |
| `existing_items` | `list[ExistingItem(id, name, quantity, gone=False)]` | Reconcile and extend modes. The model sets `matched_existing_id` from this list. `gone=True` (extend only): a group item moved elsewhere or used up since the older photos, sent as "not here any more — do not re-add". |
| `timeout_s` | `float` | `images.call_timeout(AI_TIMEOUT_SECONDS, len(images))` = `AI_TIMEOUT_SECONDS + 10 s × (images − 1)`. |

**`ExtractResult`**

- `items: list[ExtractedItem]`. Each item has:
  - `name` (non-empty)
  - `quantity` (int ≥ 1)
  - `category` (str or None)
  - `kind` (`consumable` or `belonging`)
  - `confidence` (0..1)
  - `location_hint`, `price_cents`, `purchase_date` (ISO date) and `matched_existing_id`, each of which may be None.
  - `boxes` (None or a list; see **Boxes**).
- `receipt: ReceiptMeta(merchant, purchase_date) | None`
- `overview_image: int | None`: photo modes, the index of the image that shows the most of the place (see **Overview image**).
- `usage: Usage(input_tokens, output_tokens, cost_usd=None)`. `cost_usd` is what the provider charged for the call in USD, when it reports it (OpenRouter does; Anthropic does not).

The model answers through one forced tool call named `record_items`. Its input schema is `schemas.TOOL_INPUT_SCHEMA`, which is written inline with no `$ref`, and adapters validate the tool input with `schemas.ToolOutput`.

The receipt prompt asks for product lines only: no tax, total, subtotal, bag, deposit, change or tip lines. Step 5's apply step still runs the denylist regex as a second filter (AC-17).

### Images and prompts (multi-photo)

Both adapters put a text part `Image {i}:` (`prompts.image_label(i)`) right before image `i`. The photo prompts (`shelf`, `reconcile`, `extend`) say that the images are different views of the same place, that each physical object is listed **once** across views and counted without double counting, and explain the boxes; `user_text` adds "There are N images (Image 0 to Image N-1)." Receipt and text prompts ask for `boxes: null`.

`extend` prompt: all images are views of the same place (older ones first, then the new angles); match seen objects to the existing ids (never invent an id), count across views, and never list an item marked "not here any more — do not re-add" as new (match it instead).

### Output caps

`schemas.output_cap(mode, default)`: `shelf`, `reconcile` and `extend` requests get `max_tokens` 16,384 (`BOX_OUTPUT_TOKENS`; a many-item answer with boxes in several views is long); receipts and text keep 4,096 (`DEFAULT_MAX_TOKENS`). Each adapter exposes `max_tokens_for(req)`, and the ledger's maybe-billed estimate uses the cap of that request.

### Boxes

`ExtractedItem.boxes`: `[{"image": i, "box_2d": [ymin, xmin, ymax, xmax]}]`, integers 0-1000 relative to image `i` (0,0 = top-left; Gemini's native convention), at most 4 boxes per item per image. Boxes are best effort and **never fail a call or a capture**: a `mode="before"` validator (`schemas.clean_boxes`) keeps only valid entries and never raises — wrong shapes, strings, bools, NaN/inf, 3- or 5-number boxes, negative indexes are dropped; coordinates are clamped to 0-1000 and rounded; zero or negative areas are dropped; a 5th box in the same image is dropped; a non-list value becomes None. `schemas.boxes_for_images(item, n_images)` also drops indexes ≥ the number of images sent and converts to fractions (`Fractions(image, x0, y0, x1, y1)`, 0..1). Capture apply and comparisons use the same functions. The tool schema has `boxes` (nullable array); `strict_json_schema()` also drops `minItems`/`maxItems`.

Geometry: the worker writes an upright display JPEG for every photo (EXIF-transposed, long edge 2000) and prepares the AI image from it, both aspect-preserving, so 0-1000 values map 1:1 onto the display JPEG (`photos.width/height` = its size). Receipts and text never store boxes.

### Overview image

The tool output has an optional top-level `overview_image` (`{"type": ["integer", "null"]}` in `TOOL_INPUT_SCHEMA`; nullable, so the strict schema can require it): the 0-based index of the image that shows the most of the place (the widest, most zoomed-out view). The photo prompts ask for it in `_VIEWS_AND_BOXES`; receipt and text prompts ask for null. Like boxes it **never fails a call or a capture**: a `mode="before"` validator on `ToolOutput` and `ExtractResult` (`schemas.clean_overview_image`) keeps a non-bool int ≥ 0 and turns anything else (string, bool, negative, float, object) into None. Both adapters pass it through (OpenRouter: the first non-null across the merged `record_items` calls). At apply time (`boxes.overview_photo`) an index ≥ the number of images sent is ignored too; without a valid one, the call photo with the most item boxes, then the first photo, is the capture's `captures.overview_photo_id` (shelf, reconcile and extend apply). Comparisons ignore it.

## Errors

Every error is a `ProviderError` with `.usage: Usage | None`, the usage the provider reported for the failed call.

| Class | When | `usage` | Ledger outcome |
|---|---|---|---|
| `ProviderTransientError` | connection errors, 408/409/429, any 5xx including 529 overloaded; `.retry_after` is the response's `Retry-After` in seconds (None without one; both adapters read it with `provider.parse_retry_after`: seconds or an HTTP date, a past date is 0, anything above 3600 s is 3600, non-finite is None) | usually None | `transient_error` |
| `ProviderRateLimited` (a `ProviderTransientError` subclass) | HTTP 429 (both adapters) | usually None | `transient_error` |
| `ProviderTimeoutError` (a `ProviderTransientError` subclass) | `timeout_s` exceeded | always None (`maybe_billed` when the request had been sent) | `timeout` |
| `ProviderPermanentError` | 400, 401, 403, 404, 413, 422, other 4xx, SDK misuse, unexpected SDK errors | usually None | `permanent_error` |
| `ProviderInvalidOutput(message, usage, raw)` | billed response without the tool call, output truncated at `max_tokens`, or tool input that fails validation | **always set** | `invalid_output` |

`provider.ledger_outcome(exc_or_None)` maps an outcome to the `ai_calls.outcome` value.

Every `ProviderError` also has `.detail: str | None`: the provider's own message when the exception's message is a plain-language one instead (model gone, no structured output). Provider text in messages and details is redacted (never the key, nor anything shaped like an OpenRouter key `sk-or-…`) and collapsed to one line of at most 500 characters (`provider.bounded`); comparison results store `error` / `error_detail` bounded the same way. Code that turns `retry_after` into a delay uses `provider.capped_retry_after(error, cap)`, so no value (huge, negative, NaN) can overflow a `timedelta`.

Adapters must not retry. The Anthropic adapter sets `max_retries=0`, so one `extract()` is one billed call and one ledger row. The worker handles retries by re-queuing the capture with `not_before` = now + `max(5 s, retry_after)` — a 429 without `Retry-After` waits 30 s, then 60 s — at most 5 minutes and within its 3 attempts (`worker.retry_delay`). The one exception is the OpenRouter strategy fallback below: its extra requests are refused by OpenRouter before any model runs (not billed, no usage), so the `extract()` is still one billed call and one ledger row.

## Usage-reporting rule

Whenever the provider's response carried usage, the adapter reports it: on the result on success, or on the exception (`ProviderInvalidOutput.usage`) on failure. The worker writes one `ai_calls` row immediately after every call returns or raises:

```python
record_ai_call(
    conn, settings, capture_id, started_at, exc.usage if exc else result.usage, ledger_outcome(exc)
)
```

This way billed-but-failed calls still count toward the budget. The worker and comparisons pass the usage through `ledger_usage` first:

- `ledger_usage(conn, settings, provider, usage, error, request=None) -> (usage, model_info)`: for OpenRouter calls without a reported cost it looks up the model's catalog entry (the cost fallback). An error with `maybe_billed=True` and no usage (the OpenRouter adapter sets it for a timeout or dropped connection after the request was sent, and for an oversized response) is recorded at a conservative estimate: the input estimate for the request's image count (`token_estimate`) plus the request's full output cap (`output_cap(request.mode, provider.max_tokens)`), at the catalog prices. It never raises.
- An Anthropic timeout has no usage and records 0 tokens. This is a known undercount, and the Settings page says so.

The ledger (`app/capture/budget.py`):

- `cost_micros_for(usage, settings, model_info=None, *, provider=None) -> int`: `usage.cost_usd` when present; else the catalog prices of `model_info` (an `openrouter_catalog.ModelInfo`, with a logged warning); else, when `provider` is not `"openrouter"`, `AI_INPUT_PRICE_PER_MTOK` / `AI_OUTPUT_PRICE_PER_MTOK`; else 0. An OpenRouter call never uses those (Anthropic) prices: without a cost or catalog price it records 0 and logs an error.
- `record_ai_call(conn, settings, capture_id, started_at, usage, outcome, *, comparison_result_id=None, model_info=None, provider=None, image_count=None) -> int` writes one row in its own `write_tx`. The worker and comparisons pass `image_count` (images sent) into `ai_calls.image_count`.
- `capture_token_estimate(conn) -> (input, output, from_history)`: the average tokens of the last 20 `ok` calls, or 1,500 / 400 (used for calls without images).
- `token_estimate(conn, images) -> (input, output, from_history)`: image-aware: a per-call base (300 in / 100 out) plus `images` × the per-image average of the last 20 `ok` calls that sent images (`(tokens − base) / image_count`; rows from before image counts count as 1 image), else 1,200 / 300 per image — so a 1-photo capture defaults to 1,500 / 400. Used by the Settings picker ("per 1-photo capture"), the comparison estimate (per case photo count) and `ledger_usage`. `PICKER_LIMIT` (50) caps the picker rows.
- `insert_ai_call(...)`, same arguments, writes the row inside the caller's `write_tx` (for writes that must commit together with the ledger row, such as a comparison result).

## Registry

- `get_provider(settings, model=None) -> AIProvider | None`. `model` overrides `AI_MODEL` (the worker passes the effective model, see below).
  - `AI_PROVIDER=anthropic` returns `AnthropicProvider`, or **`None` when `AI_API_KEY` is unset**. `None` means AI is disabled: callers must check for it, show `ai_disabled_reason(settings, model)`, and leave manual entry working.
  - `AI_PROVIDER=openrouter` returns `OpenRouterProvider`, or `None` without `OPENROUTER_API_KEY` or when the model is not a valid OpenRouter id (`openrouter_catalog.valid_model_id`).
  - `AI_PROVIDER=fake` returns a `FakeProvider`. It loads `FAKE_AI_SCRIPT` when that is set; otherwise its script is empty and every call raises `ProviderPermanentError("fake provider script exhausted")`. With `model`, the provider reports that model; without it, the script's `model`.
  - An unknown name raises `UnknownProviderError`, a subclass of `ValueError`. `Settings` already rejects unknown names at startup.
- `openrouter_provider(settings, model=None)`: the OpenRouter factory, usable whatever `AI_PROVIDER` is (model comparisons); `None` without `OPENROUTER_API_KEY` or with an invalid id.
- `make_provider_for(settings, provider_name=None) -> Callable[[str], AIProvider | None]`: a thread-safe `provider_for(model_id)` memoized per model id. The fake provider is built once and each model gets a `with_model` view of it, so one `FAKE_AI_SCRIPT` is consumed across captures and model changes.
- `ai_disabled_reason(settings, model=None) -> str | None` returns the user-facing message (no key for the provider, or, for OpenRouter, an invalid model id), or None when AI is available. It never builds a provider.

## Active model

`app/ai/model_choice.py` (read-only): `effective_model(conn, settings) -> str` is the model stored by the Settings picker (`settings` key `ai_model`) when the model is selectable (`AI_PROVIDER=openrouter`, or the test-only fake with `OPENROUTER_CATALOG_FILE`), else `AI_MODEL`. Writes live in the repository `app/capture/model_setting.py`: `set_model(conn, model_id)` and `clear_model(conn)`.

The worker (`Worker(settings, provider=None, clock=utcnow, *, provider_for=None, compare_provider_for=None)`) resolves `provider_for(effective_model(conn, settings))` on every tick, so a new model applies to the next capture without a restart, and each capture records the provider name and model that ran it. `run_once(conn, settings, provider, ...)` still takes a fixed provider (tests).

## Model comparisons

When no capture is claimable, a worker tick executes **one** pending model-comparison result (`app/compare/execution.run_once`) with `compare_provider_for(result.model)`: the OpenRouter factory (`make_provider_for(settings, "openrouter")`), or the worker's own `provider_for` when `AI_PROVIDER` is `openrouter` or `fake` (so one `FAKE_AI_SCRIPT`, including its `"models"` scripts, serves captures and comparisons). The request is built like a real capture of the case's type: `images.prepare_images(path, max_edge=AI_MAX_IMAGE_EDGE)` for **every case photo in order** (`benchmark_cases.photo_ids_json`: the capture group's photos when the case was made; older cases use their capture's photos) — no `photos` rows, no display JPEGs — the same mode (`shelf`, `receipt`, `text`; the plain shelf prompt even for a group with extensions, never `extend` or `existing_items`), `known_locations` for text, and the image-scaled timeout. Predictions go through `apply.normalize_predictions` before scoring; photo predictions also store their validated boxes in `items_json` (`{"image", "photo_id", "x0", "y0", "x1", "y1"}`), shown in the report but not scored. The ledger row records `image_count`.

Each call writes its ledger row with `insert_ai_call(..., capture_id=None, comparison_result_id=<result id>)` in the same `write_tx` as the result update, which is conditional on the result still being `pending` (a cancel during the call wins; the ledger row is still written). The same error classes apply: a `ProviderRateLimited` (429) re-queues the result up to `RATE_LIMIT_ATTEMPTS` (4) attempts in all, after `max(retry_after, 30 s / 60 s / 120 s)` for attempts 1 / 2 / 3 (`retry_after` counted up to 10 minutes), and is stored with a plain-language `error` ("Rate-limited by the model's provider — busy right now; retried N times.", or "…; trying again in N s." while waiting) and the adapter's message in `comparison_results.error_detail`; any other `ProviderTransientError` on the first attempt re-queues the result after `max(30 s, retry_after)`, except `ProviderTimeoutError` (it may have been billed, so it is not repeated); anything else fails that result only. A result claimed more than `GIVE_UP_AFTER` (6) times (crash loops) fails without another call. Anything that raises after the call returned (scoring, cost lookup) fails the result with `internal error: <Type>: <message>`, and the same `write_tx` still writes the ledger row. Each result is scored against the case's expected items as snapshotted when the run started (`comparison_results.expected_json`), so editing a case during a run does not change how the models are scored, and the report shows that snapshot. Comparisons never write items, captures, photos or proposals.

## OpenRouter adapter

`app/ai/openrouter_adapter.py` calls `POST https://openrouter.ai/api/v1/chat/completions` with plain `httpx` (no SDK, no retries). Headers: `Authorization: Bearer <OPENROUTER_API_KEY>` and `X-Title: Wherebox` (no `HTTP-Referer`). The key never appears in messages, exceptions or logs.

The request carries the same prompts (`prompts.system_prompt`, `prompts.user_text`), then for each image a text part `Image {i}:` followed by `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,…"}}`, and `max_tokens` = `output_cap(mode, 4096)` (16,384 for photo modes; the Anthropic adapter uses the same caps). The Anthropic adapter sends each `Image {i}:` text block before its image block, and the user text last. The **output strategy** comes from the model's catalog entry:

| Strategy | When (`supported_parameters`) | Request | Answer parsed from |
|---|---|---|---|
| `tools` | `tools` and `tool_choice` | `tools` = one function `record_items` with `TOOL_INPUT_SCHEMA`, forced `tool_choice`, `provider.require_parameters: true` | the tool call's `arguments` |
| `json_schema` | else `structured_outputs` or `response_format` | `response_format: {"type": "json_schema", "json_schema": {"name": "record_items", "strict": true, "schema": schemas.strict_json_schema()}}`, `require_parameters` | the message text |
| `prompt` ("best effort") | neither | the system prompt spells out the schema (`prompts.json_answer`) | the first JSON object in the text (prose and code fences ignored) |

Unknown capabilities (catalog unavailable) use `tools`. **Fallback:** a 404 (or a 200 `error` with code 404) whose message matches "No endpoints found that support the provided '<param>'" (no provider serves that parameter with `require_parameters`), or the generic "No endpoints found that can handle the requested parameters.", is not billed and is never raised: the same `extract()` retries at once without it — an unsupported `reasoning` or `parallel_tool_calls` is dropped, any other parameter (`tool_choice`, `tools`, `response_format`, `structured_outputs`, …) or the generic message moves to the next strategy, `tools` → `json_schema` → `prompt` (which sends no `require_parameters`). The working strategy and dropped parameters are remembered per model id in memory as soon as OpenRouter accepts a fallback request (any answer but "no endpoints", even an error), merged monotonically under a lock (the later strategy, the union of dropped parameters), for one hour (monotonic clock); after that the model is tried at its catalog strategy again (`openrouter_adapter.clear_downgrades()` forgets them all). Later captures and comparisons start there; each downgrade is logged at INFO (the parameter name capped at 64 characters). When every strategy is refused the call raises `ProviderPermanentError("No provider for <model> supports structured output right now")` with the last 404 as `detail` (and as `__cause__`). The fallback shares the call's `timeout_s`. With `tools`, every `record_items` call in the answer counts: their items are concatenated and the receipt is the first non-null one; `arguments` delivered as a JSON object instead of a string are accepted. `parallel_tool_calls: false` is sent only when the model lists `parallel_tool_calls` (with `require_parameters`, an unlisted parameter would leave no provider to route to). When the catalog's `reasoning` object says reasoning is on by default (or mandatory) at an effort above `low` and `low` is accepted, the request adds `reasoning: {"effort": "low"}` (OpenRouter, "Reasoning Tokens": the effort is mapped per model; reasoning tokens count against `max_tokens`).

The response is streamed: the whole call must finish within `timeout_s` (checked between chunks; connect timeout at most 10 s), else `ProviderTimeoutError`; a body over 2 MB is `ProviderInvalidOutput("OpenRouter response too large")`. Both are marked `maybe_billed`. The prompt strategy only searches the first 100,000 characters of the text for the JSON object. All providers share one `httpx.Client` (thread-safe). `strict_json_schema()` lists every property as required (optional ones are nullable already), sets `additionalProperties: false` and drops numeric bounds; `ToolOutput` still enforces them. If the catalog says the model does not accept images and the request has images, the adapter raises `ProviderPermanentError("Model <id> does not accept images")` before calling.

Usage: `usage.prompt_tokens`, `usage.completion_tokens` and `usage.cost` (credits = USD) → `Usage(input_tokens, output_tokens, cost_usd)`, on success, on invalid output, and on error bodies that carry usage.

| Response | Error |
|---|---|
| timeout (whole call over `timeout_s`) | `ProviderTimeoutError` (`maybe_billed` unless it never connected) |
| connection error, 408, 5xx, non-JSON body | `ProviderTransientError` (with `retry_after` from a `Retry-After` header: seconds or an HTTP date) |
| 429 | `ProviderRateLimited` (with `retry_after`) |
| 404 "No endpoints found that support the provided '<param>'" or "No endpoints found that can handle the requested parameters." | not raised: the strategy fallback above |
| 200 with an `error` object | mapped by its `error.code` like an HTTP status (no code: transient) |
| 200 with `finish_reason: "error"` | `ProviderTransientError` |
| 402 | `ProviderPermanentError("OpenRouter: insufficient credits")` |
| other 404 (except "No endpoints found…"), or 400/404 saying the model is not valid / not found | `ProviderPermanentError("Model <id> is no longer available on OpenRouter — pick another in Settings")` (OpenRouter's message as `detail`) |
| any other 404 "No endpoints found…" (e.g. "No endpoints found for <model>": OpenRouter's docs do not say whether that means the model is gone or only that no provider serves it now) | `ProviderPermanentError("OpenRouter HTTP 404: <OpenRouter's message>")` |
| other 4xx | `ProviderPermanentError("OpenRouter HTTP <status>: <OpenRouter's message>")` |
| `finish_reason: "length"` | `ProviderInvalidOutput("output truncated")`, or `"output truncated (reasoning used the token cap)"` when there is no content or tool call but reasoning |
| no tool call / no JSON object / fails `ToolOutput` | `ProviderInvalidOutput` with usage and raw |

## OpenRouter catalog

`app/ai/openrouter_catalog.py` reads the public `GET https://openrouter.ai/api/v1/models` (no key; 10 s timeout). It keeps the list in memory for an hour behind a lock (concurrent callers fetch once), persists the last good copy to `DATA_DIR/cache/openrouter_models.json` (the lifespan creates `DATA_DIR/cache`), and falls back to the memory or persisted copy when a fetch fails (next attempt after 5 minutes). A refresh (at most hourly) blocks its caller, a request or the worker, for up to the 10 s fetch timeout. `OPENROUTER_CATALOG_FILE` (test-only) reads a local file of the same shape instead, and only when `AI_PROVIDER=fake`.

- `ModelInfo(id, name, context_length, prompt_usd_per_mtok, completion_usd_per_mtok, accepts_images, strategy, parallel_tool_calls=False, lower_reasoning=False)` with `price_known`, `is_router` (`openrouter/*` routing pseudo-models, excluded from comparisons), `is_free` and `cost_usd(input_tokens, output_tokens)`. Prices that are not non-negative numbers (OpenRouter reports `-1` for `openrouter/auto`) are `None`: "price unknown".
- `load_catalog(settings) -> Catalog` (`models`, `source` live/cache/file/none, `fetched_at`, `error`, `get(id)`, `available`, `fresh`).
- `list_models(settings, query="", images_only=True) -> list[ModelInfo]`: priced models only, matching every query term in the id or name.
- `get_model(settings, id) -> ModelInfo | None`, price-unknown entries included.
- `valid_model_id(id) -> bool`: full match of `~?[A-Za-z0-9._-]+/[A-Za-z0-9._:~-]+` (at most 200 characters; the `~` prefix is OpenRouter's "latest" aliases). Every id in the live catalog of 2026-09-30 matches (`tests/fixtures/openrouter/model_ids.txt`).

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
                 "purchase_date": "2026-09-14", "matched_existing_id": null,
                 "boxes": [{"image": 0, "box_2d": [120, 40, 480, 300]}]}],
      "receipt": {"merchant": "Corner Grocery", "purchase_date": "2026-09-14"},
      "overview_image": null,
      "usage": {"input_tokens": 1200, "output_tokens": 150}
    }},
    {"delay_s": 0.5, "result": {"items": []}},
    {"error": "transient", "message": "HTTP 529 overloaded", "usage": null},
    {"error": "rate_limited", "retry_after_s": 20},
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
- `models` (optional): `{"<model id>": {"steps": [...]}}`, a separate step list per model. A provider for a model listed here (`provider.with_model(id)`, or the worker's `provider_for(id)`) consumes that list; any other model uses the top-level `steps`. `repeat_last` applies to every list.

A step is exactly one of these:

- `{"result": ExtractResult}`. `usage` defaults to 0/0 when omitted. Items may carry `boxes`; invalid entries are dropped exactly as for a real model. The result may carry `overview_image`, cleaned the same way (an invalid value becomes null).
- `{"error": "transient" | "permanent", "message"?, "usage"?}`. `usage` may be null or omitted. A transient step may add `retry_after_s` (`ProviderTransientError.retry_after`).
- `{"error": "rate_limited", "message"?, "usage"?, "retry_after_s"?}` raises `ProviderRateLimited` (a 429); `message` defaults to "OpenRouter HTTP 429: Provider returned error".
- `{"error": "invalid_output", "message"?, "usage"?, "raw"?}`. `usage` defaults to 0/0.
- `{"error": "timeout"}`. The fake sleeps just past the request's `timeout_s`, then raises `ProviderTimeoutError`.

Any step may add `delay_s`. If `delay_s` is greater than the request's `timeout_s`, the fake sleeps past the timeout and raises `ProviderTimeoutError`, as a real client-side timeout would.

In Python, the constructor is `FakeProvider(script, *, model, delay_s, repeat_last, sleep, models)`. `with_model(id)` returns a view reporting that model that shares the scripts and `requests` log. `script` items may be:

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
2. Add one entry to `PROVIDERS` in `app/ai/registry.py`: `"ollama": _ollama`. The factory takes `(settings, model)` (`model` None means `AI_MODEL`) and returns None when the adapter is not configured. Also add the name to the `Settings.ai_provider` Literal.
3. Add the adapter to the `make_provider` fixture parameters in `tests/unit/test_provider_contract.py`, driven by a mocked transport, so the shared contract tests cover it (the Anthropic and OpenRouter adapters are there).

No capture code changes.
