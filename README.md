# Ciderlane Return Agent

A multi-turn customer support AI agent for a fictional fashion retailer. It helps customers verify orders, check return eligibility, request returns, retrieve existing labels, and escalate exceptions to human support.

**Stack:** Python 3.11 · OpenAI Agents SDK · FastAPI · SQLite · HTTP integration

**Engineering focus:** How can an LLM handle flexible conversations without being trusted to authorize business operations or external side effects?

## Engineering highlights

- **Trusted business state:** The LLM selects tools and supplies an item ID/reason; Python owns verified order identity, policy facts, and eligibility decisions. Tools reject unverified or mismatched items.
- **Approval-gated writes:** `start_return` pauses for explicit customer approval, then independently checks current policy before creating a return. Rejected approvals cannot be reused.
- **Recoverable external operations:** SQLite persists an idempotency key before the HTTP label request. Bounded retries and later approved attempts reuse that key; completed returns are unique by `(order_id, item_id)`.
- **Authorized escalation:** Human support cases use application-owned evaluation evidence and are deduplicated within a conversation.
- **Failure-driven evaluation:** Manual conversation failures feed multi-turn regression scenarios that inspect tool calls, approval decisions, response meaning, and database side effects.

## Run the browser demo

Requires **Python 3.11**, **SQLite 3.35+**, and an OpenAI API key with access to the configured agent model (`gpt-5.6-luna`). From the repository root:

**1. Install and configure**

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Create a local `.env` file with `OPENAI_API_KEY=your_key`.

**2. Start the mock label provider** (terminal 1)

```bash
.venv/bin/python -m evals.mock_label_provider
```

**3. Start FastAPI** (terminal 2)

```bash
CIDERLANE_DATABASE_PATH="$(mktemp -d)/ciderlane-demo.db" \
CIDERLANE_LABEL_PROVIDER_URL=http://127.0.0.1:8765/labels \
CIDERLANE_LABEL_PROVIDER_TOKEN=mock-token \
CIDERLANE_REFERENCE_DATE=2026-08-31 \
.venv/bin/python -m uvicorn src.api:app --host 127.0.0.1 --port 8000 --workers 1
```

Open **http://127.0.0.1:8000** and try:

> Please return the white short dress from order CL1002. My email is maya@example.com.

Approve or reject the proposed return. To explore other paths, ask about the **final-sale black pants** or the **damaged pink swimsuit** in the same order. The fixed reference date keeps the sample return eligible; the temporary database starts fresh each time. Labels are simulated, not purchased.

## Tests and evaluations

| Recorded check (2026-10-08) | Result |
| --- | --- |
| Deterministic unit tests | **169 / 169 passed** |
| Full live multi-turn agent suite | **30 / 30 passed** in one recorded run |

Unit tests use scripted models and mocked/local services; GitHub Actions runs them on `main` pushes and PRs. Live evals use the real configured models, an isolated database per case, a mock HTTP provider, deterministic assertions, and a separate response grader. They incur API usage and are run manually.

```bash
# Unit tests — no OpenAI API key needed
OPENAI_AGENTS_DISABLE_TRACING=1 .venv/bin/python -m unittest discover -s tests

# Live evals — calls OpenAI models
OPENAI_AGENTS_DISABLE_TRACING=1 .venv/bin/python -m evals.run_evals \
  --output /tmp/ciderlane-evals.json
```

These results cover the recorded cases, not all possible conversations. See the [validation notes](docs/engineering-notes.md#recorded-validation).

## Scope and design notes

This is a **local demo with synthetic order data**, not a production deployment. Browser sessions and pending approvals are process-local (one worker); SQLite persists business records when configured with a stable database path. There is no production authentication, real shipping-label or ticketing integration, or operational monitoring.

For implementation details, see [agent trust boundaries](docs/agent-boundary.md), [return workflow](docs/return-workflow.md), [tool contracts](docs/tools.md), and [engineering decisions and recovery](docs/engineering-notes.md).
