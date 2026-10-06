# Ciderlane Return Agent

Ciderlane Return Agent is an evaluated Python customer-support agent for a fictional fast-fashion store. It verifies customers, applies return policies through deterministic tools, and requests return labels from a configured HTTP provider for eligible items.

## What It Does

- Verifies an order using the order ID and customer email
- Identifies the specific item the customer wants to return
- Checks eligibility against the 30-day, final-sale, and swimwear policies
- Requests a return label for eligible standard returns after customer approval
- Creates persistent support cases for policy exceptions that require human review
- Protects order information until the customer is verified

## Why I Built It

This project explores how to separate an LLM's conversational reasoning from deterministic business rules and side effects. Policy decisions are enforced by Python tools, while unit tests and agent evals check that the system continues to behave correctly.

## Label Provider Configuration

SQLite 3.35 or newer is required for `INSERT ... RETURNING`.

Set these application settings in the environment or `.env` for the CLI or HTTP service:

| Variable | Value |
| --- | --- |
| `CIDERLANE_LABEL_PROVIDER_URL` | Required: provider's label creation endpoint |
| `CIDERLANE_LABEL_PROVIDER_TOKEN` | Required: bearer token |
| `CIDERLANE_LABEL_TIMEOUT_SECONDS` | Defaults to `5`; positive finite timeout per attempt |
| `CIDERLANE_LABEL_MAX_ATTEMPTS` | Defaults to `3`; total attempts, from `1` to `5` |

The provider receives a POST with JSON `order_id` and `item_id`, plus `Authorization: Bearer <token>` and `Idempotency-Key` headers. Success requires a 2xx JSON response containing a valid absolute HTTP(S) `label_url`, for example `{"label_url": "https://carrier.example.test/label.pdf"}`. The provider must return the same label when a successful request is repeated with the same key.

The application commits the key to `return_label_requests` before HTTP. Timeout, connection failure, and 5xx responses trigger bounded retries using that key, with waits of 0.1s, 0.2s, then 0.4s, and so on. Other HTTP errors and malformed responses fail immediately. A failed operation stays pending; a later approved invocation rechecks policy and reuses the saved key. Only a valid provider success followed by a committed local write returns `return_created`. HTTP runs outside SQLite transactions.

Missing or invalid provider settings return `label_provider_not_configured`. See [tool behavior](docs/tools.md#tool-start_return) for all failure codes. Live agent evals also use the configured provider; deterministic tests mock the provider or HTTP boundary.

## HTTP + Browser Demo (M7)

From the project root, install dependencies into the existing virtual environment:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

Set `OPENAI_API_KEY` in the environment or `.env`. For fake labels, start the local
mock provider in another terminal:

```bash
.venv/bin/python -m evals.mock_label_provider
```

Then start the demo with one server worker:

```bash
CIDERLANE_LABEL_PROVIDER_URL=http://127.0.0.1:8765/labels \
CIDERLANE_LABEL_PROVIDER_TOKEN=mock-token \
CIDERLANE_REFERENCE_DATE=2026-08-31 \
.venv/bin/python -m uvicorn src.api:app --host 127.0.0.1 --port 8000 --workers 1
```

Open the [local demo](http://127.0.0.1:8000/). Try: "Please return the white short
dress from order CL1002. My email is maya@example.com." The reference-date override
keeps the bundled August orders within the return window; omit it to use today's
date. The page displays every proposed return before Approve / Reject.

`POST /sessions` creates a server-generated `session_id`.
`POST /sessions/{session_id}/chat` accepts only `{"message": "..."}` and returns
`completed` with a response or `approval_required` with descriptions and an opaque
`approval_id`. `POST /sessions/{session_id}/approval` accepts
`{"approval_id": "...", "decision": "approve"}` (or `"reject"`) and resumes the
stored SDK state. Chat while approval is pending, and consumed or stale approval
requests, return HTTP 409. The browser supplies no trusted order/policy state,
tool arguments, or business idempotency keys.

Sessions live only in one process. Reloading the page creates a new session;
restarting the server loses context, conversation history, and pending approvals.
Use a single worker. Each session retains the existing `ReturnContext` and its
independent `conversation_id`; existing SQLite return/support persistence and
idempotency remain unchanged. This local demo has no authentication.

## Tests

From the project root:

```bash
.venv/bin/python -m unittest discover -s tests
```

The focused HTTP tests use a scripted SDK model, temporary SQLite databases, and a
mock label provider; they require no API key or live model/provider calls:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_api.py'
```

To run agent evals with the local mock HTTP API:

```bash
.venv/bin/python -m evals.run_evals
```

The command starts a localhost mock API on an available port, temporarily configures
the application to use it, and closes it after the run. It needs no separate server
or provider settings. The LLM runs live using `OPENAI_API_KEY` from the environment
or `.env`. Label requests use real HTTP against the mock, which returns fake label
URLs. Each case uses a temporary database, and the harness approves `start_return`
calls automatically.

To evaluate an existing provider using the configuration above:

```bash
.venv/bin/python -m evals.run_evals --use-configured-provider
```

For CLI demos, the mock can also run separately with
`.venv/bin/python -m evals.mock_label_provider`. Its endpoint is
`http://127.0.0.1:8765/labels` and its token is `mock-token`.
