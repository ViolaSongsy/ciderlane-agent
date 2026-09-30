# Engineering Notes

## Return-label validation

### Bug

`create_return_label` accepted any order and item IDs when the caller supplied
`eligibility_decision="standard_return"`. This allowed a fabricated order to
receive a mock return label.

### Root cause

The side-effect tool trusted an agent-supplied decision and did not validate the
order/item pair against stored order data.

### Fix

The tool now rejects unknown order/item pairs and recalculates eligibility from
the stored delivery date, category, and final-sale flag before creating a label.
Regression tests cover fabricated IDs, an unknown item on a real order, a
final-sale item, and an expired item. The application also stores the verified
order ID in per-conversation memory. A label can be created only for that order,
and any failed reverification clears the previous value.

### Remaining limitation

The verification and trusted evaluation state exist only for the lifetime of one
CLI conversation and are not persisted across restarts.


## Trusted eligibility lookup

### Bug

`check_return_eligibility` accepted `delivered_date`, `category`, and
`final_sale` as model-visible inputs, so deterministic policy logic could still
run on incorrect model-supplied order facts.

### Root cause

The policy logic was deterministic, but the trust boundary stopped too early:
authoritative eligibility facts were still being copied through the model.

### Fix

The model now supplies only `item_id` and the canonical return reason. The
application reads `verified_order_id` from `ReturnContext`, reloads that order,
validates the item within the verified order, and reads the stored delivery date,
category, and final-sale flag before evaluating policy.

### Invariant

The model may resolve user intent into an item and return reason, but it cannot
supply or override the authoritative facts used to determine eligibility.


## Trusted support provenance

`check_return_eligibility` clears any previous evaluation before each attempt and
saves a new immutable application-owned evaluation only after a verified order and
item are loaded successfully. The deterministic evaluator returns a narrow set of
canonical policy codes directly alongside its human-readable reasons. Standard
returns have no codes. Support authorization checks the stored decision and codes
rather than parsing text. Codes follow policy precedence, so a final-sale swimsuit
handled by the swimwear branch records `restricted_swimwear`, not `final_sale`.

The model-visible support interface is
`request_human_support(escalation_reason, summary)`. Eligibility-dependent cases
derive their persisted order ID, item ID, and decision from the current evaluation.
`final_sale_dispute` additionally requires the canonical `final_sale` policy code.
The evaluation order must match the conversation's currently verified order.
The internal persistence helper retains conversation-scoped open-case idempotency.


## Guarded return writes

The model-visible `start_return(item_id, reason)` capability replaced a low-level
label write so the model can request the business action without supplying an
eligibility decision or authoritative order facts. A return request can go
directly to this tool; an earlier read-only eligibility check is not required.

The Agents SDK interrupts each `start_return` proposal for approval before the
tool executes. The CLI resolves the proposed item against the verified order
and asks the customer to approve that concrete item. Once approved, the
application reloads the verified order and item, checks for an existing return,
and evaluates current stored facts before inserting a new row. An old
eligibility result or approval cannot make an ineligible item returnable.

SQLite persists returns with a unique `(order_id, item_id)` key. Existing
returns are retrieved with their original label; the uniqueness constraint
also protects concurrent insert attempts. Idempotency is global across
conversations and restarts. The model may propose an action but cannot
authorize its own side effect.


## Structural and trajectory evals

Earlier evals mostly checked final outcomes and tool names. M5 records each
call as `ToolTrace(name, arguments, output)` and applies binary requirement
checks to the arguments, outputs, and ordered trajectory. Checks cover
verification before action, narrow policy and support inputs, and trusted
escalation provenance.

Each case runs against an isolated SQLite database, while turns within the
case share that database. The harness observes SDK approval interruptions and
asserts that a proposed `start_return` has made no return write before approval.
It then reconciles tool outputs with persistent return and support rows.
The duplicate-return case calls `start_return` twice and checks that the second
call returns the same persisted return without a second row.

Deterministic checks establish tool and database correctness. An LLM grader is
used only for the meaning of the customer-facing response.
