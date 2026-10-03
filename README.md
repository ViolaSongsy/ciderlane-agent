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

Set these application settings in the environment or `.env` for the CLI:

| Variable | Value |
| --- | --- |
| `CIDERLANE_LABEL_PROVIDER_URL` | Required: provider's label creation endpoint |
| `CIDERLANE_LABEL_PROVIDER_TOKEN` | Required: bearer token |
| `CIDERLANE_LABEL_TIMEOUT_SECONDS` | Defaults to `5`; positive finite timeout per attempt |
| `CIDERLANE_LABEL_MAX_ATTEMPTS` | Defaults to `3`; total attempts, from `1` to `5` |

The provider receives a POST with JSON `order_id` and `item_id`, plus `Authorization: Bearer <token>` and `Idempotency-Key` headers. Success requires a 2xx JSON response containing a valid absolute HTTP(S) `label_url`, for example `{"label_url": "https://carrier.example.test/label.pdf"}`. The provider must return the same label when a successful request is repeated with the same key.

The application commits the key to `return_label_requests` before HTTP. Timeout, connection failure, and 5xx responses trigger bounded retries using that key, with waits of 0.1s, 0.2s, then 0.4s, and so on. Other HTTP errors and malformed responses fail immediately. A failed operation stays pending; a later approved invocation rechecks policy and reuses the saved key. Only a valid provider success followed by a committed local write returns `return_created`. HTTP runs outside SQLite transactions.

Missing or invalid provider settings return `label_provider_not_configured`. See [tool behavior](docs/tools.md#tool-start_return) for all failure codes. Live agent evals also use the configured provider; deterministic tests mock the provider or HTTP boundary.

## Tests

From the project root:

```bash
.venv/bin/python -m unittest discover -s tests
```
