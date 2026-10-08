# Return Workflow

The workflow distinguishes an eligibility inquiry from a request to create a
return. Both require a verified order and a resolved item; only return creation
requires SDK approval. Conversation routing is model-driven, with business
checks enforced in tools.

## 1. Establish current intent

Use the customer's latest intent before continuing verification or selection.
For an uncreated request, explicit cancellation means acknowledge that processing
will stop. Later details alone do not restart it. A new request to proceed can
start a new attempt.

Reject declines the current approval. It creates no return or label. A later
request, including “actually return it,” can receive fresh approval. The previous
approval is never reused. This does not cancel a previously completed return.

## 2. Establish the order

Reuse verification only when the request refers to the same order. Clarify an
unclear reference before acting. In particular, after an earlier order was
verified, an unavailable order ID does not prove that a new request belongs to it,
even when the product name matches.

When the customer confirms a different order, call `begin_new_order_request`
and wait for its result. This clears `verified_order_id` and `current_evaluation`,
while retaining `conversation_id` and persisted records. Another order's email
is used only when the customer explicitly supplies or confirms it for this order.

## 3. Verify before item selection

- If the order ID or email is missing but available, ask only for the missing field.
- If the customer cannot supply a field, call `request_human_support` with
  `verification_failed` directly. No failed verification call is required first.
- With both actual values, call `verify_order` unless that same order is already
  verified. Never guess, use placeholders, or submit empty credentials.
- On failure, reveal no order details and do not ask the customer to choose an item.
  Ask them to check the ID and email once. Confirmation of the same information or
  another failure routes to verification support.
- Corrected or newly available credentials can continue an active request after
  escalation. Cancellation must still be respected.

Every verification attempt clears prior authorization and policy evidence;
success sets the verified order ID. For a confirmed order switch with unavailable
credentials, reset is necessary before requesting unverified support.

## 4. Resolve the item

Use the current and earlier customer messages against the verified order.
An exact item name needs no extra selection question. If multiple items match,
ask the customer to choose.

A sole item can resolve a generic request, but cannot override an explicitly
conflicting product name. For example, CL1001 contains a floral long dress; a
request for a white short dress must be clarified before checking that sole item.

## 5. Choose the action

| Customer intent | Action |
| --- | --- |
| “Can I return this?” | Call the read-only `check_return_eligibility`. |
| “I want to return this.” | Propose `start_return` directly; no prior eligibility check is required. |

Both tools take only `item_id` and `reason`. Canonical reasons are `damaged`,
`wrong_item`, `other`, or null; do not require a reason to continue. The application
loads the verified order and stored item facts itself.

Every eligibility question reads current tool results, including questions after
return creation. An earlier eligibility answer can become stale. A failed lookup
returns no policy decision: verify for `order_not_verified`, or clarify the item
for `order_or_item_not_found`. Storage failures must not be presented as eligibility.

## 6. Approve and execute a return request

1. The SDK pauses the `start_return` proposal.
2. HTTP or CLI validates that the proposed item belongs to the verified order and
   describes its stored name, ID, order, and reason. Unresolved proposals are rejected.
3. The customer approves or rejects. Reject skips execution; a renewed request
   later needs a new approval. HTTP consumes its opaque approval ID before resuming.
4. After approval, the tool validates verification, reason, and item membership again.
5. A completed return is returned with its original label before policy evaluation.
6. For a new return, evaluate fresh stored facts. Only `standard_return` proceeds
   to the HTTP label provider and completed-return persistence.
7. Save the return only after valid provider success. Errors do not count as creation.

Because policy is checked during execution, a requested return can require
approval and then be blocked. A pure eligibility inquiry requires no approval.

## 7. Explain the outcome

| Tool outcome | Customer response |
| --- | --- |
| `standard_return` from a query | Explain eligibility and the unworn, unwashed, tags-attached requirements. |
| `return_created` | Confirm the return and label were created; provide the label URL and condition reminders. |
| `return_already_exists` | Report the existing return and original label; do not claim a new return or current policy re-evaluation. |
| `blocked` | Explain the returned policy reason; do not create a label. |
| `exception_flow` | Request human review using the trusted evaluation; provide the actual case result. |
| Creation or lookup error | Explain that the operation did not succeed; do not invent a return, label, or case. |

For `start_return`, blocked and exception results have
`status: "return_not_created"` plus the policy decision. A completed-return lookup
contains saved return fields and no new policy decision. Pending label requests
are not completed returns.

A blocked decision is not an automatic escalation. Separate rules cover
final-sale disputes and continued anger after explanation. Plain disagreement
with a return-window decision is not a final-sale dispute.

## Examples

These examples use the bundled data and reference date `2026-08-31`. Wording may
vary; the required actions and side effects are the acceptance criteria.

### Eligibility question

Customer: “Can I return the white short dress from order CL1002? My email is
maya@example.com.”

Flow: `verify_order` succeeds → `check_return_eligibility(item_id="it00004", reason=null)`
→ `standard_return`. Explain eligibility without approval or label creation.

### Return creation and later inquiry

Customer: “Please return the white short dress from order CL1002. My email is
maya@example.com.”

Flow: successful verification → `start_return` proposal → customer **Approve**
→ `return_created` with saved label. Later, “Can I still return it?” calls the
read-only tool and receives `return_already_exists` with that original label.

### Reject and retry

Customer rejects a floral-short-dress proposal, then says “Sorry, actually return
it.” A new `start_return` proposal receives fresh approval. Only the newly
approved attempt may execute.

### Missing ID for a different order

After verifying CL1002, the customer confirms a different order but cannot
provide its ID. Call `begin_new_order_request` →
`request_human_support(escalation_reason="verification_failed", summary=...)`.
Report the actual case ID and required human review; create no return.

### Restricted-item exception

Customer: “Can I return the pink swimsuit from order CL1002? It arrived damaged.
My email is maya@example.com.”

Flow: successful verification → read-only eligibility with `reason="damaged"`
→ `exception_flow` → support with `return_exception`. No return approval or label
is created for this inquiry.

See [tool contracts](tools.md) for schemas and [engineering notes](engineering-notes.md)
for persistence and regression design.
