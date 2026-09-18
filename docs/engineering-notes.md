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
