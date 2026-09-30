# Ciderlane Return Agent

A command-line customer support agent for Ciderlane, a fictional online clothing store. Built with Python, the OpenAI Agents SDK, and SQLite.

The agent helps customers check return eligibility, start a return, and request human support. It uses an LLM to understand requests and explain results, while application code enforces policy and controls database writes.

## Features

- Verify orders using an order ID and customer email before sharing order details.
- Apply the 30-day return window, final-sale restrictions, and swimwear rules, with human review for qualifying damaged or wrong items.
- Create persistent returns with mock label URLs after explicit approval, and retrieve existing returns on repeat requests.
- Open persistent support cases for policy exceptions, disputes, and verification problems.

## Getting started

Requires Python 3.10+ and an OpenAI API key with access to the model configured in `src/agent.py`.

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Create a `.env` file:

```dotenv
OPENAI_API_KEY=your-api-key
```

Start the CLI using the reference date for the bundled sample orders:

```bash
CIDERLANE_REFERENCE_DATE=2026-08-31 python -m src.main
```

Try this request:

```text
I want to return the white short dress from order CL1002.
My email is maya@example.com.
```

The CLI asks you to approve the specific item before the return tool executes. Enter `y` to approve, or decline to leave the return uncreated. Type `quit` to exit.

Run `python -m src.main` without the reference date to use the current date. Returns and support cases are stored in `data/ciderlane.db`; set `CIDERLANE_DATABASE_PATH` to use another database file.

## How it works

The model has access to four business tools:

| Tool | Arguments | Purpose |
| --- | --- | --- |
| `verify_order` | `order_id`, `email` | Verify the order and return its details. |
| `check_return_eligibility` | `item_id`, optional `reason` | Check eligibility without creating a return. |
| `start_return` | `item_id`, optional `reason` | Create an eligible return or retrieve an existing return and label. |
| `request_human_support` | `escalation_reason`, `summary` | Create or retrieve a support case when trusted state permits it. |

The application owns verified state, authoritative order facts, deterministic policy, authorization, idempotency, and persistence. The model selects an item and return reason, but cannot supply or override the stored delivery date, category, final-sale status, or policy decision.

When a customer requests a return, the model proposes `start_return`. The SDK approval gate pauses execution until the customer approves the concrete item through the CLI. The application then independently validates the verified order and item and performs fresh policy validation before inserting a new return. An earlier eligibility result does not authorize a write.

SQLite enforces persistent, global return idempotency with a unique `(order_id, item_id)` key. Repeat requests retrieve the original return and label, including across conversations and restarts. Eligibility-dependent support escalation uses an immutable application-owned evaluation tied to the verified order; the model cannot invent its order, item, decision, or policy-code provenance.

## Tests and evaluation

Run the deterministic tests:

```bash
python -m unittest discover -s tests
```

Run the live agent evaluations with a configured API key:

```bash
python -m evals.run_evals
```

Each evaluation case uses an isolated database and a fixed reference date. Deterministic checks inspect tool arguments and ordering, verification before action, approval before write, trusted escalation provenance, persisted database side effects, and duplicate-write behavior. An LLM grader assesses only the semantics of the final response.

Current validation results: **101 deterministic tests passing** and **11/11 live evaluation cases passing**.

## Project structure

```text
src/
  main.py          CLI and SDK approval handling
  agent.py         Agent instructions and tool registration
  agent_tools.py   Model-visible tool definitions
  context.py       Verified conversation state and trusted evaluations
  tools/           Order verification, return policy, and support logic
  storage.py       SQLite schema and connections
data/orders.json   Sample order data
tests/             Deterministic tests
evals/             Live evaluation cases and grading harness
docs/              Workflow, tool contracts, and engineering notes
```

## Scope and limitations

This is a demo using sample orders and mock return labels. Support cases are stored locally; there is no external ticketing or shipping integration. Verification and trusted evaluation state last for one CLI conversation, while returns and support cases persist in SQLite.

## Documentation

- [Return workflow](docs/return-workflow.md)
- [Tool contracts](docs/tools.md)
- [Agent boundary](docs/agent-boundary.md)
- [Engineering notes](docs/engineering-notes.md)
