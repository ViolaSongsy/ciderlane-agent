# Engineering Notes

This document explains the current implementation's boundaries, the failures that
motivated them, and how they are validated. It describes the final local demo,
including the browser and CLI entry points.

## Trusted facts and execution-time authorization

An earlier prototype accepted model-supplied policy facts or decisions at the
side-effect boundary. Deterministic code could therefore act on fabricated inputs.
The current interfaces keep authoritative facts in the application:

- `verify_order` matches the order ID and email against stored fixtures and records
  only successful verification in `ReturnContext`.
- Return tools receive only `item_id` and a canonical reason. They reload the
  verified order and validate that the selected item belongs to it.
- Delivery date, category, and final-sale status come from stored data, rather than
  model arguments or client JSON.
- `start_return` checks fresh facts after SDK approval. Earlier eligibility results
  and customer claims about eligibility cannot authorize a new return.

The model still interprets intent and selects an item. The application enforces
access relative to the verified order and policy eligibility for the selected
item; conversational correctness remains a separate evaluation concern.

## Approval and rejection

`start_return_tool` uses `needs_approval=True`. The SDK pauses the invocation;
HTTP and CLI validate and describe the item from trusted order data before asking
for a decision. The internal business function does not collect approval itself.

The HTTP session stores paused SDK state and generates an opaque approval ID. It
serializes chat and approval requests with a session lock, rejects chat while an
approval is pending, and consumes the approval ID before resuming. Stale or replayed
approval requests cannot execute the same paused state again.

Manual testing showed that a rejection message telling the model not to call
`start_return` again could prevent later legitimate requests. The final feedback
states only that this approval was rejected and no return or label was created.
Agent instructions allow a renewed request with fresh approval. Rejection ends
that proposal; it does not permanently prohibit the item.

Cancellation applies to an uncreated conversational request. The prompt checks
current intent before resuming verification or item selection, so merely supplying
more details does not restart a cancelled request. There is no shipment-cancellation
or refund tool.

## Order-scoped verification

A conversation can discuss more than one order. Keeping the previous verified
order active caused a different order's missing-credentials support request to be
rejected as if that new order were already verified.

The small `begin_new_order_request` tool clears `verified_order_id` and
`current_evaluation` when the customer confirms another order. It retains the
conversation ID and all persisted business records. If scope is unclear, the
model asks first. Same-order requests and return retries do not need a reset.

The model decides when to invoke the reset; Python performs the actual state
change. Tool calls are configured sequentially. This keeps the final design within
the existing context and tools, without a separate request controller. A strict
requirement to guarantee every dialogue transition would need explicit application
flow control; the current dialogue rules are measured with regression evals.

## Completed-return queries

Manual testing also found that an eligibility inquiry after return creation could
say only that the item was eligible. Policy evaluation alone omitted the current
business state.

`check_return_eligibility` now verifies order/item membership and calls `find_return`
before evaluating policy. A completed return yields `return_already_exists` and
the original saved label, even if the return window has since expired. It does not
claim that current policy was re-evaluated.

`find_return` opens SQLite in read-only mode. Missing databases or absent returns
tables mean no completed return; reading does not create a file or initialize
schema. Corrupt or unavailable storage returns a query failure, rather than a
potentially misleading policy answer. A pending label request is not a completed
return. The trusted evaluation is cleared before lookup and is populated only
when a new policy evaluation succeeds.

## Durable label keys and recovery

The application coordinates a local return record with an external provider:

1. Under a SQLite write lock, reuse or reserve one idempotency key for the order/item.
2. Commit the key before making HTTP requests.
3. Use the same key for every bounded retry and later approved recovery attempt.
4. After valid provider success, check for concurrent completion, save the return,
   and remove its pending request in one transaction.

Timeouts, connection failures, and HTTP 5xx results are transient. Retries wait
0.1s, 0.2s, 0.4s, and so on, with the configured total-attempt limit. Other HTTP
errors and invalid response bodies fail immediately. Redirects are not followed;
error outputs contain safe codes rather than credentials or response bodies.

HTTP runs outside SQLite transactions. A failed provider request or a failed local
write leaves the durable key available for recovery. The next approved attempt
re-evaluates current policy before contacting the provider again. Completed
returns are globally unique by order and item and reuse the original label.
The provider must honor the key; local uniqueness alone cannot guarantee exactly
one external label.

## Support provenance

