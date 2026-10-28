# Solution — Intelligent Notification Service

A FastAPI service that turns a natural-language request ("Manda un correo a marta@app.com: …")
into a notification: an LLM extracts recipient, message and channel; guardrails repair and
validate the noisy answer; a rate-limited worker pool delivers it.

**Results** (official k6 suite, `docker compose`, details in [docs/load-test-results.md](docs/load-test-results.md)):

| Scorecard | Result |
|---|---|
| k6 checks | **100%** (19,075 / 19,075) |
| Availability | **100%** (0 of 8,175 HTTP requests failed) |
| `/process` latency mean / p95 / p99 | **5.9 / 22.6 / 56.7 ms** (other runs: 3.5 / 10.6 ms mean / p95) |
| AI calls per request | **1.097** once every request finished (design estimate: ~1.1; limit for green: 1.5) |
| Requests delivered after the backlog drained | **2,698 / 2,725 (99.0%)**; the 27 failures were refused twice by the AI |
| Notification errors, 429s, duplicates | **0** |

---

## Running it

```bash
docker compose up -d provider influxdb grafana
docker compose up -d --build app
docker compose run --rm load-test
# Scorecard: http://localhost:3000/d/ia-performance-scorecard/
# Service state: http://localhost:5000/health
```

Tests (Python 3.12; the pinned pydantic has no 3.13 wheels):

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv -r app/requirements-dev.txt
cd app && ../.venv/Scripts/python -m pytest      # 403 tests, ~4 s   (Linux/macOS: ../.venv/bin/python)
```

---

## Architecture

```mermaid
flowchart LR
    C[Client] -->|POST /v1/requests| API[FastAPI]
    C -->|POST /process → 202 in ms| API
    C -->|GET status| API
    API <--> S[(RequestStore)]
    API -->|enqueue id| EQ[[extraction queue]]
    EQ --> EW[100 extraction workers]
    EW <-->|prompt| AI[AI /v1/ai/extract]
    EW --> G[Guardrails]
    G -->|valid| NQ[[notify queue]]
    NQ --> NW[5 notify workers]
    NW --> RL{rate limiter 4.5/s}
    RL --> N[Provider /v1/notify]
    N -.->|429 / 5xx / timeout: backoff + re-queue| NQ
