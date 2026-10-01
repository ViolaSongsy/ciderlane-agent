# M6: six reviewable PRs

Merge in order. Each PR targets one review question, 80–160 changed lines of code
and tests, with a ceiling of 200 (additions + deletions). Count tests in that
budget. If an actual diff exceeds it, split that PR before submitting. Run the
full deterministic suite for every PR. Review each against its predecessor,
not the accumulated M6 diff. Estimates below are budgets, not measured sizes.

1. **Pending-request schema** (this PR; about 60 code/test lines).
   Add `return_label_requests`: one row per `(order_id, item_id)`, a non-null
   unique idempotency key, and creation time. Test constraints, repeat startup,
   and preservation of completed returns. Keep the current return flow active.
   Review question: does the new table leave `returns` unchanged?
2. **HTTP provider and response contract** (about 130–160 lines).
   Add a small `ReturnLabelProvider` with application-owned URL, credentials,
   and timeout configuration. Start with one HTTP attempt. Send trusted order
   and item IDs plus the caller's application key. Accept only a valid 2xx JSON
   response with an absolute HTTP(S) label URL; return safe failure codes.
   Test successful HTTP, authorization headers, malformed responses, and bad
   configuration. Keep it disconnected from `start_return`.
   Review question: what exactly counts as provider success?
3. **Durable key reservation** (about 80–110 lines).
   Add one small return-specific storage helper that generates and commits a
   key on first reservation, or reloads the existing key. Commit and close its
   connection before returning. Test persistence across connections, repeat
   reservation, and reservation failure. No HTTP and no return-flow cutover yet.
   Review question: can a later invocation get exactly the same durable key?
4. **Bounded provider retries** (about 80–110 lines).
   Add application-owned attempt limits. Retry timeout/transport errors and
   5xx with the identical key and payload; do not retry malformed success or
   rejected HTTP requests automatically. Test timeout, exhausted 5xx, 5xx then
   success, and timeout then success using scripted HTTP responses.
   Review question: can retries ever change the external operation's identity?
5. **Safe `start_return` cutover** (about 150–200 lines).
   Replace mock generation after the existing verification and fresh policy
   checks. Retrieve completed duplicates first; recheck while reserving the key;
   commit the reservation;
   release SQLite; call the provider; then atomically insert the completed
   return and remove the pending row. Include failure handling now, leaving
   pending rows intact and exposing no label on failure. Update existing test
   fixtures and assert success, no transaction during HTTP, completed duplicate,
   blocked/exception/invalid/unverified paths, and SDK approval rejection.
   Review question: can HTTP or a success claim happen before authorization or commit?
6. **Recovery proofs and usage docs** (about 100–150 code/test lines).
   Test exhausted failure leaves no completed return; a later invocation reuses
   the saved key; provider success plus local write failure can recover; and
   concurrent callers share one key and completed return. Use isolated SQLite
   and deterministic provider responses. Add concise configuration, contract,
   and recovery-order documentation. No new workflow machinery.
   Review question: can an ambiguous failure recover without a second external operation?

PRs 1–4 only add independently tested building blocks. PR 5 activates HTTP with
all safety invariants already implemented; PR 6 adds recovery evidence and docs.
M6 is complete only after all six PRs and their tests are merged.

Final states: `no request → pending external request → completed return`.
The model still supplies only `item_id` and `reason`. URL, credentials, retries,
authorization, and keys stay application-owned. Rejected approval and unauthorized
paths never call HTTP. Existing completed returns return `return_already_exists`.
The provider must replay success for the same key, including after lost responses
or failed local writes. No queues, webhooks, CRM, RAG, multi-agent architecture,
integration frameworks, background workers, or redesign of M1–M5.
