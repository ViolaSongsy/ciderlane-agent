# Agent Boundary

Ciderlane is a fictional fashion retailer. This agent helps shoppers check return
eligibility, request an approved return, retrieve an existing label, and request
human review. Order data is synthetic and loaded from `data/orders.json`.

## Responsibilities

| Layer | Responsibility |
| --- | --- |
| LLM | Interpret current intent, resolve order and item references, ask clarifying questions, choose tools, and explain their results. |
| Python tools | Match order credentials, validate item membership, read authoritative policy facts, calculate decisions, authorize support, and persist business records. |
| Agents SDK | Pause every `start_return` call for customer approval and resume the approved or rejected call. |
| HTTP / CLI | Describe the proposed item using stored facts and collect approval. The HTTP API also owns session and approval IDs. |
| Label provider | Return a valid label URL and honor the application's persisted idempotency key. |

Intent and dialogue order are instructions to the model, evaluated through
regression scenarios. Verification state, tool parameters, policy evaluation,
approval, and database constraints are application boundaries. A prompt rule
should not be described as a deterministic application guarantee.

## Supported actions

- Verify an order using its actual order ID and checkout email.
- Clarify an ambiguous item or a name that conflicts with the verified order.
- Check eligibility without starting a return.
- Propose a specific return and execute it after fresh approval and policy checks.
- Retrieve an existing return and original label after verification.
- Start a fresh approval after a previous proposal was rejected.
- Clear old verification and policy evidence when the customer confirms another order.
- Create or retrieve an authorized support case.

The model sees five tools. `begin_new_order_request` resets conversation state;
it does not create a return, verify new credentials, or delete saved records.

## Business boundaries

- Return and eligibility tools require an application-owned verified order and
  reject items outside it. Verification failure clears previous authorization.
- The model supplies only an item ID and canonical reason for return operations.
  It cannot supply delivery dates, categories, final-sale flags, or policy decisions.
- Approval authorizes a proposed invocation, not an override of return policy.
- A new label is requested only for `standard_return`. `blocked` and
  `exception_flow` results do not create labels.
- Completed returns are unique by order and item. Repeated reads or approved
  attempts reuse the persisted return and label.
- The agent reports creation only after the relevant tool succeeds.

## Human review

| Situation | Required evidence / behavior |
| --- | --- |
| Credentials cannot be supplied | Use `verification_failed` directly for an unverified order; do not invent credentials. |
| Verification fails | Ask the customer to check the information once. If they confirm it or verification fails again, request review. |
| Restricted item arrived damaged or wrong | Current trusted evaluation must be `exception_flow`. |
| Customer disputes a final-sale decision | Current evaluation must be `blocked` with the `final_sale` policy code. |
| Customer remains angry after a clear policy explanation | Current evaluation must be `blocked`. |

A blocked result alone does not require review. A return-window rejection does
not qualify as a final-sale dispute, although continued anger can qualify under
its own rule. The model recognizes the conversational trigger; the support tool
checks the associated order, decision, and policy evidence.

The support tool accepts only an escalation reason and summary. It derives
structured order, item, decision, and conversation metadata from application
state. Unverified cases contain no structured order, item, or decision. Identical
open cases are reused within a conversation. A case records a request for review;
it does not approve a return.

## Scope

The demo matches an order ID and email; it has no production authentication.
Sessions and pending approvals are in memory, while returns and cases are saved
in SQLite. Cancellation stops an uncreated conversational request. There is no
tool to cancel a completed return, issue a refund, or send a case to an external
ticketing platform.

See [the workflow](return-workflow.md) for request handling and [tool contracts](tools.md)
for exact inputs and results.