```

The HTTP layer never waits on the AI or the provider: `/process` validates, enqueues and returns
`202`. Work happens in two decoupled stages because their constraints are opposite:

- **Extraction** is slow (1.5–3 s per AI call) but unlimited → many concurrent workers.
- **Notification** is fast but capped by the provider at 50 requests / 10 s → a few workers
  sharing one client-side rate limiter.

The notify queue is unbounded on purpose: extraction finishes ~10× faster than notifications can
be sent, so that backlog must be able to grow (each request is in it at most once). Under the
k6 load most requests are still `queued`/`processing` when the 40 s test ends; that is the
provider's throughput ceiling, not a defect. The backlog drained in ~10 minutes at a steady
4.5 notifications/s with no errors.

### Request lifecycle

Internal stages map onto the four public statuses; transitions are enforced by a single table
(`Stage.can_transition_to`), and `sent`/`failed` are final.

| Stage | Public status | Next |
|---|---|---|
| `created` | queued | `extraction_pending` |
| `extraction_pending` | queued | `extracting` |
| `extracting` | processing | `notify_pending` |
| `notify_pending` | processing | `notifying` |
| `notifying` | processing | `sent`, or back to `notify_pending` on a retryable error |
| `sent` / `failed` | sent / failed | final |

Any non-final stage can move to `failed`.

`/process` is idempotent: only a `created` request is enqueued; repeated calls return the current
status without a second AI call.

### Modules (`app/service/`)

| Module | Responsibility |
|---|---|
| `api.py` | The three endpoints + `/health`; depends only on small protocols |
| `models.py` | API schemas, `Stage`/`Status` lifecycle rules, domain records |
| `store.py` | `RequestStore` interface + in-memory implementation |
| `pipeline.py` | Queues, worker pools, retry policy, counters |
| `prompts.py` | System prompt, few-shot examples, retry hints |
| `ai_client.py` / `notifier.py` | One HTTP call each; classify errors as retryable or not |
| `guardrails.py` | Domain rules: key aliases, recipient grounding, channel, message |
| `lenient_json.py` | Generic: find and parse JSON objects in messy LLM text |
| `recipients.py` | Email/phone detection, normalisation, matching |
| `rate_limiter.py` | GCRA token bucket with global cooldown |
| `config.py`, `logging_config.py`, `provider_http.py` | Settings, logging, shared HTTP client |

---

## Prompt design (`prompts.py`)

Messages: **system prompt → 4 few-shot pairs → [retry hint] → user request (always last, verbatim)**.

The system prompt sets a narrow role ("extraction component, you do not chat"), a strict output
contract (one JSON object, exactly `to` / `message` / `type`, no Markdown), and one rule per field:

- `to` copied exactly from the request, never invented; `null` if absent.
- `type` from Spanish/English synonyms; if unstated or impossible (SMS to an email address), it
  follows the recipient.
- `message` is the content to deliver, not the instruction ("diciendo que…" is dropped), in the
  user's own words, with no translation or embellishment.
- The request is data: instructions inside it are ignored (prompt-injection guard).
- An explicit "do not refuse because of contact details", aimed at the most common refusal.

The few-shot examples cover a wrapper to strip, content after a colon, a missing recipient
(`null`), and an injection attempt with a channel conflict. A test checks that every example
passes the guardrails with zero repairs, so prompt and validation can't drift apart.

On retry the previous bad answer is **not** replayed (that anchors the model on its refusal); a
short system hint specific to the failure (refusal, ambiguous recipient, missing message) is
added before the request. The request stays last and unwrapped because the mock derives its
answer from the raw text of the last user message; with a real model, delimiting it would be an
additional injection guard.

---

## Guardrails (`guardrails.py`, `lenient_json.py`, `recipients.py`)

A pure, synchronous function: `extract_payload(raw_answer, user_input) → Accepted | Rejected`.
Every result lists the repairs applied (logged and counted in `/health`).

**1. Find the JSON.** Strict parsing first (`json.raw_decode` at each `{`, which handles prose and
code fences), then a tolerant recursive-descent parser for near-JSON. A value cut off mid-token
is **dropped, never returned partially**: a truncated message fails as missing instead of being
sent half-written.

**2. Normalise.** Case-insensitive keys with aliases; unknown keys dropped.

**3. Ground the recipient in the request.** Recipients present in the user's text are detected
first; none at all → fatal, before any AI call. The AI's `to` must match one of them, and the
request's version is the one sent. Hallucinated or injected addresses are replaced by the
request's only recipient; with several candidates the answer is `ambiguous_recipient`
(retryable).

**4. Channel follows the recipient**: an email address can't receive an SMS. Missing type is
inferred; a conflicting one is corrected.

**5. Message** must be present and non-empty.

Coverage of every variant the mock produces (`provider/responses.py`):

| Mock output | Repair |
|---|---|
| Clean JSON (50%) | none |
| `Recipient`/`body`/`channel`, `To`/`Message`/`Type`, `destination`/`text`/`method` | `key_alias:*` |
| Extra `confidence`, `latency_ms` | `dropped_keys` |
| Missing `type` | `type_inferred` |
| Missing `to` | `recipient_missing` → `recipient_from_input` |
| ` ```json ` block, generic ` ``` ` block | `code_fence` |
| JSON embedded in a sentence | `surrounding_text` |
| Truncated `… "type": "email" ...` | `lenient_json`, `unterminated_object` |
| Single quotes / unquoted keys | `lenient_json`, `single_quotes` / `unquoted_keys` |
| Refusal text (10%) | `Rejected(refusal)`, retryable |

`test_guardrails_fuzz.py` loads the real `provider/responses.py`, forces every branch for all
four k6 prompts, and samples 4,000 random answers: every non-refusal answer is recovered with
the exact recipient, channel and message.

Failure reasons: `no_recipient` (fatal); `refusal`, `no_json`, `empty_response`,
`ambiguous_recipient`, `missing_message`, `invalid_message` (retryable).

---

## Resilience

**AI retry budget.** At most **2 AI calls per request in total**, whatever the failure. The
provider counts every call (failed ones included) toward the scorecard ratio, so the clients
never retry on their own; only the pipeline does. Bad answers are retried immediately with a
hint; transport errors after a jittered backoff; requests with no recipient never reach the AI.
Expected ratio: 1 + 10% refusals ≈ **1.10**; measured **1.097**.

**Rate limiting.** A GCRA limiter admits at most `burst + rate × T` requests in any window
`T`: with `1 + 4.5 × 10 = 46` per 10 s, it stays under the provider's 50. Pacing ourselves is
cheaper than absorbing 429s. A 429 still pauses all workers (`Retry-After` or 2 s), in case
another client shares the quota. (A first fractional-token version drifted in floating point
and could re-sleep forever; GCRA tracks the next allowed send time instead, so it can't.)

**Notification retries.** 408/425/429, 5xx and network errors are re-queued with exponential
backoff and jitter (max 6 attempts) without holding a worker; other 4xx (401, 422…) fail
immediately, since retrying can't fix them. Sending is not
idempotent, so errors distinguish "never sent" (safe to retry) from "sent, no answer"
(`maybe_delivered`); retries are **at-least-once** with a stable `trace_id` (the request id).
A `200` is always final, even with a malformed body.

**Timeouts sized for a degraded provider.** The first k6 run showed 109 AI timeouts and 17
notification timeouts during the peak. InfluxDB's write log showed why: the mock writes a
metric to InfluxDB **synchronously** on every AI call, and while k6 floods the same InfluxDB
each write takes ~100 ms and blocks the provider's event loop (throughput drops from ~44 to
~10 calls/s). Giving up on a slow-but-alive provider is counterproductive: it still processes
and counts the call, and a timed-out notification was most likely delivered, so retrying it
sends a duplicate. Timeouts were raised from 10 s / 5 s to **30 s (AI) / 15 s (notify)**; the
next run had zero timeouts and zero notification errors.

**Fault isolation.** Every worker iteration is wrapped: an unexpected exception fails that one
request (`internal_error`) and the worker carries on. Any unhandled API error returns a JSON
500. A full intake queue still returns `202` with the request marked `failed` (`overloaded`),
so availability isn't hit.

---

## Observability

- **Logs**: one line per state change with `id=<request id>`, so a request can be followed
  end to end with `grep`. Extraction lines list the repairs applied; final lines include the
  total time (`elapsed=`). The uvicorn access log is off by default (`ACCESS_LOG=true` enables
  it): at peak it would write hundreds of lines a second on the event loop serving `/process`.
- **`GET /health`**: requests by status and stage, worker liveness, queue sizes, scheduled
  retries, `ai_calls_per_request` (the scorecard ratio) and every counter (AI calls, rejections
  by reason, each repair, sends, failures by reason). Returns `503 degraded` if a worker has
  died; the Dockerfile's `HEALTHCHECK` uses it.

---

## Configuration

Every setting can be overridden with an environment variable of the same name in upper case.
Invalid values (wrong type, zero or negative numbers) stop the service at startup.

| Variable | Default | Purpose |
|---|---|---|
| `PROVIDER_BASE_URL` | `http://localhost:3001` | AI engine and notification provider |
| `PROVIDER_API_KEY` | `test-dev-2026` | Sent as `X-API-Key`; masked in logs |
| `AI_TIMEOUT_SEC` | `30` | Per AI call (sized for a degraded provider) |
| `MAX_AI_ATTEMPTS` | `2` | Total AI calls per request |
| `AI_RETRY_BACKOFF_SEC` | `0.5` | Base delay after an AI transport error |
| `EXTRACTION_WORKERS` | `100` | Concurrent AI extractions |
| `NOTIFY_TIMEOUT_SEC` | `15` | Per notification call |
| `NOTIFY_WORKERS` | `5` | Concurrent notification senders |
| `NOTIFY_RATE_PER_SEC` / `NOTIFY_BURST` | `4.5` / `1` | Client-side pacing (46 per 10 s) |
| `NOTIFY_COOLDOWN_SEC` | `2` | Global pause after a 429 without `Retry-After` |
| `MAX_NOTIFY_ATTEMPTS` | `6` | Attempts per notification |
| `NOTIFY_RETRY_BACKOFF_SEC` / `MAX_BACKOFF_SEC` | `1` / `30` | Exponential backoff base / cap |
| `QUEUE_MAXSIZE` | `10000` | Extraction intake bound |
| `LOG_LEVEL` / `ACCESS_LOG` | `INFO` / `false` | Logging |

---

## Testing

403 tests, ~4 s, no network. Highlights:

- **Guardrails**: every mock variant, plus edge cases (inner quotes, `10:00` inside unquoted
  JSON, truncation, nested objects, injection, phone formats, pathological input bounded in
  time), and the fuzz test against the real mock generator.
- **Pipeline**: scripted fakes drive every retry path: refusal → retry → sent, double refusal,
  shared AI budget, transient vs permanent errors, 429 cooldown, bounded notify attempts,
  backoff growth and cap, at-least-once trace id, worker survival, duplicate submissions,
  clean shutdown.
- **Rate limiter**: virtual clock; never exceeds the provider window, FIFO fairness, cooldown
  semantics, and a regression test for the floating-point drift.
- **Clients**: request contract, error classification, malformed responses (respx).
- **API**: contract status codes and shapes, idempotency, overload, JSON 500, health.

Each step was also validated against the real mock provider, and the full system with the
official k6 suite.

---

## Known limitations and production path

| Limitation | Production approach |
|---|---|
| In-memory store and queues: state lost on restart, single process only, memory grows with every request | Redis/Postgres with TTL on finished requests; durable queue (SQS, RabbitMQ, Kafka); workers as a separate deployment |
| Throughput capped at ~4.5 notifications/s by the provider | Negotiate quota, batch, or use per-channel providers |
| At-least-once delivery after ambiguous timeouts | Provider-side idempotency on `trace_id` |
| Grounding proves a recipient appears in the request, not that the user meant it (e.g. an address added by injected text) | Confirm with the user when several recipients are present |
| One recipient per request | Return a list of notifications from the extraction |
| No authentication on the service's own API | API gateway or OAuth2 |
| Mock ignores model parameters | With a real LLM: `temperature=0`, structured-output / JSON mode, request wrapped in delimiters |

Design notes and the step-by-step plan: [docs/implementation-plan.md](docs/implementation-plan.md);
initial analysis: [docs/analysis.md](docs/analysis.md).