A fresh immutable `ReturnEvaluation` contains the verified order, selected item,
decision, and canonical policy codes. Support authorization reads it from context,
checks its order against current verification, and uses its structured metadata.
The model exposes only an escalation reason and factual summary.

- Return exceptions require `exception_flow`.
- Final-sale disputes require `blocked` and the `final_sale` code.
- Continued anger requires `blocked`.
- Verification failures require an unverified order and persist no structured
  order, item, or policy decision.

Codes follow policy precedence. A final-sale swimsuit handled by the swimwear
branch records `restricted_swimwear`, so it cannot supply final-sale-dispute
provenance. Return-window rejection alone does not cause escalation; continued
anger after explanation can authorize review under its own rule.

The model recognizes disputes and emotional context; the tool verifies the
associated business evidence. Identical open cases are deduplicated within a
conversation. Cases are local records, not external tickets or return approvals.

## Multi-turn regression strategy

Manual failures are retained in `evals/manual_failures.json` as historical reports.
Its original collection/status fields are not the current pass/fail result.
Executable acceptance cases live in `evals/cases.json`, which grew from 14 to 30
scenarios. They include rejection retries, order switching, unavailable and
corrected credentials, completed-return inquiries, cancellation, changed intent,
and item-name conflicts.

The runner calls the same backend session and approval handlers used by the UI.
It runs the real agent and SDK with one temporary database per case and a local
mock HTTP label provider. It does not drive browser DOM or test FastAPI transport;
HTTP behavior is covered by deterministic API tests and rendering by manual use.

Scripted turns define `content`, `approvals`, and optional `expected` assertions.
Approvals belong to that turn only. Approvals not declared for a scripted turn
are recorded as failures and never executed. Original string-message cases
retain automatic approval.

The harness records tool inputs/outputs by call ID, SDK resume replays, approval
IDs and decisions, responses, and database state after each turn. Checks include:

- No return write before approval or from a rejected proposal.
- No unauthorized order/item access or fabricated policy inputs.
- Required and forbidden calls, ordered calls, and per-turn row deltas.
- Reused labels and support provenance matching current verified state.
- Fresh approval for renewed requests, rather than consuming a later turn's decision.

`first_business_tool_call` skips only `begin_new_order_request`. It still enforces
verification as the first business action where required. Focused `--case` runs
and incremental JSON `--output` reports make failures reproducible and inspectable.

## Response grading

Business facts and writes are checked in code. A separate `gpt-5.6-sol` grader
checks response meaning; the business agent uses `gpt-5.6-luna`.

Earlier grading could confuse conversation background with the target reply. The
runner now separates the target from reference context and requires the grader
to reproduce the exact target before evaluating it. A mismatch is reported as a
failure; unmet requirements are not filtered away. Reference context is supplied
only for checks that opt into it, and rejection instruction text is excluded from
that factual background.

These checks help detect target confusion, but the grader remains a model. Unit
tests also exercise the evaluator, including incorrect targets, background claims,
call-ID matching, unexpected approvals, and report contents. Semantic grading
complements deterministic checks; it does not replace them.

## Local sessions and deployment scope

Browser sessions, verification, conversation history, policy evidence, and pending
approvals live in one process. Page reload creates a new session; server restart
loses those states. Run one worker. Returns, pending keys, and cases survive when
the database path is reused; a fresh temporary path starts an empty demo database.

The HTTP demo has no production authentication, and order ID/email matching uses
synthetic fixtures. It integrates with a configured HTTP label contract, with a
mock supplied for demos. There is no external ticketing or refund integration.

## Recorded validation

Recorded on **2026-10-08**, after the final prompt consolidation:

| Check | Result |
| --- | --- |
| Deterministic unit tests across 13 files | 169 / 169 |
| Full live agent eval suite | 30 / 30, one full run |
| `different_invalid_order_verifies_before_item_selection` | Passed three targeted runs |
| `cancelled_pending_return_credentials_do_not_resume` | Passed three targeted runs |

The targeted runs used one fixed implementation, followed by the full suite.
Source integrity was checked across the validation runs. The final prompt
consolidation reduced its length from 1,287 to 1,157 English words. Historical
results from the larger, removed controller do not validate this implementation.

These are recorded results for the supplied scenarios, not a promise that every
future conversation will succeed. Reproduce them with the commands in [README](../README.md).
GitHub Actions runs unit tests for main pushes, PRs targeting main, and manual
runs. Paid live agent evals are run separately.
