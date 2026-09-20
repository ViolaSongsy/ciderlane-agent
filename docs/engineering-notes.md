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

The verification state exists only for the lifetime of one CLI conversation and
is not persisted across restarts. A production design would persist a trusted
eligibility result and pass an opaque eligibility-check ID to side-effect tools.


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
